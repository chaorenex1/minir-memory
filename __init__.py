"""Minir memory plugin — a minimal Hermes memory-provider template.

A complete, dependency-free reference implementation of the ``MemoryProvider``
contract (``agent/memory_provider.py``): completed turns and explicit notes go
into a per-profile SQLite database, recall is a keyword search over that store,
and four tools let the model search, remember, forget, and inspect it.

Fork this as a starting point:

* ``register(ctx)`` plus the lifecycle methods below are the contract surface.
* ``store.py`` is the storage layer — swap it for your backend (API client,
  vector index, knowledge graph) and keep this module's shape.
* Delete optional methods you do not need instead of leaving empty overrides;
  the ABC already provides no-op defaults.

Activation (ONE external provider at a time, ``memory.provider`` in config.yaml)::

    memory:
      provider: minir

The plugin directory name must equal both the ``memory.provider`` value and the
``name`` property below (``minir``). See README.md for install/rename steps.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import (
    INDICATOR_GLYPH,
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)
from tools.registry import tool_error

from .store import MinirStore, format_when

logger = logging.getLogger(__name__)

_PROVIDER_LABEL = "Minir"
_STORE_DIRNAME = "minir-memory"   # under $HERMES_HOME
_DB_FILENAME = "memory.db"
_DEFAULT_PREFETCH_LIMIT = 5
_MAX_BLOCK_CHARS = 2000           # recall block injected into the prompt is truncated to this
_MAX_TOOL_EXCERPT = 600           # text excerpt returned by the minir_search tool

_BOOL_WORDS = {**dict.fromkeys(("1", "true", "yes", "on"), True),
               **dict.fromkeys(("0", "false", "no", "off"), False)}


def _coerce_bool(value: Any, default: bool) -> bool:
    """bool/number -> truthiness; common true/false words parsed; anything else -> default."""
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return _BOOL_WORDS.get(value.strip().lower(), default) if isinstance(value, str) else default


def _load_config() -> dict[str, Any]:
    """``memory.minir`` block from config.yaml ({} on any error — never raise at import/load time)."""
    try:
        from hermes_cli.config import load_config
        block = (load_config().get("memory") or {}).get("minir")
    except Exception:
        block = None
    return dict(block) if isinstance(block, dict) else {}


def _excerpt(text: str) -> str:
    """Flatten and bound text for tool responses (see ``_MAX_TOOL_EXCERPT``)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _MAX_TOOL_EXCERPT else flat[:_MAX_TOOL_EXCERPT] + "…"


def _env_int(name: str, default: int) -> int:
    """Integer env override; anything unparsable falls back to *default*."""
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


class MinirMemoryProvider(MemoryProvider):
    """Local-first memory: SQLite turn/note store + keyword recall."""

    def __init__(self, config: Optional[dict[str, Any]] = None):
        self._config = dict(config) if config is not None else _load_config()
        self._capture = _coerce_bool(
            os.environ.get("MINIR_CAPTURE_TURNS"), _coerce_bool(self._config.get("capture_turns"), True))
        try:
            default_limit = int(self._config.get("prefetch_limit") or _DEFAULT_PREFETCH_LIMIT)
        except (TypeError, ValueError):
            default_limit = _DEFAULT_PREFETCH_LIMIT
        self._prefetch_limit = max(1, min(20, _env_int("MINIR_PREFETCH_LIMIT", default_limit)))

        self._store: Optional[MinirStore] = None
        self._session_id = ""
        self._agent_context = "primary"
        self._lock = threading.Lock()              # guards the recall cache below
        self._cached: Optional[tuple[str, str, int]] = None  # (session_id, block, count)
        self._last_recall: Optional[RecallStatus] = None
        self._prefetch_thread: Optional[threading.Thread] = None

    # -- contract -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "minir"

    def is_available(self) -> bool:
        """No credentials or external binaries required — always ready."""
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Open the store. ``hermes_home`` is profile-scoped and must be used as given."""
        hermes_home = kwargs.get("hermes_home") or self._fallback_home()
        db_path = Path(hermes_home) / _STORE_DIRNAME / _DB_FILENAME
        self._store = MinirStore(db_path)
        self._session_id = session_id or ""
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        with self._lock:
            self._cached = None
        logger.debug("Minir store ready at %s", db_path)

    @staticmethod
    def _fallback_home() -> str:
        """``initialize`` always passes ``hermes_home``; this is only a safety net."""
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Setup fields for ``hermes memory setup``.

        Every field carries ``env_var``, so this plugin follows the ABC's
        env-var-only rule and does not need to override ``save_config()``.
        """
        return [
            {"key": "capture_turns", "description": "Store completed turns in the local Minir database",
             "type": "boolean", "default": "true", "choices": ["true", "false"],
             "env_var": "MINIR_CAPTURE_TURNS"},
            {"key": "prefetch_limit", "description": "Maximum memory items injected per turn",
             "type": "integer", "default": str(_DEFAULT_PREFETCH_LIMIT),
             "minimum": 1, "maximum": 20, "env_var": "MINIR_PREFETCH_LIMIT"},
        ]

    # -- prompt + recall ------------------------------------------------------

    def system_prompt_block(self) -> str:
        """STATIC prompt text; dynamic recall goes through ``prefetch()``."""
        return (
            "# Minir Memory\n"
            f"Active. Long-term memory lives in {_PROVIDER_LABEL}'s local store.\n"
            "Use minir_search to look up past turns and notes, minir_remember to store "
            "facts worth keeping across sessions, minir_forget to delete a note by id, "
            "and minir_stats to inspect the store."
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Run recall in the background at turn end; ``prefetch()`` consumes it next turn."""
        if self._store is None or not (query or "").strip():
            return
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2.0)  # don't pile up threads on rapid turns
            if self._prefetch_thread.is_alive():
                logger.debug("Minir prefetch still running; skipping this batch")
                return
        session = session_id or self._session_id

        def _work() -> None:
            try:
                rows = self._search(query)
                block = self._format_block(rows)
                with self._lock:
                    self._cached = (session, block, len(rows))
            except Exception as exc:  # a background recall must never surface anywhere
                logger.debug("Minir background recall failed: %s", exc)

        self._prefetch_thread = spawn_context_thread(_work, name="minir-prefetch")
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Formatted recall for the upcoming turn. Must stay fast.

        The host calls this on the critical path before the LLM request: consume
        the queued result when it matches, otherwise fall back to a bounded
        inline search (cheap for local SQLite). A remote backend should return
        only the cached block here and do all I/O in ``queue_prefetch``.
        """
        session = session_id or self._session_id
        block, count = "", 0
        cached = self._take_cached(session)
        if cached is not None:
            block, count = cached
        elif self._store is not None and (query or "").strip() and not is_trivial_prompt(query):
            try:
                rows = self._search(query)
                block, count = self._format_block(rows), len(rows)
            except Exception as exc:
                logger.debug("Minir prefetch search failed: %s", exc)
        self._last_recall = RecallStatus(provider_label=_PROVIDER_LABEL, count=count, glyph=INDICATOR_GLYPH) \
            if block else None
        return block

    def recall_status(self) -> Optional[RecallStatus]:
        """What the last ``prefetch()`` injected — never a stale prior count."""
        return self._last_recall

    # -- persistence ----------------------------------------------------------

    def sync_turn(
        self, user_content: str, assistant_content: str, *,
        session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        """Store the completed turn (host calls this on a background worker, never inline)."""
        if self._store is None or not self._capture:
            return
        if self._agent_context not in ("", "primary"):
            return  # host contract: non-primary contexts (subagent/cron/flush) do not write memory
        user_content = (user_content or "").strip()
        if not user_content or is_trivial_prompt(user_content):
            return
        session = session_id or self._session_id
        author = str((turn_author or {}).get("name") or "")
        try:
            self._store.add_turn(session, "user", user_content, author=author)
            assistant = (assistant_content or "").strip()
            if assistant:
                self._store.add_turn(session, "assistant", assistant)
        except Exception as exc:  # memory failures must never break a turn
            logger.warning("Minir sync_turn failed: %s", exc)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[dict[str, Any]] = None) -> None:
        """Mirror built-in memory-tool writes so both stores stay searchable."""
        if self._store is None or not self._capture or not (content or "").strip():
            return
        try:
            if action in {"add", "replace"}:
                self._store.remember(content.strip(), source=f"memory_tool/{target}")
            elif action == "remove":
                # ``metadata["previous_content"]`` may be absent; content itself is the entry.
                self._store.forget_by_content(content.strip())
        except Exception as exc:
            logger.debug("Minir memory mirror failed: %s", exc)

    # -- session lifecycle ----------------------------------------------------

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        """Rebind per-session state after /resume, /branch, /reset, /new or compression."""
        self._session_id = new_session_id or self._session_id
        if reset:
            with self._lock:
                self._cached = None
            self._last_recall = None

    def shutdown(self) -> None:
        """Flush and close. Idempotent — the host may call it more than once."""
        with self._lock:
            self._cached = None
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=10.0)
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception as exc:
                logger.debug("Minir store close failed: %s", exc)

    # -- tools ----------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [_TOOL_SEARCH, _TOOL_REMEMBER, _TOOL_FORGET, _TOOL_STATS]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Dispatch one of this provider's tools. Must return a JSON string."""
        if self._store is None:
            return tool_error("Minir is not initialized")
        args = args or {}
        if tool_name == "minir_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return tool_error("query is required")
            rows = self._search(query, limit=args.get("limit"))
            return json.dumps({
                "count": len(rows),
                # ``id`` is the note id for kind=note (usable with minir_forget) and the
                # turn row id for kind=turn (informational only). Text is excerpted so a
                # broad query cannot flood the model's context.
                "results": [{"id": row["id"], "kind": row["kind"], "when": format_when(row["when"]),
                             "role": row["role"], "text": _excerpt(row["text"])} for row in rows],
            })
        if tool_name == "minir_remember":
            content = str(args.get("content") or "").strip()
            if not content:
                return tool_error("content is required")
            note_id = self._store.remember(content, source="tool")
            return json.dumps({"stored": True, "note_id": note_id})
        if tool_name == "minir_forget":
            try:  # tolerate "5" from models that stringify integers
                note_id = int(args.get("note_id"))
            except (TypeError, ValueError):
                return tool_error("note_id must be an integer")
            return json.dumps({"deleted": self._store.forget(note_id), "note_id": note_id})
        if tool_name == "minir_stats":
            return json.dumps({**self._store.stats(), "session_id": self._session_id,
                               "capture_turns": self._capture, "prefetch_limit": self._prefetch_limit})
        return tool_error(f"Unknown tool: {tool_name}")

    # -- internals ------------------------------------------------------------

    def _search(self, query: str, limit: Optional[Any] = None) -> list[dict[str, Any]]:
        try:
            requested = int(limit) if limit is not None else self._prefetch_limit
        except (TypeError, ValueError):
            requested = self._prefetch_limit
        return self._store.search(query, limit=max(1, min(20, requested)))

    def _take_cached(self, session: str) -> Optional[tuple[str, int]]:
        """Consume the queued recall for *session*; ``None`` when there is nothing usable."""
        with self._lock:
            cached, self._cached = self._cached, None
        if cached is None:
            return None
        cached_session, block, count = cached
        if cached_session != session or not block:
            return None
        return block, count

    def _format_block(self, rows: list[dict[str, Any]]) -> str:
        """Render recall rows as a compact markdown block (empty string when there are none)."""
        if not rows:
            return ""
        lines = [f"## {_PROVIDER_LABEL} recall"]
        size = len(lines[0])
        for row in rows:
            if row["kind"] == "turn":
                speaker = "User" if row["role"] == "user" else "Assistant"
                label = f"{speaker} · {format_when(row['when'])}"
            else:
                label = f"Note ({row['author'] or 'tool'}) · {format_when(row['when'])}"
            text = " ".join(row["text"].split())
            line = f"- [{label}] {text}"
            size += len(line) + 1
            if size > _MAX_BLOCK_CHARS:
                lines.append("- […more results truncated]")
                break
            lines.append(line)
        return "\n".join(lines)


def _tool_schema(name: str, description: str, properties: Optional[dict[str, Any]] = None,
                 required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}


def _prop(description: str, type_: str = "string", **extra: Any) -> dict[str, Any]:
    return {"type": type_, **extra, "description": description}


_TOOL_SEARCH = _tool_schema(
    "minir_search",
    "Search Minir's long-term memory (past turns and stored notes) for relevant context. "
    "Use whenever earlier sessions, decisions, or preferences would help.",
    {"query": _prop("What to search for."),
     "limit": _prop("Maximum results to return (default: the provider's prefetch limit).", "integer")},
    ("query",))
_TOOL_REMEMBER = _tool_schema(
    "minir_remember",
    "Store an explicit fact, preference, or decision in long-term memory so future sessions can recall it.",
    {"content": _prop("The information to remember.")}, ("content",))
_TOOL_FORGET = _tool_schema(
    "minir_forget",
    "Delete a stored note by its id (ids come from minir_remember and minir_search results).",
    {"note_id": _prop("Id of the note to delete.", "integer")}, ("note_id",))
_TOOL_STATS = _tool_schema(
    "minir_stats",
    "Report Minir store statistics — turn/note/session counts and the database path.")


def register(ctx) -> None:
    """Entry point the Hermes loader calls; registers this provider under its name."""
    ctx.register_memory_provider(MinirMemoryProvider())
