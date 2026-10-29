"""Background agent runs the adviser is allowed to observe.

``minir_agents`` hands every background job a session id it can address (the run's own
``AIAgent.session_id``) and registers it here. Three readers use this table:

* the adviser recorder hooks, which admit a watched session where they would otherwise drop
  everything that is not the provider's own session;
* the realtime mirror, which writes each observed event to the run's own stream;
* the progress line, which keeps the run's last steps so the session that dispatched it sees the
  run move.

Nothing here budgets reviews or reads config: ``adviser_mode.enabled`` turns the whole observation
on, ``adviser_mode.observe_children`` (default false) decides whether a registered run is admitted
at all, and the adviser's existing per-turn cap and roster cadence bound the cost.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

#: A grandchild is not watched: review cost grows with depth, and the parent review already sees
#: the grandchild's work as the child's own tool calls.
DEFAULT_MAX_DEPTH = 1
#: Kept for the progress line; the ring itself is the adviser's.
PROGRESS_STEPS = 8
#: Lines inside this window merge into one push, so a fast tool loop cannot flood the parent.
PROGRESS_MIN_GAP = 2.0

CHILD_BRIEF = (
    "[background agent review]\n"
    "You are reviewing a live background agent run ({agent}, job {job}). It is not the "
    "operator's session and it cannot answer you. Your text is inserted after its next tool "
    "batch as an adviser row: write one to three sentences naming the exact tool or step to "
    "change, and nothing else.\n"
    "[end background agent review]\n"
)


@dataclass
class Watch:
    """One background run the adviser is allowed to observe."""

    session: str
    parent: str
    agent: str
    job_id: str
    depth: int = 1
    #: Whether the adviser may record this run at all. A pass core shipped with the plugin
    #: (``plugins/minir/agents``) is dispatched by the plugin itself, its output is a JSON
    #: contract, and it stays unobservable whatever ``adviser_mode.observe_children`` says.
    observe: bool = True
    #: The profile that dispatched the run. One process can serve more than one profile, and a
    #: watch may only be read by the profile whose streams it was written to.
    profile: str = ""
    steps: deque = field(default_factory=lambda: deque(maxlen=PROGRESS_STEPS))
    advice: list = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    pending_progress: str = ""
    last_push_at: float = 0.0


def child_session_id(parent_session: str, agent: str, job_id: str) -> str:
    """The host session id of one background run: stable, addressable, one per job."""
    parent = str(parent_session or "").strip() or "session"
    name = str(agent or "").strip() or "agent"
    job = str(job_id or "").strip() or "job"
    return f"{parent}:minir:{name}:{job}"


class WatchRegistry:
    """Process-wide table of watched runs, keyed by the run's session id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Watch] = {}

    def register(self, *, parent: str = "", agent: str = "", job_id: str = "",
                 depth: int = 1, max_depth: int = DEFAULT_MAX_DEPTH,
                 capabilities: Optional[dict] = None, profile: str = "",
                 observe: bool = True) -> Optional[Watch]:
        """Watch one run; ``None`` when it has no job id or is too deep to watch."""
        if not str(job_id or "").strip():
            return None
        try:
            level = int(depth)
        except (TypeError, ValueError):
            level = DEFAULT_MAX_DEPTH
        try:
            ceiling = int(max_depth)
        except (TypeError, ValueError):
            ceiling = DEFAULT_MAX_DEPTH
        if level > max(0, ceiling):
            return None
        watch = Watch(session=child_session_id(parent, agent, job_id),
                      parent=str(parent or ""), agent=str(agent or ""),
                      job_id=str(job_id).strip(), depth=level, observe=bool(observe),
                      profile=str(profile or ""),
                      capabilities=dict(capabilities or {}))
        with self._lock:
            self._items[watch.session] = watch
        return watch

    def unregister(self, session: str) -> None:
        with self._lock:
            self._items.pop(str(session or ""), None)

    def get(self, session: str) -> Optional[Watch]:
        with self._lock:
            return self._items.get(str(session or ""))

    def is_child(self, session: str) -> bool:
        return self.get(session) is not None

    def note_step(self, session: str, line: str) -> None:
        """One raw progress line per tool batch; bounded, newest last."""
        watch = self.get(session)
        text = str(line or "").strip()
        if watch is not None and text:
            watch.steps.append(text)

    def push_line(self, session: str, line: str, *, gap: float = PROGRESS_MIN_GAP,
                  now: Optional[float] = None) -> str:
        """Merge lines inside the gap window; return the text to push now, or '' to wait."""
        watch = self.get(session)
        text = str(line or "").strip()
        if watch is None or not text:
            return ""
        merged = f"{watch.pending_progress}; {text}" if watch.pending_progress else text
        clock = time.time() if now is None else float(now)
        if clock - watch.last_push_at < max(0.0, float(gap)):
            watch.pending_progress = merged
            return ""
        watch.pending_progress = ""
        watch.last_push_at = clock
        return merged

    def note_advice(self, session: str, text: str) -> None:
        """Keep what reached the run, in order; a caller can audit what the agent was told."""
        watch = self.get(session)
        if watch is not None and text:
            watch.advice.append(str(text))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


REGISTRY = WatchRegistry()


def register(**kwargs: Any) -> Optional[Watch]:
    return REGISTRY.register(**kwargs)


def unregister(session: str) -> None:
    REGISTRY.unregister(session)


def get(session: str) -> Optional[Watch]:
    return REGISTRY.get(session)


def is_child(session: str) -> bool:
    return REGISTRY.is_child(session)


def note_step(session: str, line: str) -> None:
    REGISTRY.note_step(session, line)


def push_line(session: str, line: str, **kwargs: Any) -> str:
    return REGISTRY.push_line(session, line, **kwargs)


def note_advice(session: str, text: str) -> None:
    REGISTRY.note_advice(session, text)


def clear() -> None:
    REGISTRY.clear()


def child_brief(session: str) -> str:
    """The extra system paragraph for a child review; empty for the primary session."""
    watch = get(session)
    if watch is None:
        return ""
    return CHILD_BRIEF.format(agent=watch.agent or "agent", job=watch.job_id or "job")
