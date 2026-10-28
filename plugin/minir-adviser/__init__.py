"""``/adviser``, registered where plugin discovery can see it.

The mode switch belongs to the ``minir`` memory provider: the mode is per session and the
``pre_llm_call`` hook that injects the directive reads the provider's switch. But the host refuses
an exclusive plugin at discovery (``exclusive plugin — activate via <category>.provider config``),
so ``minir`` imports only when the provider loads — at the first turn. A command registered there
does not exist between process start and that turn, and the CLI answers ``unknown command`` while
the code is correct.

This directory is an ordinary plugin: discovery loads it at boot, so the command resolves from the
first keystroke. It keeps no mode state; it forwards to the provider through the named seams
``minir`` publishes (two plugin directories are separate packages, so the names and signatures are
the contract between them).

Those seams live in a module that enters ``sys.modules`` only when the provider loads, which is the
next turn. The cold path -- the first keystroke of a process, the one this plugin exists for --
therefore resolves the provider package itself through the host's loader
(``plugins.memory.import_provider_module``) before giving up: that import uses the module name the
memory loader will reuse, so the package whose seams answered is the package the provider
initializes in.

Arming before a provider exists still has to work the way the user asked: the command queues one
ready-check turn through ``inject_message``; that turn builds the agent (initializing the provider),
the ``pre_llm_call`` hook below applies the queued request to the now-live switch, and the directive
``minir`` returns for the same turn makes the model load ``minir:hermes-lead`` and stop, ready for
the next message.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Seams ``minir`` publishes. ``command`` answers with the line to print (None while no provider
#: is initialized), ``verb`` classifies the arguments without touching any state, and ``kickoff``
#: is the ready-check request injected when arming.
SEAM_COMMAND = "_minir_adviser_command"
SEAM_VERB = "_minir_adviser_verb"
SEAM_KICKOFF = "_minir_adviser_kickoff"

#: The memory provider these seams belong to; the host resolves it by this name.
PROVIDER_NAME = "minir"

#: Session -> raw arguments waiting for a provider. Insertion-ordered, oldest dropped first.
PENDING: dict[str, str] = {}
MAX_PENDING = 64

#: The provider package could not be imported, so nothing here can even classify the arguments.
NOT_READY = (
    "adviser: the minir memory provider package could not be imported in this process, so there is "
    "no session mode to switch. Check that the minir plugin is installed for this profile and that "
    "memory.provider is minir."
)
#: Installed, but another backend owns ``memory.provider``: arming would queue a turn that can
#: never apply, so name the provider in the way instead of printing install advice.
NOT_MINIR = (
    "adviser: memory.provider is {name!r}, not minir, so this session has no minir mode to switch. "
    "/adviser belongs to the minir memory provider."
)
PENDING_QUEUED = (
    "adviser: multagents queued for this session. A ready-check turn is starting now: it carries "
    "the directive, loads minir:hermes-lead and stops, ready for your request."
)
PENDING_NO_TURN = (
    "adviser: multagents queued for this session, but no ready-check turn could be queued (no "
    "injector on this surface). It arms on your next message."
)
CLEARED_PENDING = (
    "adviser: dispatch requested; the queued arm was dropped. No provider was loaded, so no mode "
    "had been set."
)
STATUS_PENDING = (
    "adviser: multagents queued for this session; it arms on the ready-check turn, which loads the "
    "skill. Nothing has been switched yet."
)
STATUS_NOT_LOADED = (
    "adviser: mode=dispatch — the minir memory provider is not loaded in this process yet, so no "
    "session mode is set."
)
USAGE = "adviser: usage — /adviser multagents | dispatch | status"


_PACKAGE: dict[str, Any] = {"resolved": False, "module": None}


def _provider_package() -> Optional[Any]:
    """The installed ``minir`` package, imported on first need.

    Not at registration: this plugin loads at boot in every process, and a session that never types
    ``/adviser`` should not pay the provider's import. The host resolves the package under the module
    name the memory loader owns, so the package whose seams answered here is the one the provider
    later initializes in. A failed resolution is remembered: neither cause (not installed, import
    error) clears without a restart.
    """
    if _PACKAGE["resolved"]:
        return _PACKAGE["module"]
    _PACKAGE["resolved"] = True
    try:
        from plugins.memory import import_provider_module
    except Exception:
        logger.debug("minir-adviser: host has no memory-provider resolver", exc_info=True)
        return None
    try:
        _PACKAGE["module"] = import_provider_module(PROVIDER_NAME)
    except Exception:
        logger.warning("minir-adviser: importing the %s provider package failed",
                       PROVIDER_NAME, exc_info=True)
    return _PACKAGE["module"]


def _candidates(name: str) -> list[Callable[..., Any]]:
    """Every callable publishing ``name``: the loaded copies, then the installed package.

    More than one loaded copy is normal: the memory-provider and context-engine loaders each import
    ``minir`` as their own package, and only the copy whose provider was initialized answers. The
    package resolved from disk comes last; before the first turn it is the only copy that exists.
    """
    found: list[Callable[..., Any]] = []
    for module in list(sys.modules.values()):
        fn = getattr(module, name, None)
        if callable(fn) and fn not in found:
            found.append(fn)
    if found:
        return found
    package = _provider_package()
    fn = getattr(package, name, None) if package is not None else None
    return [fn] if callable(fn) else []


def _find(name: str) -> Optional[Callable[..., Any]]:
    found = _candidates(name)
    return found[0] if found else None


def run_adviser(raw_args: str, *, session_id: str = "",
                deliver: Optional[Callable[[str], bool]] = None) -> Optional[str]:
    """Forward one invocation; None when no copy of ``minir`` has a provider yet."""
    for seam in _candidates(SEAM_COMMAND):
        try:
            answer = seam(raw_args, session_id=session_id, deliver=deliver)
        except Exception:  # a failing seam must not cost the command surface
            logger.warning("minir adviser seam failed", exc_info=True)
            continue
        if answer is not None:
            return answer
    return None


def _verb(raw_args: str) -> str:
    fn = _find(SEAM_VERB)
    if fn is None:
        return ""
    try:
        return str(fn(raw_args) or "")
    except Exception:
        logger.warning("minir adviser verb seam failed", exc_info=True)
        return ""


def _kickoff() -> Optional[str]:
    fn = _find(SEAM_KICKOFF)
    if fn is None:
        return None
    try:
        return str(fn() or "") or None
    except Exception:
        logger.warning("minir adviser kickoff seam failed", exc_info=True)
        return None


def _configured_provider() -> str:
    """``memory.provider`` from config, or "" when the host cannot be asked."""
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    except Exception:
        return ""
    memory = config.get("memory") if isinstance(config, dict) else None
    if not isinstance(memory, dict):
        return ""
    return str(memory.get("provider") or "").strip()


def _remember(session: str, raw_args: str) -> None:
    while len(PENDING) >= MAX_PENDING:
        PENDING.pop(next(iter(PENDING)), None)
    PENDING[session] = raw_args


def _pending_key(session: str) -> Optional[str]:
    """The queued request for *session*, allowing for the two ids one session has.

    A command typed before the first turn only knows the host's session key
    (``agent:main:local:dm:local-<id>`` on the gateway and in the TUI); ``pre_llm_call`` hands the
    agent's own ``session_id`` (``local-<id>``). Exact match first, else the queued key the host
    composed around the hook's id -- the tail after a colon separator, so a bare substring can
    never stand in for a different session.
    """
    if session in PENDING:
        return session
    if not session:
        return None
    suffix = f":{session}"
    matches = [key for key in PENDING if key.endswith(suffix)]
    return max(matches, key=len) if matches else None


def _defer(session: str, raw_args: str,
           deliver: Optional[Callable[[str], bool]]) -> str:
    """Queue the request and start the turn that can honour it."""
    _remember(session, raw_args)
    kickoff = _kickoff()
    queued = bool(kickoff and deliver and deliver(kickoff))
    logger.info("minir-adviser: queued %r under session %r (ready-check turn queued=%s)",
                raw_args, session, queued)
    return PENDING_QUEUED if queued else PENDING_NO_TURN


def flush_pending(session_id: str = "", session_key: str = "", **kwargs: Any) -> None:
    """``pre_llm_call`` hook: apply a queued arm once the provider exists.

    Runs on the turn the command injected. That turn's agent build initializes the provider, so the
    seam answers here even though it answered None when the command ran. Returning None keeps this
    hook out of the prompt: the directive is ``minir``'s, and it reads the switch this hook armed.
    """
    session = session_id or session_key
    key = _pending_key(session)
    if key is None:
        return None
    raw_args = PENDING[key]
    answer = run_adviser(raw_args, session_id=session)
    if answer is None:
        return None  # still no provider; keep it queued for the next turn
    PENDING.pop(key, None)
    logger.info("minir-adviser: queued %r applied to the live switch", raw_args)
    return None


def register(ctx: Any) -> None:
    """Discovery entry point: one command and one flush hook, no mode state of its own."""
    register_command = getattr(ctx, "register_command", None)
    if not callable(register_command):
        logger.warning("minir-adviser: host has no command surface; /adviser stays unavailable")
        return
    # ``inject_message`` is what turns arming into one ready-check turn instead of waiting for the
    # user's next message. A host without it still arms the mode; the printed line says so.
    inject = getattr(ctx, "inject_message", None)
    deliver = (lambda text: bool(inject(text))) if callable(inject) else None

    def command(raw_args: str = "", session_id: str = "", session_key: str = "",
                **kwargs: Any) -> str:
        session = session_id or session_key
        answer = run_adviser(raw_args, session_id=session, deliver=deliver)
        if answer is not None:
            queued = _pending_key(session)
            if queued is not None:  # a live switch answered; nothing is left queued
                PENDING.pop(queued, None)
            return answer
        verb = _verb(raw_args)
        if verb == "arm":
            return _defer(session, raw_args, deliver)
        if verb == "clear":
            queued = _pending_key(session)
            if queued is not None:
                PENDING.pop(queued, None)
            return CLEARED_PENDING
        if verb == "status":
            return STATUS_PENDING if _pending_key(session) is not None else STATUS_NOT_LOADED
        if verb == "unknown":
            return USAGE
        configured = _configured_provider()
        if configured and configured != PROVIDER_NAME:
            return NOT_MINIR.format(name=configured)
        return NOT_READY

    register_command(
        "adviser",
        command,
        description="Switch this session between hermes-dispatch and hermes-lead mode.",
        args_hint="multagents | dispatch | status",
    )

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_llm_call", flush_pending)
    else:
        logger.warning("minir-adviser: host has no hook surface; a queued arm only lands on the "
                       "next /adviser call")
