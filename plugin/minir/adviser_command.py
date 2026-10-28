"""``/adviser``: switch the orchestration contract a session runs under.

Two modes exist. ``dispatch`` (the default) is the auto-loaded hermes-dispatch loop. ``multagents``
is the hermes-lead bundle: judgment, scope with a rubric, one Kanban goal card, roles routed
through ``minir_agents``, and the evidence and acceptance gates that bundle defines.

The switch has to survive the command turn, and a plugin command cannot do that with its return
value: the host prints a command's result to the console and never feeds it to the model. So the
handler records the mode, and the plugin's ``pre_llm_call`` hook returns the directive below while
the mode is on. The host injects a hook's string into the *next* turn's user message (never the
system prompt, which is built once per session and replayed for prompt caching), which is exactly
the "arm now, act on the next input" contract this command promises.

Why the directive repeats every turn instead of landing once: a context compression can drop the
hermes-lead body from the window while the session keeps looking normal, and the directive is what
reloads it. Its length is bounded for the same reason it repeats.

State is in-memory and keyed by session id: a mode belongs to one conversation, not to the
profile, so a restart or a fresh session lands back in dispatch. ``MAX_SESSIONS`` bounds the map
because the gateway process lives for weeks and must not accumulate session state forever.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

MODE_DISPATCH = "dispatch"
MODE_MULTIAGENTS = "multagents"

# The user's own spelling, plus the two shapes people type for it.
MULTIAGENTS_NAMES = frozenset({"multagents", "multiagents", "lead"})
DISPATCH_NAMES = frozenset({"dispatch", "off", "default"})

USAGE = "usage: /adviser multagents | dispatch | status"

MAX_SESSIONS = 64

# Injected before every turn's user message while the mode is on. It names the switch it takes and
# the command that ends it, so the model never has to guess either one.
MULTIAGENTS_DIRECTIVE = (
    "[adviser mode: multagents]\n"
    "This session runs the hermes-lead contract, not hermes-dispatch. Before acting on this "
    "message: if the hermes-lead skill body is not in context, load it with "
    "skill_view(name=\"minir:hermes-lead\") (fallback skill_view(name=\"hermes-lead\"); if both "
    "fail, report the missing skill and stop). Then run its Lead loop: task judgment over "
    "references/jev.md, scope with a rubric row per requirement, one Kanban goal card, route, "
    "evidence gate, implement, acceptance gate, cleanup, deliver. Dispatch roles with the "
    "minir_agents tool on references/model_route.yaml; if that tool is absent, say so instead of "
    "substituting another. hermes-dispatch stays in the system prompt but no longer governs: use "
    "its rules only where hermes-lead names them. End the mode with /adviser dispatch."
)

_ARMED = (
    "adviser: multagents armed for this session. The next message runs the hermes-lead loop; "
    "hermes-dispatch no longer governs. Clear it with /adviser dispatch."
)
_RELEASED = (
    "adviser: dispatch restored. hermes-dispatch governs again and the next message dispatches "
    "no role."
)
_ARMED_STATUS = "adviser: mode=multagents (hermes-lead governs; hermes-dispatch superseded)."
_DISPATCH_STATUS = "adviser: mode=dispatch (hermes-dispatch governs)."


class ModeSwitch:
    """Per-session orchestration mode. Thread-safe; no I/O."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._modes: dict[str, str] = {}

    # -- state ----------------------------------------------------------------

    def mode(self, session_id: str) -> str:
        """The contract *session_id* runs under; an unknown session is in dispatch."""
        with self._lock:
            return self._modes.get(session_id, MODE_DISPATCH)

    def arm(self, session_id: str) -> None:
        with self._lock:
            self._prune()
            self._modes[session_id] = MODE_MULTIAGENTS

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._modes.pop(session_id, None)

    def clear_all(self) -> None:
        """Drop every mode; test isolation, and the shape a future reset would use."""
        with self._lock:
            self._modes.clear()

    def _prune(self) -> None:
        # Insertion order, so the least recently armed session goes first. Caller holds the lock.
        while len(self._modes) >= MAX_SESSIONS:
            self._modes.pop(next(iter(self._modes)), None)

    # -- the two seams --------------------------------------------------------

    def directive(self, session_id: str) -> Optional[str]:
        """The block ``pre_llm_call`` returns, or None when dispatch governs."""
        return MULTIAGENTS_DIRECTIVE if self.mode(session_id) == MODE_MULTIAGENTS else None

    def handle(self, raw_args: Any, session_id: str = "") -> str:
        """Run one ``/adviser`` invocation; returns the line the host prints."""
        tokens = str(raw_args or "").strip().lower().split()
        name = tokens[0] if tokens else "status"
        if name in MULTIAGENTS_NAMES:
            self.arm(session_id)
            return _ARMED
        if name in DISPATCH_NAMES:
            self.clear(session_id)
            return _RELEASED
        if name == "status":
            return _ARMED_STATUS if self.mode(session_id) == MODE_MULTIAGENTS else _DISPATCH_STATUS
        return f"adviser: unknown mode {name!r}. {USAGE}"


MODES = ModeSwitch()


def command_handler(resolve_provider: Callable[[], Any]) -> Callable[..., str]:
    """Build the handler ``register_command`` receives.

    ``invoke_plugin_command`` passes the context keywords a handler declares, so ``session_id``
    (and its gateway twin ``session_key``) arrive by name. The provider's bound session is the
    fallback for a host that dispatches a command with neither. The switch is read off the *live*
    provider, not this module: the plugin directory is imported once per surface, and a mode armed
    through one import must be the one the ``pre_llm_call`` hook of the other reads.
    """
    def handler(raw_args: str = "", session_id: str = "", session_key: str = "",
                **kwargs: Any) -> str:
        provider = resolve_provider()
        switch = getattr(provider, "_modes", None) or MODES
        session = session_id or session_key or str(getattr(provider, "_session_id", "") or "")
        return switch.handle(raw_args, session_id=session)

    return handler
