"""adviser — a second-opinion adviser that runs a real agent loop, hosted by this plugin.

Two surfaces ride one route (``auxiliary.adviser``):

* ``ctx.register_auxiliary_task("adviser")`` — the ``auxiliary.adviser`` config block, its
  model-picker entry and the ``AUXILIARY_ADVISER_*`` env bridge are owned by this plugin.
* the ``adviser_consult`` tool — the main adviser's synchronous entry point; with a bound
  provider it delegates to ``AdviserMode.consult``/``consult_async`` and shares ``ADVISER_CORE``;
* the ``adviser_session_search`` tool — read-only, multi-shape recall over the host's own
  ``state.db`` (see :mod:`session_search`), in the same ``adviser`` toolset.

The loop itself is :mod:`agent_loop` (one implementation, sync + async); this module owns the
route's configuration, the tool schema, and the delegation. ``AdviserMode`` owns the consult and
review prompts and the session context:

    sync   adviser.handle({...}, provider=...)              / adviser.run(...)
    async  await adviser.handle_async({...}, provider=...)  / await adviser.run_async(...)

The registered tool is the *async* handler with ``is_async=True``: the host bridges it for sync
callers (``tools.registry.dispatch`` -> ``_run_async``) and awaits it on a gateway loop, so one
consult does not stall the other conversations in the process. Keep that pairing — an async
handler registered with ``is_async=False`` would hand a coroutine to the result normaliser, and
a sync handler marked ``is_async=True`` would hand it a string to await.

Everything is lazy: importing this module never touches the host's config, credentials, or
model client, so the memory provider keeps loading when the adviser is never used.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import agent_loop, file_guard, session_search, tools
from .adviser_prompts import consult_prompt

logger = logging.getLogger(__name__)

TASK_KEY = "adviser"          # must be alnum/underscore; must not collide with a built-in aux task
TOOL_NAME = "adviser_consult"
TOOLSET = "adviser"

# Plugin-owned keys layered under the user's ``auxiliary.adviser`` block. ``provider``/``model``
# pass straight through to ``agent_loop``; everything else (credentials, reasoning, budget) is
# resolved by the loop itself.
_CFG_DEFAULTS: dict[str, Any] = {"toolsets": []}

# The adviser's framing is the run's system message, not part of the user message: the question
# stays exactly what the caller asked, and the instructions stay instructions. One shared core
# serves both entry points; this is the CONSULT half of it.
_ADVISER_BRIEF = consult_prompt()

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Ask an independent adviser (separate agent loop on the auxiliary.adviser model route) "
        "for a second opinion on a decision, design, or diagnosis. It can read files under the "
        "project root and your memory store. Returns the adviser's answer plus how many model "
        "calls it took."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question or decision to get advice on."},
            "context": {"type": "string", "description": "Evidence the adviser needs: code, errors, constraints."},
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Override the toolsets the adviser may use (default: auxiliary.adviser.toolsets).",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}


def load_config() -> dict[str, Any]:
    """Effective ``auxiliary.adviser`` config: plugin defaults < user config."""
    try:
        from hermes_cli.config import load_config_readonly

        aux = (load_config_readonly() or {}).get("auxiliary") or {}
        cfg = dict(aux.get(TASK_KEY) or {}) if isinstance(aux, dict) else {}
    except Exception:  # health: allow BLE001 -- a config read must never break the tool
        logger.debug("adviser: auxiliary config read failed", exc_info=True)
        cfg = {}
    return {**_CFG_DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}


def _route(cfg: dict[str, Any]) -> dict[str, Any]:
    """Only the knobs the adviser owns; credentials/budget are resolved inside the loop."""
    return {"model": str(cfg.get("model") or ""), "provider": str(cfg.get("provider") or ""),
            "api_mode": str(cfg.get("api_mode") or ""), "toolsets": cfg.get("toolsets")}


def project_dir(cfg: dict[str, Any] | None = None) -> Optional[str]:
    """Read root an adviser run may use outside HERMES_HOME.

    ``None`` means "use the default": every peer run already inherits the cwd's git root.
    ``auxiliary.adviser.project_dir`` overrides it; an unusable path returns ``""`` so the run
    gets no project read instead of silently falling back to another directory.
    """
    cfg = load_config() if cfg is None else cfg
    explicit = str(cfg.get("project_dir") or "").strip()
    if not explicit:
        return None
    root = file_guard.project_root(explicit)
    if root is None:
        logger.warning("adviser: project_dir is not a usable directory: %s", explicit)
        return ""
    return str(root)


def run(question: str, context: str = "", toolsets: Any = None) -> dict[str, Any]:
    """Synchronous consult — one in-process agent loop, returned as a plain dict."""
    cfg = load_config()
    route = _route(cfg)
    return agent_loop.run(
        question, context=context, model=route["model"], provider=route["provider"],
        api_mode=route["api_mode"],
        toolsets=tools.agent_toolsets(
            toolsets if toolsets is not None else route["toolsets"]),
        system_message=_ADVISER_BRIEF,
        project_dir=project_dir(cfg),
    ).as_dict()


async def run_async(question: str, context: str = "", toolsets: Any = None) -> dict[str, Any]:
    """Same consult, awaited off the event loop."""
    cfg = load_config()
    route = _route(cfg)
    result = await agent_loop.run_async(
        question, context=context, model=route["model"], provider=route["provider"],
        api_mode=route["api_mode"],
        toolsets=tools.agent_toolsets(
            toolsets if toolsets is not None else route["toolsets"]),
        system_message=_ADVISER_BRIEF,
        project_dir=project_dir(cfg),
    )
    return result.as_dict()


def _invalid() -> str:
    return json.dumps({"success": False, "error": {"type": "invalid_args",
                                                   "message": "question is required"}})


def _payload(data: dict[str, Any]) -> str:
    """Tool result envelope — a JSON string, never a dict."""
    if not data["answer"]:
        return json.dumps({"success": False, "error": {"type": "empty_answer",
                                                       "message": "the adviser returned no text"},
                           "data": data})
    return json.dumps({"success": True, "data": data})


def _failure(exc: Exception) -> str:
    # Surface the failure to the caller instead of raising into the model's tool loop.
    logger.exception("adviser: consult failed")
    return json.dumps({"success": False, "error": {"type": "adviser_failed",
                                                   "message": str(exc)[:500]}})


def handle(args: dict[str, Any], *, provider: Any = None, **_kwargs: Any) -> str:
    """Synchronous tool handler (direct callers, tests, non-async hosts)."""
    args = args or {}
    question = str(args.get("question") or "").strip()
    if not question:
        return _invalid()
    try:
        context = str(args.get("context") or "")
        toolsets = args.get("toolsets")
        mode = getattr(provider, "_adviser_mode", None) if provider is not None else None
        data = (mode.consult(question, context=context, toolsets=toolsets)
                if mode is not None else run(question, context=context, toolsets=toolsets))
    except Exception as exc:  # BLE001 -- reported as JSON, never raised into the loop
        return _failure(exc)
    return _payload(data)


async def handle_async(args: dict[str, Any], *, provider: Any = None, **_kwargs: Any) -> str:
    """Async tool handler — the one registered with the host (``is_async=True``)."""
    args = args or {}
    question = str(args.get("question") or "").strip()
    if not question:
        return _invalid()
    try:
        context = str(args.get("context") or "")
        toolsets = args.get("toolsets")
        mode = getattr(provider, "_adviser_mode", None) if provider is not None else None
        data = (await mode.consult_async(question, context=context, toolsets=toolsets)
                if mode is not None
                else await run_async(question, context=context, toolsets=toolsets))
    except Exception as exc:  # BLE001 -- reported as JSON, never raised into the loop
        return _failure(exc)
    return _payload(data)


def register(ctx, provider: Any = None) -> None:
    """Register the auxiliary route and the tool on the host's plugin context."""
    # Config block auxiliary.adviser (+ model-picker entry + AUXILIARY_ADVISER_* env bridge).
    # Plugin-owned defaults ride along because build_auxiliary_task_entry keeps unknown keys.
    ctx.register_auxiliary_task(
        TASK_KEY,
        display_name="Adviser",
        description="Second-opinion adviser: model route for the memory plugin's in-process agent loop.",
        defaults={"toolsets": []},
    )
    async def _consult_handler(args: dict[str, Any], **kwargs: Any) -> str:
        kwargs.pop("provider", None)  # the host may pass its own provider kwarg
        return await handle_async(args, provider=provider, **kwargs)

    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=_consult_handler,
        is_async=True,
        description=("Ask the main adviser — the same adviser that reviews your turns — for a "
                     "second opinion on a decision, design, or diagnosis; it can read files "
                     "under the project root."),
        emoji="🧭",
    )
    # Read-only, stdlib-only store access. The handler is synchronous on purpose: one bounded
    # SQLite read with no model call, so the host needs no thread bridge.
    ctx.register_tool(
        name=session_search.TOOL_NAME,
        toolset=TOOLSET,
        schema=session_search.SCHEMA,
        handler=session_search.handle,
        is_async=False,
        description=("Search/read the host's durable session store (state.db): FTS5 message "
                     "search, session browse, transcript read, message window, stats."),
        emoji="🗂️",
    )
