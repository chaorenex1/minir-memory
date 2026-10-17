"""One-shot in-process ``AIAgent`` runs — the Feishu comment-agent shape, extracted.

The caller supplies only what it can know: the **user message**, an optional **context**
block, an optional **system message**, and the overrides it owns — **model**, **provider**,
**toolsets**. Everything else is resolved here, the way the gateway resolves a message before it
builds an agent (``plugins/platforms/feishu/feishu_comment.py::_resolve_model_and_runtime``):

* model          — ``_resolve_gateway_model(_load_gateway_config())``, with the provider's
                   default model as the fallback;
* credentials    — ``_resolve_runtime_agent_kwargs()``: ``api_key``, ``base_url``,
                   ``api_mode``, ``credential_pool`` (plus the gateway's fallback chain);
* reasoning      — ``resolve_reasoning_config(config, model)`` for the *final* model, the one
                   chokepoint that makes ``reasoning_effort`` reach the adapter;
* the agent knobs — ``max_iterations``, quiet mode, no context files, no memory, no background
                   review fork, and the operator's soul identity loaded.

A pinned ``provider`` re-resolves credentials for that provider instead of borrowing the main
route's key, so an override is a *route*, not just a string on the request.

Two entry points, one implementation:

* :func:`run` — synchronous, for tool handlers, workers and CLI paths.
* :func:`run_async` — awaits the same run off the event loop. ``AIAgent``'s loop is synchronous
  (no ``async def`` in ``run_agent``/``agent.conversation_loop``), so this is an honest thread
  offload rather than a coroutine wrapper: blocking HTTP and tool subprocesses stay off the
  loop, and ``close()`` runs in the very thread that built the agent.

Import order is not cosmetic. ``run_agent`` bootstraps the managed environment and *replaces*
``sys.path`` (it re-execs into ``hermes/tools/python-<ver>``); anything imported from the old
path first — ``hermes_cli.config`` and its ``hermes_yaml``/``ruamel`` chain — is orphaned and
fails on the next lazy import. ``run_agent`` is therefore imported first, inside the runner.

Why a private ``AIAgent`` instead of ``ctx.subagent_lifecycle``: the caller wants a *peer* agent
(advice, a summary, a review), not delegated work — it must not appear in the delegation tree.
The price is the private import ``hermes plugins validate`` reports as a catalog-rule-9 warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

#: Tool-call budget for one peer run — the Feishu comment agent's number.
MAX_ITERATIONS = 15

#: Constructor kwargs that come from the resolved runtime rather than from the caller — the same
#: set the gateway forwards from ``_resolve_runtime_agent_kwargs()`` (``_runtime_agent_kwargs``),
#: minus ``model`` (resolved separately) and the internal ``_fallback_notice``.
_RUNTIME_KEYS = ("api_key", "base_url", "api_mode", "credential_pool", "request_overrides",
                 "requested_provider", "capabilities", "command", "args")


@dataclass(frozen=True)
class AgentRun:
    """Result of one in-process run: the answer plus how it was produced."""

    answer: str
    api_calls: int
    model: str
    provider: str
    toolsets: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"answer": self.answer, "api_calls": self.api_calls, "model": self.model,
                "provider": self.provider, "toolsets": list(self.toolsets)}


def _resolve_model_and_runtime() -> tuple[str, dict[str, Any]]:
    """Model + provider credentials, resolved the way the gateway resolves a message.

    Mirrors the Feishu comment agent's helper of the same name: the gateway config decides the
    model, ``_resolve_runtime_agent_kwargs`` supplies the credentials (primary -> fallback
    chain), and a provider with no configured model falls back to its own default.
    """
    from gateway.run import _load_gateway_config, _resolve_gateway_model, _resolve_runtime_agent_kwargs

    model = _resolve_gateway_model(_load_gateway_config())
    runtime = dict(_resolve_runtime_agent_kwargs() or {})
    try:
        if not model and runtime.get("provider"):  # fall back to the provider's default model
            from hermes_cli.models import get_default_model_for_provider

            model = get_default_model_for_provider(str(runtime["provider"]))
    except Exception:  # health: allow BLE001 -- a default-model lookup must not break the run
        logger.debug("agent_loop: default model lookup failed", exc_info=True)
    return model, runtime


def _agent_kwargs(provider: str, model: str, runtime: dict[str, Any]) -> dict[str, Any]:
    """Credential/provider kwargs for the final route, plus its reasoning config.

    The gateway's runtime is the default route. A pinned provider that differs from it is
    resolved *as a provider* (``resolve_runtime_provider``), otherwise the run would send
    another provider's model with the main route's key.
    """
    from gateway.run import _load_gateway_config
    from hermes_constants import resolve_reasoning_config

    resolved = str(runtime.get("provider") or "")
    final_provider = provider or resolved
    if provider and provider not in ("auto", "main") and provider != resolved:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        pinned = resolve_runtime_provider(requested=provider, target_model=model or None) or {}
        knobs = {key: pinned.get(key) for key in _RUNTIME_KEYS}
        final_provider = str(pinned.get("provider") or provider)
    else:
        knobs = {key: runtime.get(key) for key in _RUNTIME_KEYS}
    knobs["provider"] = final_provider
    knobs["reasoning_config"] = resolve_reasoning_config(_load_gateway_config(), model)
    return knobs


def _prompt(message: str, context: str = "") -> str:
    return message if not context else f"{message}\n\n--- context ---\n{context}"


def run(message: str, *, context: str = "", model: str = "", provider: str = "",
        toolsets: Optional[Sequence[str]] = None, system_message: str = "") -> AgentRun:
    """Run one agent loop on *message* and return its answer. Never leaves an agent open."""
    from run_agent import AIAgent  # first: it replaces sys.path (managed environment bootstrap)

    resolved_model, runtime = _resolve_model_and_runtime()
    final_model = str(model or "").strip() or str(resolved_model or "").strip()
    final_provider = str(provider or "").strip() or str(runtime.get("provider") or "")
    tools = [str(tool) for tool in (toolsets or [])]

    agent = AIAgent(
        model=final_model,
        **_agent_kwargs(final_provider, final_model, runtime),
        enabled_toolsets=tools or None,
        max_iterations=MAX_ITERATIONS,
        # A peer run brings its own instructions and evidence: no project context files, no host
        # memory, no background review fork. ``load_soul_identity`` stays on so the run speaks in
        # the operator's configured voice.
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        load_soul_identity=True,
    )
    try:
        result = agent.run_conversation(_prompt(message, context),
                                        system_message=str(system_message or "").strip() or None)
        return AgentRun(
            answer=(result.get("final_response") or "").strip(),
            api_calls=result.get("api_calls", 0),
            model=final_model,
            provider=final_provider,
            toolsets=tuple(tools),
        )
    finally:
        # One agent per run in a long-lived host: close() releases tool subprocesses and HTTP
        # clients, otherwise every run leaks them (same reason as the Feishu comment agent).
        with contextlib.suppress(Exception):
            agent.close()


async def run_async(message: str, *, context: str = "", model: str = "", provider: str = "",
                    toolsets: Optional[Sequence[str]] = None,
                    system_message: str = "") -> AgentRun:
    """Await :func:`run` off the event loop (see the module docstring for why a thread)."""
    return await asyncio.to_thread(run, message, context=context, model=model,
                                   provider=provider, toolsets=toolsets,
                                   system_message=system_message)
