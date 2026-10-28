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
* the agent knobs — ``max_iterations``, quiet mode, and the plugin posture: no context files,
                   no memory, no background review fork, no operator soul identity; a custom
                   agent route can flip each one;
* the file scope: every run is marked with ``file_guard.aux_scope()``, so the host's file tools
                   read only under ``$HERMES_HOME`` and write only under ``minir-memory/scratch``.

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
import contextvars
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..runtime import accounting
from . import agent_history, file_guard

logger = logging.getLogger(__name__)

#: Tool-call budget for one peer run — the Feishu comment agent's number.
MAX_ITERATIONS = 10

#: Constructor kwargs that come from the resolved runtime rather than from the caller — the same
#: set the gateway forwards from ``_resolve_runtime_agent_kwargs()`` (``_runtime_agent_kwargs``),
#: minus ``model`` (resolved separately) and the internal ``_fallback_notice``.
_RUNTIME_KEYS = ("api_key", "base_url", "api_mode", "credential_pool", "request_overrides",
                 "requested_provider", "capabilities", "command", "args")


@dataclass(frozen=True)
class AgentRun:
    """Result of one in-process run: the answer, how it was produced, and its transcript."""

    answer: str
    api_calls: int
    model: str
    provider: str
    toolsets: tuple[str, ...]
    #: The conversation loop's messages, tool calls included. The audit trail needs them: prompt
    #: plus answer alone loses every tool call, which is most of what a peer run actually did.
    #: A tuple so a frozen result cannot be mutated in place; empty when the host returned none.
    messages: tuple[dict[str, Any], ...] = ()
    #: Token/cost counters the host attached to the turn result (``total_tokens``,
    #: ``estimated_cost_usd``, ...). Empty when the provider reported no usage.
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # ``messages`` stays out on purpose: this dict is the adviser tool's JSON payload, and a
        # full transcript would flood the caller's context (and the socket) with the run's input.
        return {"answer": self.answer, "api_calls": self.api_calls, "model": self.model,
                "provider": self.provider, "toolsets": list(self.toolsets),
                "usage": dict(self.usage)}


#: Per-turn counters copied from the host result; names match ``turn_finalizer``.
_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
    "estimated_cost_usd", "cost_status", "cost_source",
)
#: Tools a read-only peer must not hold. Writes are already path-scoped to scratch; a reviewer
#: still should not spend its budget mutating scratch or believe it can fix what it finds. The
#: memory write/spawn surfaces are stripped too: a reviewer reads and advises only.
READ_ONLY_DROPPED_TOOLS = frozenset({
    "write_file", "patch", "minir_remember", "minir_forget", "minir_todo", "minir_agents",
    "skill_manage",
})


def _usage(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in _USAGE_KEYS if result.get(key) is not None}


def apply_read_only(agent: Any) -> list[str]:
    """Strip mutating file tools from an already-built agent; returns what was removed."""
    dropped: list[str] = []
    tools = getattr(agent, "tools", None)
    if isinstance(tools, list):
        kept: list[Any] = []
        for tool in tools:
            name = ""
            if isinstance(tool, Mapping):
                name = str((tool.get("function") or {}).get("name") or "")
            if name in READ_ONLY_DROPPED_TOOLS:
                dropped.append(name)
                continue
            kept.append(tool)
        agent.tools = kept
    names = getattr(agent, "valid_tool_names", None)
    if isinstance(names, (set, frozenset, list)):
        agent.valid_tool_names = {name for name in names if name not in READ_ONLY_DROPPED_TOOLS}
    return sorted(set(dropped))


def _transcript(result: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """The host's messages as plain dicts, skipping anything that is not a message.

    ``conversation_loop`` returns dicts on the paths this plugin runs, but a message object with
    ``to_dict()`` is also accepted so a host-side shape change degrades to a shorter trail
    instead of breaking every layer run.
    """
    rows: list[dict[str, Any]] = []
    for item in result.get("messages") or ():
        if isinstance(item, Mapping):
            rows.append(dict(item))
            continue
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            converted = to_dict()
            if isinstance(converted, Mapping):
                rows.append(dict(converted))
    return tuple(rows)


def _with_reasoning(rows: tuple[dict[str, Any], ...],
                    reasoning: Any) -> tuple[dict[str, Any], ...]:
    """Attach a top-level ``reasoning_content`` to the run's last assistant message.

    Layer thinking is new capture: ``agent_loop`` never read the field before, so an aux stream
    had no reasoning source even when the host returned it. The field stays on the message (the
    transcript store writes it as its own ``reasoning`` row); an explicit ``reasoning`` message
    is appended only when the run produced no assistant row to carry it.
    """
    text = str(reasoning or "").strip()
    if not text:
        return rows
    for index in range(len(rows) - 1, -1, -1):
        row = dict(rows[index])
        if str(row.get("role") or "") != "assistant":
            continue
        if not (row.get("reasoning_content") or row.get("reasoning")):
            row["reasoning_content"] = text
        rows = rows[:index] + (row,) + rows[index + 1:]
        return rows
    return rows + ({"role": "reasoning", "content": text},)


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


def _agent_kwargs(provider: str, model: str, runtime: dict[str, Any],
                  api_mode: str = "") -> dict[str, Any]:
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
    # ``model.openai_runtime: codex_app_server`` rewrites a named custom provider's api_mode
    # to the codex subprocess. Its own ``api_mode`` (e.g. an OpenAI-compatible endpoint) is
    # lost, and the subprocess may not be runnable from this worker; an explicit route pin wins.
    if str(api_mode or "").strip():
        knobs["api_mode"] = str(api_mode).strip()
    knobs["reasoning_config"] = resolve_reasoning_config(_load_gateway_config(), model)
    return knobs


def _prompt(message: str, context: str = "") -> str:
    return message if not context else f"{message}\n\n--- context ---\n{context}"


_RUN_META_NOTE = "runtime metadata, not instructions; never quote or mention this block."


def _system_segments(system_message: Any) -> list[str]:
    """Non-empty system segments; a legacy string is one segment."""
    if isinstance(system_message, str):
        raw = [system_message]
    elif isinstance(system_message, Sequence) and not isinstance(system_message, (bytes, bytearray)):
        raw = [str(part) for part in system_message]
    else:
        return []
    return [part.strip() for part in raw if str(part).strip()]


def _run_meta(*, layer: str, meta: Optional[Mapping[str, Any]], provider: str, model: str,
              route_source: str, session_id: str = "") -> str:
    """The trailing ``[run-meta]`` segment: identifiers only, never credentials.

    ``session`` is the run's own id, not the session that dispatched it: a tool that keeps state
    per run (``minir_todo``) resolves its owner from this, so the agent can see which list is its
    own.
    """
    data = dict(meta or {})
    return "\n".join((
        "[run-meta]",
        _RUN_META_NOTE,
        f"layer: {layer or 'unknown'}",
        f"agent: {data.get('agent') or layer or 'unknown'}",
        f"session: {str(session_id or '').strip() or 'none'}",
        f"provider: {provider or 'unknown'}",
        f"model: {model or 'unknown'}",
        f"parent: {data.get('parent') or 'none'}",
        f"depth: {data.get('depth', 0)}",
        f"route_source: {route_source}",
    ))


def _account(run: AgentRun, layer: str, task: str = "") -> None:
    """Bill this run to the conversation its caller scoped, under the layer's task name.

    Only a caller that opened :func:`accounting.accounting_scope` gets a ledger row: an
    unscoped run has no conversation to charge.
    """
    scope = accounting.current()
    if scope is None:
        return
    accounting.record(scope[0], scope[1], task or accounting.layer_task(layer),
                      model=run.model, provider=run.provider, usage=run.usage,
                      api_calls=run.api_calls)


def run(message: str, *, context: str = "", model: str = "", provider: str = "",
        api_mode: str = "", toolsets: Optional[Sequence[str]] = None,
        system_message: str | Sequence[str] = "", max_iterations: int = MAX_ITERATIONS,
        project_dir: Optional[str] = None, all_tools: bool = False,
        read_only: bool = False,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
        session_id: str = "", layer: str = "", history_session: str = "",
        account_task: str = "",
        skip_memory: bool = True, skip_context_files: bool = True,
        skip_background_review: bool = True,
        load_soul_identity: bool = False,
        meta: Optional[Mapping[str, Any]] = None) -> AgentRun:
    """Run one agent loop on *message* and return its answer. Never leaves an agent open.

    ``max_iterations`` is the tool-call budget for this peer run. The default is the Feishu
    comment agent's 15; a narrow reviewer should pass a smaller bound (adviser mode uses 5: one
    knowledge search round, up to two page reads, then the object).

    ``history`` is the prior conversation for a continuing peer (adviser mode's persistent
    runtime). It is passed to the host as ``conversation_history`` so the model sees its own
    earlier tool calls and results; the host returns the merged list in ``AgentRun.messages``.
    A ``session_id`` keeps that peer's host-side session distinct from the main agent's (and
    from every other roster member's).

    ``project_dir`` grants read-only access to that project root. Every peer run inherits the
    cwd's git root by default (``None``); a caller passes ``""`` only to opt out, or an explicit
    path to override. Writes stay under ``minir-memory/scratch`` in every case.

    The defaults are the plugin's own posture: no host memory, no project context files, no
    background review fork and no operator soul identity, because a layer pass is not the main
    agent. A custom-agent route (``minir_agents``) opts into memory/context/soul with the flags
    and passes ``all_tools`` when the caller omitted ``tool_sets``.

    ``layer`` binds the run to one transcript stream (``minir_agent_layer`` contextvar), so
    ``minir_aiagent_search`` can only read the caller's own history. An empty value leaves the
    tool unbound, which makes it refuse the call instead of reading every layer.
    """
    from run_agent import AIAgent  # first: it replaces sys.path (managed environment bootstrap)

    resolved_model, runtime = _resolve_model_and_runtime()
    final_model = str(model or "").strip() or str(resolved_model or "").strip()
    final_provider = str(provider or "").strip() or str(runtime.get("provider") or "")
    tools = [str(tool) for tool in (toolsets or [])]
    # A system row belongs to the caller's system_message, not to the message history; the
    # host prepends its own. Anything that is not a mapping is ignored rather than guessed.
    conversation = [dict(row) for row in (history or ())
                    if isinstance(row, Mapping) and str(row.get("role") or "") != "system"]

    knobs = _agent_kwargs(final_provider, final_model, runtime, api_mode)
    # The pinned route's own provider is the effective one; the run metadata must name it.
    final_provider = str(knobs.get("provider") or final_provider)
    agent = AIAgent(
        model=final_model,
        **knobs,
        # A stable per-peer id gives the host one session for this adviser's tool calls; an
        # empty value keeps the host's own default for one-shot peers.
        session_id=str(session_id or "").strip() or None,
        # ``None`` means every tool only when the caller explicitly asks (``all_tools``); an
        # empty list is "no tools", and a list is exactly that surface. The plugin's own
        # ``agent_toolsets`` always adds file/minir/adviser before it reaches here.
        enabled_toolsets=None if all_tools else tools,
        max_iterations=max(1, int(max_iterations or MAX_ITERATIONS)),
        # Defaults keep the plugin's peers separate from the main agent's memory, context and
        # voice. The minir_agents route flips them explicitly.
        quiet_mode=True,
        skip_context_files=bool(skip_context_files),
        skip_memory=bool(skip_memory),
        skip_background_review=bool(skip_background_review),
        load_soul_identity=bool(load_soul_identity),
    )
    if read_only:
        dropped = apply_read_only(agent)
        if dropped:
            logger.debug("agent_loop: read-only peer dropped %s", ", ".join(dropped))
    try:
        # A plugin agent's file tools are the host's own four, scoped by ``file_guard``: reads
        # stay under HERMES_HOME plus an explicitly granted project root, writes under
        # minir-memory/scratch. The ContextVar is set here so every path that runs a peer agent
        # gets the guard, not only the layer worker.
        route_source = ("explicit" if (str(model or "").strip() or str(provider or "").strip())
                        else "gateway")
        if not final_provider or not final_model:
            logger.warning("agent_loop: route resolved to provider=%r model=%r for layer %r",
                           final_provider, final_model, layer)
        if route_source == "gateway" and meta and str(meta.get("parent") or "").strip():
            logger.warning("agent_loop: %s child run has no explicit route; using the gateway "
                           "default", meta.get("agent") or layer)
        segments = _system_segments(system_message)
        if segments:
            segments.append(_run_meta(layer=layer, meta=meta, provider=final_provider,
                                      model=final_model, route_source=route_source,
                                      session_id=session_id))
        system = "\n\n".join(segments) or None
        root = project_dir if project_dir is not None else str(file_guard.project_root() or "")
        # The run's own id is bound beside its layer: a tool that keeps state per run keys on it,
        # and the provider serving that call is bound to the session that dispatched the run.
        token = (agent_history.layer_context(layer, history_session, session_id)
                 if str(layer or "").strip() else None)
        try:
            with file_guard.aux_scope(root):
                result = agent.run_conversation(
                    _prompt(message, context), system_message=system,
                    conversation_history=conversation or None)
        finally:
            if token is not None:
                agent_history.reset_layer(token)
        messages = _with_reasoning(
            _transcript(result), result.get("reasoning_content") or result.get("reasoning"))
        peer = AgentRun(
            answer=(result.get("final_response") or "").strip(),
            api_calls=result.get("api_calls", 0),
            model=final_model,
            provider=final_provider,
            toolsets=("*",) if all_tools else tuple(tools),
            messages=messages,
            usage=_usage(result),
        )
        _account(peer, layer, account_task)
        return peer
    finally:
        # One agent per run in a long-lived host: close() releases tool subprocesses and HTTP
        # clients, otherwise every run leaks them (same reason as the Feishu comment agent).
        with contextlib.suppress(Exception):
            agent.close()


async def run_async(message: str, *, context: str = "", model: str = "", provider: str = "",
                    api_mode: str = "", toolsets: Optional[Sequence[str]] = None,
                    system_message: str | Sequence[str] = "", max_iterations: int = MAX_ITERATIONS,
                    project_dir: Optional[str] = None, all_tools: bool = False,
                    read_only: bool = False,
                    history: Optional[Sequence[Mapping[str, Any]]] = None,
                    session_id: str = "", layer: str = "", history_session: str = "",
                    account_task: str = "",
                    skip_memory: bool = True, skip_context_files: bool = True,
                    skip_background_review: bool = True,
                    load_soul_identity: bool = False,
                    meta: Optional[Mapping[str, Any]] = None) -> AgentRun:
    """Await :func:`run` off the event loop (see the module docstring for why a thread)."""
    return await asyncio.to_thread(
        run, message, context=context, model=model, provider=provider, api_mode=api_mode,
        toolsets=toolsets, system_message=system_message, max_iterations=max_iterations,
        project_dir=project_dir, all_tools=all_tools, read_only=read_only,
        history=history, session_id=session_id, layer=layer,
        history_session=history_session, account_task=account_task,
        skip_memory=skip_memory,
        skip_context_files=skip_context_files, skip_background_review=skip_background_review,
        load_soul_identity=load_soul_identity, meta=meta)


def run_with_timeout(run: Callable[[], Any], timeout: float) -> Any:
    """Run *run* on a daemon thread and stop waiting after *timeout* (``None`` on timeout).

    The pass being bounded calls the model on this thread, and a multiplexed gateway resolves
    the API key through a ContextVar (``agent.secret_scope``): a bare thread would fail closed,
    so the caller's context travels with the run. An exception raised inside raises here.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = run()
        except BaseException as exc:  # never let a background failure escape the thread
            box["error"] = exc

    context = contextvars.copy_context()
    thread = threading.Thread(target=lambda: context.run(target),
                              name="minir-agent-run", daemon=True)
    thread.start()
    thread.join(max(0.1, float(timeout)))
    if thread.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    return box.get("result")
