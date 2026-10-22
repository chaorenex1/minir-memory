"""The plugin's own toolset — the store tools, the knowledge search, the agent-facing searches.

The memory-provider surface already hands these schemas to the main agent through
``MemoryProvider.get_tool_schemas``. That path only reaches an agent that has the host's
``memory`` toolset enabled. This module registers the same functions under a registry toolset
named ``minir`` so the plugin's own agents (the adviser, the layer worker, the hand-off and
session-summary agents, the idle knowledge extraction) can be granted them explicitly:

    enabled_toolsets = ["file", "minir", "skills"]

``minir_kb_search`` is the read-only way into the knowledge directory: :func:`select_paths` only
injects path hints for a turn, while the tool answers "which page covers this" with the matching
lines.

It also registers ``minir_aiagent_search`` (the caller's own transcript stream) and
``minir_hermes_session_search`` is registered by ``adviser.register`` into the same toolset.

One schema source, two delivery paths: :data:`SCHEMAS` is what ``get_tool_schemas`` returns and
what :func:`register` puts in the registry. The handlers delegate to the live provider's
``handle_tool_call`` so there is one implementation of every tool and no drift between the two
surfaces.

:func:`agent_toolsets` also grants the host's ``file`` toolset. Those four tools are not
re-registered here: the plugin runs the host's handlers and ``file_guard`` scopes their paths
for the duration of a peer-agent run.

Registering a tool through the plugin context requires it to be listed under
``provides_tools`` in ``plugin.yaml``; the host validator rejects an undeclared registration.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from tools.registry import tool_error

from .store import CONCLUSION_KINDS
from .knowledge import DEFAULT_SEARCH_LIMIT
from .layers import CATEGORIES, ORGS
from . import agent_history

logger = logging.getLogger(__name__)

TOOLSET = "minir"
SKILL_TOOLSET= "skills"
#: The host's four file tools (``read_file``, ``write_file``, ``patch``, ``search_files``).
#: This plugin does not re-register them: it grants the host toolset to every in-process
#: agent it runs, and ``file_guard`` scopes the calls while that agent runs.
FILE_TOOLSET = "file"
TOOL_NAMES: tuple[str, ...] = (
    "minir_search", "minir_kb_search", "minir_remember", "minir_forget", "minir_todo",
    "minir_stats", "minir_agents", "minir_aiagent_search")

_EMOJI: dict[str, str] = {
    "minir_search": "🔎", "minir_kb_search": "📚", "minir_remember": "📌", "minir_forget": "🗑️",
    "minir_todo": "✅", "minir_stats": "📊", "minir_agents": "🧩",
}


def _tool_schema(name: str, description: str, properties: Optional[dict[str, Any]] = None,
                 required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {},
                           "required": list(required)}}


def _prop(description: str, type_: str = "string", **extra: Any) -> dict[str, Any]:
    return {"type": type_, **extra, "description": description}


_TOOL_SEARCH = _tool_schema(
    "minir_search",
    "Search Minir's conclusions with FTS5 (trigram, CJK-safe) and a LIKE fallback. Rows are "
    "the published cards and summaries (base.* user/AI cards, dialectic.review, session_summary, "
    "window_summary), notes, value markers and todos. Minir keeps no transcripts, and adviser "
    "reviews live in their own table, so neither is searched here. Default scope='session' reads "
    "this session's rows plus durable rows that carry no session id; scope='all' reads every "
    "session but never returns the session-bound base kinds. limit is capped at 20.",
    {"query": _prop("What to search for."),
     "limit": _prop("Maximum results to return (default: the provider's prefetch limit; "
                    "capped at 20).", "integer"),
     "scope": _prop("Recall scope: 'session' (default) or 'all' for cross-session search.",
                    "string", enum=["session", "all"]),
     "type": _prop("Only return conclusions of this kind; omitted searches every kind. A "
                   "session-bound kind needs a session id, and scope='all' drops it unless it "
                   "is the only kind requested, which raises session_scope_required.",
                   "string", enum=list(CONCLUSION_KINDS)),
     "session_id": _prop("Optional session to search instead of the current one "
                         "(ignored when scope='all').")},
    ("query",))
_TOOL_KB_SEARCH = _tool_schema(
    "minir_kb_search",
    "Search the Minir knowledge directory — the pages the knowledge hint points at — and return "
    "the matching lines with each page's topic, category, summary and absolute path. Read-only "
    "and scoped to that directory, never the rest of the profile. Use it when you need to find "
    "which page covers something before opening a file. The pattern is ripgrep/Rust regex "
    "syntax ('docker (prune|restart)', '\\bindex\\b'); matching is case-insensitive, and org or "
    "category filter on the page's front-matter.",
    {"query": _prop("Ripgrep pattern matched case-insensitively against page text."),
     "org": _prop("Only pages under this org folder.", "string", enum=list(ORGS)),
     "category": _prop("Only pages with this front-matter category.", "string",
                       enum=list(CATEGORIES)),
     "limit": _prop("Maximum pages to return (default 8, at most 25).", "integer",
                    minimum=1, maximum=25, default=DEFAULT_SEARCH_LIMIT)},
    ("query",))
_TOOL_REMEMBER = _tool_schema(
    "minir_remember",
    "Record a new conclusion: one kind plus the statement itself, bound to this session and "
    "truncated at 12000 characters. The row is searchable by minir_search and deletable by "
    "minir_forget with its conclusions_id. type='todo' stores a standalone row; the session's "
    "todo list is minir_todo's job.",
    {"type": _prop("Conclusion kind.", "string", enum=list(CONCLUSION_KINDS)),
     "content": _prop("The value-output marker to remember.")},
    ("type", "content"))
_TOOL_FORGET = _tool_schema(
    "minir_forget",
    "Delete one stored conclusion by its conclusions_id and kind. Use only an id returned by "
    "minir_remember or minir_search; the kind must match the stored row or nothing is deleted.",
    {"type": _prop("Conclusion kind the row was stored with.", "string",
                   enum=list(CONCLUSION_KINDS)),
     "conclusions_id": _prop("Id of the conclusion to delete.", "integer")},
    ("type", "conclusions_id"))
_TOOL_TODO = _tool_schema(
    "minir_todo",
    "Replace this session's todo list. At most two levels: a top-level item, and a child item "
    "naming its parent's id. The whole list is stored as one row and rendered into the final "
    "answer as a markdown table, and every prefetch block for the session carries the list. An "
    "empty list deletes the row.",
    {"todos": _prop("The complete list; sending [] clears it.", "array",
                    items={"type": "object",
                           "properties": {
                               "id": _prop("Stable id, unique in this list (<=64 chars)."),
                               "content": _prop("What the item is (<=2000 chars)."),
                               "status": _prop("Item state.", "string",
                                               enum=["pending", "in_progress", "completed",
                                                     "cancelled"]),
                               "parent": _prop("Id of the top-level item this belongs to.")},
                           "required": ["id", "content"]})},
    ("todos",))
_TOOL_STATS = _tool_schema(
    "minir_stats",
    "Report Minir store statistics — note/marker/session/job counts, the base + dialectic layer "
    "counters for this session, the unified scheduler ledger (every turn/time task: runs, "
    "failures, skips, last outcome/detail), the published layer artifacts (subject + version), "
    "the final token usage of this session's turns, and the database path.")
_TOOL_AGENTS = _tool_schema(
    "minir_agents",
    "Run a named in-process agent with its own system prompt, user prompt and optional context "
    "files, then persist its full conversation to sessions/<agent_name>/<layer_session_id>.jsonl. "
    "The system prompt is the first prompt file found under HERMES_HOME, in this order: "
    ".agents/<agent_name>, agents/<agent_name>, skills/<skill_name>/agents/<agent_name>, "
    "plugins/minir/agents/<agent_name>, plugins/minir/skills/<skill_name>/agents/<agent_name>, "
    "each directory accepting <agent_name>.md or a bare <agent_name> file; system_prompts is the "
    "last fallback. Omit "
    "tool_sets to grant every host tool. Unlike the plugin's own layer passes, this agent keeps "
    "host memory and context files and loads the operator soul by default; all four knobs can be "
    "overridden.",
    {"agent_name": _prop("Stable name for this agent; also its transcript folder and the prompt "
                         "file's name."),
     "skill_name": _prop("Skill whose agents/ folder may define the prompt (the two "
                         "skills/<skill_name>/agents directories). Optional."),
     "model": _prop("Model route as 'provider:model', e.g. 'openrouter:anthropic/claude-sonnet-4'. "
                    "A bare model name pins only the model and keeps the gateway's provider; "
                    "omitted uses the gateway route."),
     "system_prompts": _prop("System prompt for the agent. Used only when no prompt file is "
                             "found."),
     "user_prompts": _prop("User message or task for the agent."),
     "context_files": _prop("Files to read and append as context; relative paths resolve "
                            "against project_dir (at most 20 files, 200 KB total).", "array",
                            items={"type": "string"}),
     "project_dir": _prop("Project root the agent may read; defaults to the cwd's git root."),
     "tool_sets": _prop("Toolset names to grant. Omitted or empty grants every host tool; "
                        "'*' is also accepted.", "array", items={"type": "string"}),
     "skip_memory": _prop("Skip host memory injection (default false).", "boolean"),
     "skip_context_files": _prop("Skip host context files (default false).", "boolean"),
     "skip_background_review": _prop("Skip the host background review fork (default true).",
                                      "boolean"),
     "load_soul_identity": _prop("Load the operator SOUL identity (default true).", "boolean")},
    ("agent_name", "user_prompts"))

SCHEMAS: tuple[dict[str, Any], ...] = (
    _TOOL_SEARCH, _TOOL_KB_SEARCH, _TOOL_REMEMBER, _TOOL_FORGET, _TOOL_TODO, _TOOL_STATS,
    _TOOL_AGENTS)


def agent_toolsets(base: Any = None) -> list[str]:
    """*base* plus this plugin's fixed toolsets, order kept, duplicates dropped.

    Every in-process agent this plugin runs gets three surfaces: the host's ``file`` toolset,
    ``minir`` for the store tools and the layer-scoped transcript search, and ``skills`` so a
    reviewer can consult a loaded skill. ``file`` is granted here, not left to the caller, so a
    route that configures no toolsets still gets the file surface ``file_guard`` scopes;
    ``None`` becomes the three, never ``None`` — the host reads ``None`` as "every tool".
    """
    if isinstance(base, str):
        base = [base]
    toolsets = [str(tool) for tool in (base or []) if str(tool).strip()]
    for name in (FILE_TOOLSET, TOOLSET, SKILL_TOOLSET):
        if name not in toolsets:
            toolsets.append(name)
    return toolsets


def register(ctx: Any, provider: Any = None,
             resolve: Optional[Callable[[], Any]] = None) -> list[str]:
    """Register the store tools plus the agent transcript search; returns the names added.

    ``resolve`` is the live-provider lookup (the host can construct this plugin twice). It is
    called per tool call, not at registration, so a provider initialized after this call is still
    the one that serves the tools. A host context without ``register_tool`` (an older collector,
    the Noop engine collector) is left untouched.
    """
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        logger.debug("minir tools: plugin context has no register_tool; provider surface only")
        return []

    lookup = resolve or (lambda: provider)
    registered: list[str] = []
    for schema in SCHEMAS:
        name = str(schema["name"])

        def handler(args: dict[str, Any], _name: str = name, **_kwargs: Any) -> str:
            return _dispatch(_name, args, lookup)

        try:
            register_tool(
                name=name, toolset=TOOLSET, schema=schema, handler=handler,
                is_async=False, description=str(schema.get("description") or "")[:120],
                emoji=_EMOJI.get(name, "🧠"),
            )
            registered.append(name)
        except Exception as exc:  # one refused tool must not cost the other three
            logger.warning("minir tool %s registration failed: %s", name, exc)

    def history_handler(args: dict[str, Any], **_kwargs: Any) -> str:
        try:
            live = lookup()
        except Exception as exc:  # a lookup failure is reported, never raised into the tool loop
            return tool_error(f"Minir provider lookup failed: {exc}")
        return agent_history.handle(args, provider=live)

    try:
        register_tool(
            name=agent_history.TOOL_NAME, toolset=TOOLSET, schema=agent_history.SCHEMA,
            handler=history_handler, is_async=False,
            description="Read the calling layer's own transcript stream.",
            emoji="🗂️",
        )
        registered.append(agent_history.TOOL_NAME)
    except Exception as exc:
        logger.warning("minir tool %s registration failed: %s", agent_history.TOOL_NAME, exc)
    return registered


def _dispatch(name: str, args: dict[str, Any], lookup: Callable[[], Any]) -> str:
    """Route one registry-tool call to the live memory provider's own implementation."""
    try:
        provider = lookup()
    except Exception as exc:  # a lookup failure is reported, never raised into the tool loop
        return tool_error(f"Minir provider lookup failed: {exc}")
    if provider is None or getattr(provider, "_store", None) is None:
        return tool_error("Minir is not initialized")
    return provider.handle_tool_call(name, dict(args or {}))
