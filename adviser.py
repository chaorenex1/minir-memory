"""adviser — a second-opinion adviser that runs a real agent loop, hosted by this plugin.

Two surfaces ride one route (``auxiliary.adviser``):

* ``ctx.register_auxiliary_task("adviser")`` — the ``auxiliary.adviser`` config block, its
  model-picker entry and the ``AUXILIARY_ADVISER_*`` env bridge are owned by this plugin.
* the ``adviser_consult`` tool — an in-process agent loop the main agent can consult.

The loop itself is :mod:`agent_loop` (one implementation, sync + async); this module owns the
route's configuration, the prompt, the tool schema and the two handlers:

    sync   adviser.handle({...})              / adviser.run(...)
    async  await adviser.handle_async({...})  / await adviser.run_async(...)

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
from typing import Any

from . import agent_loop

logger = logging.getLogger(__name__)

TASK_KEY = "adviser"          # must be alnum/underscore; must not collide with a built-in aux task
TOOL_NAME = "adviser_consult"
TOOLSET = "adviser"

# Plugin-owned keys layered under the user's ``auxiliary.adviser`` block. ``provider``/``model``
# pass straight through to ``agent_loop``; everything else (credentials, reasoning, budget) is
# resolved by the loop itself.
_CFG_DEFAULTS: dict[str, Any] = {"toolsets": []}

# The adviser's framing is the run's system message, not part of the user message: the question
# stays exactly what the caller asked, and the instructions stay instructions.
_ADVISER_BRIEF = (
    "You are a senior adviser giving a second opinion. Lead with the recommendation, then the "
    "reasoning, the main risks, and what you would verify. Disagree when the evidence says so; "
    "never agree just to be agreeable. Be concise."
)

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Ask an independent adviser (separate agent loop on the auxiliary.adviser model route) "
        "for a second opinion on a decision, design, or diagnosis. Returns the adviser's answer "
        "plus how many model calls it took."
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
            "toolsets": cfg.get("toolsets")}


def run(question: str, context: str = "", toolsets: Any = None) -> dict[str, Any]:
    """Synchronous consult — one in-process agent loop, returned as a plain dict."""
    route = _route(load_config())
    return agent_loop.run(
        question, context=context, model=route["model"], provider=route["provider"],
        toolsets=toolsets if toolsets is not None else route["toolsets"],
        system_message=_ADVISER_BRIEF,
    ).as_dict()


async def run_async(question: str, context: str = "", toolsets: Any = None) -> dict[str, Any]:
    """Same consult, awaited off the event loop."""
    route = _route(load_config())
    result = await agent_loop.run_async(
        question, context=context, model=route["model"], provider=route["provider"],
        toolsets=toolsets if toolsets is not None else route["toolsets"],
        system_message=_ADVISER_BRIEF,
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


def handle(args: dict[str, Any], **_kwargs: Any) -> str:
    """Synchronous tool handler (direct callers, tests, non-async hosts)."""
    args = args or {}
    question = str(args.get("question") or "").strip()
    if not question:
        return _invalid()
    try:
        data = run(question, context=str(args.get("context") or ""), toolsets=args.get("toolsets"))
    except Exception as exc:  # BLE001 -- reported as JSON, never raised into the loop
        return _failure(exc)
    return _payload(data)


async def handle_async(args: dict[str, Any], **_kwargs: Any) -> str:
    """Async tool handler — the one registered with the host (``is_async=True``)."""
    args = args or {}
    question = str(args.get("question") or "").strip()
    if not question:
        return _invalid()
    try:
        data = await run_async(question, context=str(args.get("context") or ""),
                               toolsets=args.get("toolsets"))
    except Exception as exc:  # BLE001 -- reported as JSON, never raised into the loop
        return _failure(exc)
    return _payload(data)


def register(ctx) -> None:
    """Register the auxiliary route and the tool on the host's plugin context."""
    # Config block auxiliary.adviser (+ model-picker entry + AUXILIARY_ADVISER_* env bridge).
    # Plugin-owned defaults ride along because build_auxiliary_task_entry keeps unknown keys.
    ctx.register_auxiliary_task(
        TASK_KEY,
        display_name="Adviser",
        description="Second-opinion adviser: model route for the memory plugin's in-process agent loop.",
        defaults={"toolsets": []},
    )
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=handle_async,
        is_async=True,
        description="Ask an independent adviser (separate agent loop) for a second opinion.",
        emoji="🧭",
    )
