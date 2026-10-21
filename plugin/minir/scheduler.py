"""One scheduling surface for the plugin's turn and time work.

Every recurring decision the plugin makes is registered here:

* **turn** tasks run on the host's turn thread, through :meth:`Scheduler.on_turn`.
  The base/dialectic counters are the one turn task today: one tick per user turn.
* **time** tasks run on the scheduler's single daemon clock: the three-day card
  review and the idle-session extraction poll their own durable state and run
  only when that state says they are due.

The scheduler owns no domain logic. A task is a callable plus a cadence; this
module decides *when* it runs, records the outcome per task (runs, failures,
skips, last detail, duration) and emits one structured line per execution, so
every task type is observable in one place. A task that raises is contained:
the clock keeps running and the failure is counted and logged.

Time tasks run inline on the clock thread, deliberately: the two heavy ones are
rare (3 days, 24h idle) and serialising them means at most one model call from
the clock is in flight when the provider shuts down.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

TURN = "turn"
TIME = "time"

#: How often the clock wakes to check per-task cadences.
DEFAULT_TICK_SECONDS = 1.0
_MAX_DETAIL = 240


def _describe(result: Any) -> str:
    """Short, log-safe rendering of a task's return value (empty means "nothing happened")."""
    if result is None:
        return ""
    if isinstance(result, bool):
        return "ok" if result else "failed"
    if isinstance(result, int):
        return f"count={result}"
    if isinstance(result, Mapping):
        text = ", ".join(f"{key}={value}" for key, value in result.items())
    elif isinstance(result, str):
        text = result
    else:
        text = repr(result)
    text = " ".join(text.split())
    return text if len(text) <= _MAX_DETAIL else text[:_MAX_DETAIL] + "…"


@dataclass
class Task:
    """One registered task and its ledger."""

    name: str
    kind: str
    run: Callable[..., Any]
    every: float = 0.0
    due: Optional[Callable[[], bool]] = None
    next_at: float = 0.0
    runs: int = 0
    failures: int = 0
    skipped: int = 0
    last_at: float = 0.0
    last_ms: float = 0.0
    last_outcome: str = ""
    last_detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "every": self.every,
            "runs": self.runs,
            "failures": self.failures,
            "skipped": self.skipped,
            "last_at": self.last_at,
            "last_ms": round(self.last_ms, 1),
            "last_outcome": self.last_outcome,
            "last_detail": self.last_detail,
        }


class Scheduler:
    """Turn and time tasks behind one ledger and one clock.

    Thread-safe; a task failure is counted and logged, never raised into the caller.
    """

    def __init__(self, *, tick: float = DEFAULT_TICK_SECONDS):
        self._tick = max(0.01, float(tick))
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- registration ---------------------------------------------------------

    def add_turn(self, name: str, run: Callable[..., Any]) -> Task:
        """Register a task driven by :meth:`on_turn` (``run(session_id=, turn_number=)``)."""
        return self._add(Task(name=str(name), kind=TURN, run=run))

    def add_time(self, name: str, every: float, run: Callable[[], Any], *,
                 due: Optional[Callable[[], bool]] = None,
                 first_delay: Optional[float] = None) -> Task:
        """Register a polled task.

        ``every`` is the clock cadence; ``due`` is the task's own durable check and
        suppresses the run (counted as a skip) when it returns false. ``first_delay``
        defaults to one cadence; pass ``0`` for a task that should catch up at startup.
        """
        interval = max(0.01, float(every))
        delay = interval if first_delay is None else max(0.0, float(first_delay))
        return self._add(Task(name=str(name), kind=TIME, run=run, every=interval,
                              due=due, next_at=time.monotonic() + delay))

    def _add(self, task: Task) -> Task:
        with self._lock:
            # Re-initialising a provider replaces the slot; two copies of one task must never
            # run on two cadences.
            self._tasks[task.name] = task
        return task

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        """Start the clock when there is a time task; returns whether a thread was started."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            timed = [task for task in self._tasks.values() if task.kind == TIME]
            if not timed:
                return False
            inventory = ", ".join(f"{task.name}@{task.every:g}s" for task in timed)
            turns = ", ".join(task.name for task in self._tasks.values() if task.kind == TURN)
            self._stop.clear()
            logger.info("Minir scheduler started: turn=[%s] time=[%s]", turns, inventory)
            self._thread = threading.Thread(target=self._loop, name="minir-scheduler", daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the clock and join it; idempotent, and safe from inside a task."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("Minir scheduler did not stop within %.0fs", timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a scheduler failure must never kill the clock
                logger.warning("Minir scheduler tick failed", exc_info=True)
            if self._stop.wait(self._tick):
                break

    # -- execution ------------------------------------------------------------

    def on_turn(self, session_id: str, turn_number: Any, **kwargs: Any) -> list[str]:
        """Run every turn task once; returns the task names that ran."""
        with self._lock:
            tasks = [task for task in self._tasks.values() if task.kind == TURN]
        ran: list[str] = []
        for task in tasks:
            self._execute(task, TURN, {"session_id": session_id,
                                       "turn_number": turn_number, **kwargs})
            ran.append(task.name)
        return ran

    def tick(self, now: Optional[float] = None) -> list[str]:
        """Run every time task whose cadence elapsed; returns the names that ran.

        ``now`` is a ``time.monotonic()`` value (injectable for tests).
        """
        stamp = time.monotonic() if now is None else float(now)
        with self._lock:
            tasks = [task for task in self._tasks.values() if task.kind == TIME]
        ran: list[str] = []
        for task in tasks:
            with self._lock:
                if stamp + 1e-9 < task.next_at:
                    continue
                task.next_at = stamp + task.every
            if task.due is not None:
                try:
                    if not task.due():
                        with self._lock:
                            task.skipped += 1
                        logger.debug("Minir scheduler: task=%s trigger=time skipped", task.name)
                        continue
                except Exception as exc:
                    self._record(task, "error", f"due check failed: {exc}", 0.0)
                    logger.warning("Minir scheduler: task=%s trigger=time due check failed: %s",
                                   task.name, exc, exc_info=True)
                    continue
            self._execute(task, TIME, {})
            ran.append(task.name)
        return ran

    def _execute(self, task: Task, trigger: str, kwargs: dict[str, Any]) -> None:
        started = time.perf_counter()
        try:
            result = task.run(**kwargs)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            self._record(task, "error", f"{type(exc).__name__}: {exc}", elapsed)
            logger.warning("Minir scheduler: task=%s trigger=%s failed after %.1fms: %s",
                           task.name, trigger, elapsed, exc, exc_info=True)
            return
        elapsed = (time.perf_counter() - started) * 1000.0
        detail = _describe(result)
        # A task returning literal False means "due but did not publish" (the card review's
        # retry path). Zero is a count, not a failure.
        if result is False:
            self._record(task, "retry", detail, elapsed)
            logger.warning("Minir scheduler: task=%s trigger=%s outcome=retry detail=%s ms=%.1f",
                           task.name, trigger, detail or "-", elapsed)
            return
        self._record(task, "ok", detail, elapsed)
        if detail:
            logger.info("Minir scheduler: task=%s trigger=%s outcome=ok detail=%s ms=%.1f",
                        task.name, trigger, detail, elapsed)
        else:
            logger.debug("Minir scheduler: task=%s trigger=%s outcome=ok ms=%.1f",
                         task.name, trigger, elapsed)

    def _record(self, task: Task, outcome: str, detail: str, elapsed_ms: float) -> None:
        with self._lock:
            task.runs += 1
            if outcome in {"error", "retry"}:
                task.failures += 1
            task.last_at = time.time()
            task.last_ms = elapsed_ms
            task.last_outcome = outcome
            task.last_detail = detail

    # -- introspection --------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-task ledger for ``minir_stats`` and tests."""
        with self._lock:
            return {name: task.as_dict() for name, task in self._tasks.items()}
