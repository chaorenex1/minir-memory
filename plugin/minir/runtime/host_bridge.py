"""Optional bridge to the host's plugin control surface.

The host patch series under ``host-patches/`` adds ``hermes_cli.plugin_runtime``:
a session-scoped handle that lets a plugin steer the live turn, interrupt the
in-flight model request, and leave one continuation for the stop gate. The adviser
is the only caller here.

Every function degrades to a no-op when the host predates the patch: the ledger
channels (``llm_request``, the turn-boundary aside, the gateway notify) remain the
delivery of record. Nothing in this module imports the host at module import time,
so a stock Hermes loads the plugin unchanged.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MODULE: Any = None
_CHECKED = False
_LOGGED_UNAVAILABLE = False


def _module() -> Any:
    """The host module when it carries the capability, else ``None`` (checked once)."""
    global _MODULE, _CHECKED
    if _CHECKED:
        return _MODULE
    with _LOCK:
        if _CHECKED:
            return _MODULE
        _CHECKED = True
        try:
            from hermes_cli import plugin_runtime as mod  # type: ignore

            version = str(getattr(mod, "CAPABILITY_VERSION", "") or "")
            _MODULE = mod if version else None
        except Exception:
            _MODULE = None
        return _MODULE


def reset_cache() -> None:
    """Forget the cached probe (tests, or a host patched during the process's life)."""
    global _MODULE, _CHECKED, _LOGGED_UNAVAILABLE
    with _LOCK:
        _MODULE = None
        _CHECKED = False
        _LOGGED_UNAVAILABLE = False


def capability() -> str:
    mod = _module()
    return str(getattr(mod, "CAPABILITY_VERSION", "") or "") if mod is not None else ""


def available() -> bool:
    global _LOGGED_UNAVAILABLE
    mod = _module()
    if mod is None and not _LOGGED_UNAVAILABLE:
        _LOGGED_UNAVAILABLE = True
        logger.info(
            "minir host bridge unavailable: install the series in host-patches/ to enable "
            "mid-turn steer and turn continuation"
        )
    return mod is not None


def live(session_id: str) -> bool:
    mod = _module()
    if mod is None or not session_id:
        return False
    try:
        return bool(mod.live_turn(str(session_id)))
    except Exception:
        logger.debug("host bridge live_turn failed", exc_info=True)
        return False


def live_state(session_id: str) -> dict[str, Any]:
    """The host's four routing facts, or ``{}`` on a host without P-0023.

    ``{streaming, aborting, tool_batch, terminal_idle}``: an empty dict means the host cannot
    report them, and every caller must fall back to its own inference rather than read a missing
    key as false. A bound-but-dead session reports all four false with ``live: false``.
    """
    mod = _module()
    if mod is None or not session_id:
        return {}
    reader = getattr(mod, "live_state", None)
    if not callable(reader):
        return {}
    try:
        state = reader(str(session_id))
        return dict(state) if isinstance(state, dict) else {}
    except Exception:
        logger.debug("host bridge live_state failed", exc_info=True)
        return {}


def turn_state(session_id: str) -> dict[str, Any]:
    mod = _module()
    if mod is None or not session_id:
        return {}
    try:
        return dict(mod.turn_state(str(session_id)) or {})
    except Exception:
        logger.debug("host bridge turn_state failed", exc_info=True)
        return {}


def steer(session_id: str, text: str) -> dict[str, Any]:
    """Queue advice for the live turn. ``ok`` is the host's acceptance."""
    mod = _module()
    if mod is None or not session_id or not str(text or "").strip():
        return {"ok": False, "mode": "", "reason": "bridge_unavailable"}
    try:
        return dict(mod.steer(str(session_id), str(text)) or {})
    except Exception as exc:
        logger.debug("host bridge steer failed: %s", exc)
        return {"ok": False, "mode": "", "reason": "bridge_error"}


def interrupt(session_id: str, text: str) -> dict[str, Any]:
    """Queue advice and stop the current tool batch (host interrupt)."""
    mod = _module()
    if mod is None or not session_id or not str(text or "").strip():
        return {"ok": False, "mode": "", "reason": "bridge_unavailable"}
    try:
        return dict(mod.interrupt(str(session_id), str(text), source="minir-adviser") or {})
    except Exception as exc:
        logger.debug("host bridge interrupt failed: %s", exc)
        return {"ok": False, "mode": "", "reason": "bridge_error"}


def queue_post_turn(session_id: str, text: str) -> dict[str, Any]:
    """Ask the surface to run this text as the next turn (leftover pending_steer)."""
    mod = _module()
    if mod is None or not session_id or not str(text or "").strip():
        return {"ok": False, "reason": "bridge_unavailable"}
    try:
        return dict(mod.queue_post_turn(
            str(session_id), str(text), source="minir-adviser", kind="adviser") or {})
    except Exception as exc:
        logger.debug("host bridge queue_post_turn failed: %s", exc)
        return {"ok": False, "reason": "bridge_error"}


def preserve_handle(session_id: str) -> Any:
    """A callable that appends one preserved adviser card at the turn end, or ``None``.

    Must be captured while the turn is live (``post_llm_call``); the host unbinds the agent
    when the turn ends and cannot hand out the writer afterwards.
    """
    mod = _module()
    if mod is None or not session_id:
        return None
    maker = getattr(mod, "preserve_handle", None)
    if not callable(maker):
        return None
    try:
        return maker(str(session_id))
    except Exception:
        logger.debug("host bridge preserve_handle failed", exc_info=True)
        return None


def flags(session_id: str) -> dict[str, Any]:
    mod = _module()
    if mod is None or not session_id:
        return {}
    try:
        return dict(mod.get_flags(str(session_id)) or {})
    except Exception:
        return {}


def continue_turn(session_id: str, text: str, *, source: str = "minir-adviser",
                  token: str = "") -> dict[str, Any]:
    """Ask the stop gate to keep the turn open for this text."""
    mod = _module()
    if mod is None or not session_id or not str(text or "").strip():
        return {"ok": False, "token": "", "reason": "bridge_unavailable"}
    try:
        return dict(mod.request_continuation(
            str(session_id), str(text), source=str(source or ""), token=str(token or ""),
        ) or {})
    except Exception as exc:
        logger.debug("host bridge request_continuation failed: %s", exc)
        return {"ok": False, "token": "", "reason": "bridge_error"}


def continuation_status(session_id: str, token: str) -> str:
    mod = _module()
    if mod is None or not session_id or not token:
        return "unknown"
    try:
        return str(mod.continuation_status(str(session_id), str(token)) or "unknown")
    except Exception:
        return "unknown"


def max_continuations() -> int:
    mod = _module()
    if mod is None:
        return 0
    try:
        return int(mod.max_continuations())
    except Exception:
        return 0


__all__ = [
    "available",
    "capability",
    "continue_turn",
    "continuation_status",
    "flags",
    "interrupt",
    "live",
    "live_state",
    "max_continuations",
    "preserve_handle",
    "queue_post_turn",
    "reset_cache",
    "steer",
    "turn_state",
]
