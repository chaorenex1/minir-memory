"""Turn counting for the Minir base / dialectic-reasoning layers — stdlib only.

Two layers, one turn stream. ``on_turn_start`` ticks both counters with the host's
own per-session turn number (``agent._user_turn_count``, already incremented for the
turn that is starting), and each layer fires a job every ``interval`` turns with the
time window the job should reason over.

Three rules make this safe under the host's threading model:

* **The host owns the number.** ``on_turn_start`` receives the host's counter, so this
  module never derives a turn number of its own; it adopts the host value and keeps
  ``max(local + 1, host)`` so a resume (host count rehydrated from the transcript) moves
  the counter forward instead of double-counting.
* **A watermark, not a modulo.** Triggering on ``turns % interval == 0`` is only
  equivalent while nothing else reads the counter; the session-end and pre-compress
  paths read it from other threads and can fire more than once per boundary, which the
  modulo turns into overlapping or dropped windows. ``turns - last_trigger_turn >=
  interval`` is the same cadence for the periodic path, and the *same* test degenerates
  to "are there unprocessed turns?" at a boundary — every trigger is idempotent.
* **Windows are edge-to-edge.** ``from_ts`` of the next window is ``to_ts`` of the
  previous one (the first one starts at the session's start time), so no turn can fall
  between two windows and be reasoned over twice.

State is pure in-memory bookkeeping; the caller persists the jobs it returns. Every
entry point takes an injectable ``now`` so the logic is testable without sleeping.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

BASE_LAYER = "base"
DIALECTIC_LAYER = "dialectic"

DEFAULT_BASE_INTERVAL = 20
DEFAULT_DIALECTIC_INTERVAL = 10
DEFAULT_SESSION_MODE = "default"

_MAX_SESSIONS = 64  # long-lived processes (gateway) must not accumulate session state forever


def _positive(value: Any, default: int) -> int:
    """Config/env value -> interval >= 1 (anything unparsable falls back to *default*)."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


@dataclass
class LayerCounter:
    """One layer's progress: how far it has counted, and the last window it dispatched."""

    layer: str
    interval: int
    turns: int = 0
    last_trigger_turn: int = 0
    last_trigger_ts: float = 0.0

    def due(self) -> bool:
        """True when a full interval has accumulated since this layer's last trigger."""
        return self.turns - self.last_trigger_turn >= self.interval

    def as_dict(self) -> dict[str, Any]:
        return {"turns": self.turns, "interval": self.interval,
                "last_trigger_turn": self.last_trigger_turn, "last_trigger_ts": self.last_trigger_ts}


@dataclass
class Trigger:
    """A layer job to run: reason over ``session_id``'s history in ``(from_ts, to_ts]``."""

    layer: str
    session_id: str
    from_ts: float
    to_ts: float
    turns: int


@dataclass
class SessionCounter:
    """Per-session counting state; ``layers`` holds one :class:`LayerCounter` per layer."""

    session_id: str
    mode: str
    started_at: float
    layers: dict[str, LayerCounter] = field(default_factory=dict)
    last_turn_number: int = 0
    closed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "mode": self.mode, "started_at": self.started_at,
                "last_turn_number": self.last_turn_number, "closed": self.closed,
                "layers": {name: counter.as_dict() for name, counter in self.layers.items()}}


class TurnTracker:
    """Counts turns per session and reports due layer jobs. Thread-safe; no I/O."""

    def __init__(self, *, base_interval: int = DEFAULT_BASE_INTERVAL,
                 dialectic_interval: int = DEFAULT_DIALECTIC_INTERVAL,
                 default_mode: str = DEFAULT_SESSION_MODE):
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionCounter] = {}
        self.configure(base_interval=base_interval, dialectic_interval=dialectic_interval,
                       default_mode=default_mode)

    # -- configuration --------------------------------------------------------

    def configure(self, *, base_interval: Any = None, dialectic_interval: Any = None,
                  default_mode: Optional[str] = None) -> None:
        """Set the intervals/mode applied to sessions opened from now on.

        Existing sessions keep the intervals they started with: changing an interval
        mid-session would retroactively move a watermark that already dispatched jobs.
        """
        with self._lock:
            if base_interval is not None:
                self._base_interval = _positive(base_interval, DEFAULT_BASE_INTERVAL)
            if dialectic_interval is not None:
                self._dialectic_interval = _positive(dialectic_interval, DEFAULT_DIALECTIC_INTERVAL)
            if default_mode is not None:
                self._default_mode = str(default_mode) or DEFAULT_SESSION_MODE

    @property
    def base_interval(self) -> int:
        return self._base_interval

    @property
    def dialectic_interval(self) -> int:
        return self._dialectic_interval

    @property
    def default_mode(self) -> str:
        return self._default_mode

    # -- session lifecycle ----------------------------------------------------

    def begin(self, session_id: str, *, mode: Optional[str] = None, now: Optional[float] = None) -> SessionCounter:
        """Get-or-create the state for *session_id*, reopening a stopped session.

        Idempotent by contract: the provider's ``initialize``/``on_session_switch`` and the
        plugin's ``on_session_start``/``on_session_reset`` all open the same session, and a
        second call must never wipe progress the first one already recorded. A stopped
        session is reopened because "begin" is the host telling us the session is live.
        """
        with self._lock:
            return self._open(session_id, mode=mode, now=now, reopen=True)

    def stop(self, session_id: str) -> None:
        """Freeze a session's counters: later ticks on it are ignored."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None:
                state.closed = True

    def inherit(self, old_session_id: str, new_session_id: str, *,
                now: Optional[float] = None) -> SessionCounter:
        """Carry ``/branch`` progress into the new session id.

        A branch continues its parent's transcript, so it must continue the count and the
        watermarks too — restarting at zero would re-reason over history the parent's jobs
        already covered.
        """
        with self._lock:
            source = self._sessions.get(old_session_id)
            target = self._open(new_session_id, mode=None, now=now, reopen=True)
            if source is not None:
                target.mode = source.mode
                target.last_turn_number = source.last_turn_number
                for name, counter in target.layers.items():
                    previous = source.layers.get(name)
                    if previous is not None:
                        counter.turns = previous.turns
                        counter.last_trigger_turn = previous.last_trigger_turn
                        counter.last_trigger_ts = previous.last_trigger_ts
            return target

    # -- counting -------------------------------------------------------------

    def tick(self, session_id: str, turn_number: Any, *,
             now: Optional[float] = None) -> list[Trigger]:
        """Advance both layers by the turn that is starting; return the jobs that are due."""
        with self._lock:
            state = self._open(session_id, mode=None, now=now)
            if state.closed:
                return []
            stamp = time.time() if now is None else now
            try:
                host_turn = int(turn_number)
            except (TypeError, ValueError):
                host_turn = 0
            state.last_turn_number = max(state.last_turn_number, host_turn)
            for counter in state.layers.values():
                counter.turns = max(counter.turns + 1, host_turn)
            return [self._fire(state, counter, stamp) for counter in state.layers.values()
                    if counter.due()]

    def tail(self, session_id: str, *, now: Optional[float] = None, close: bool = False) -> list[Trigger]:
        """Flush the dialectic layer's un-reasoned tail, e.g. at a session boundary.

        A no-op when the counter already sits on a dispatched watermark (the periodic tick
        covered those turns), which is what keeps a boundary that fires several hooks — a
        ``/new`` runs ``on_session_finalize``, ``on_session_end`` and ``on_session_reset`` —
        from queuing the same window more than once. ``close`` freezes the session first.

        A stopped session still flushes: the host fires its boundary hooks in an order that
        freezes the session *before* the queued tail arrives (CLI ``/new`` sends
        ``on_session_finalize`` synchronously and only then queues ``on_session_end`` on the
        memory worker), and the watermark — not the stop — is what makes this exactly-once.
        Only ``tick`` treats a stop as final.
        """
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return []
            stamp = time.time() if now is None else now
            counter = state.layers[DIALECTIC_LAYER]
            triggers = [self._fire(state, counter, stamp)] if counter.turns > counter.last_trigger_turn else []
            if close:
                state.closed = True
            return triggers

    # -- introspection --------------------------------------------------------

    def snapshot(self, session_id: str) -> Optional[dict[str, Any]]:
        """Counting state for *session_id* (``None`` when the session was never opened)."""
        with self._lock:
            state = self._sessions.get(session_id)
            return state.as_dict() if state is not None else None

    def reset(self) -> None:
        """Drop every session's state (tests, and a full profile teardown)."""
        with self._lock:
            self._sessions.clear()

    # -- internals ------------------------------------------------------------

    def _open(self, session_id: str, *, mode: Optional[str], now: Optional[float],
              reopen: bool = False) -> SessionCounter:
        """Get-or-create under the lock; the caller owns the lock.

        ``reopen`` distinguishes "the host says this session is live" (``begin``/``inherit``)
        from "a turn arrived" (``tick``) — a tick must not resurrect a stopped session.
        """
        session_id = str(session_id or "")
        state = self._sessions.get(session_id)
        if state is not None:
            if reopen:
                state.closed = False
            return state
        if len(self._sessions) >= _MAX_SESSIONS:
            self._evict()
        stamp = time.time() if now is None else now
        state = SessionCounter(
            session_id=session_id, mode=str(mode or self._default_mode), started_at=stamp,
            layers={name: LayerCounter(layer=name, interval=interval, last_trigger_ts=stamp)
                    for name, interval in ((BASE_LAYER, self._base_interval),
                                           (DIALECTIC_LAYER, self._dialectic_interval))},
        )
        self._sessions[session_id] = state
        return state

    def _evict(self) -> None:
        """Drop one session to bound memory.

        Least valuable first: an entry that never ticked (a subagent's ``on_session_start``
        opens one, but only the bound primary session is ever ticked), then a closed session,
        then the oldest — so speculative entries can never displace an ongoing count.
        """
        ordered = sorted(self._sessions.items(),
                         key=lambda item: (bool(item[1].last_turn_number), not item[1].closed))
        self._sessions.pop(ordered[0][0], None)

    @staticmethod
    def _fire(state: SessionCounter, counter: LayerCounter, now: float) -> Trigger:
        """Build the trigger a due counter owes, and move its watermark onto it."""
        trigger = Trigger(layer=counter.layer, session_id=state.session_id,
                          from_ts=counter.last_trigger_ts, to_ts=now, turns=counter.turns)
        counter.last_trigger_turn = counter.turns
        counter.last_trigger_ts = now
        return trigger


TRACKER = TurnTracker()
"""Process-wide counting state.

The host can construct this plugin twice (memory-provider load plus general plugin
discovery) and drive only one instance, while plugin-hook callbacks stay bound to the
other — a module-level registry keeps both views on the same counters. Memory-manager
state is per-session anyway, so nothing here is instance-scoped.
"""
