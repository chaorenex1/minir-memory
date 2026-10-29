"""Read-only session search over the host's durable conversation store.

The host keeps every turn verbatim in ``$HERMES_HOME/state.db`` (``messages.timestamp`` is REAL
epoch seconds). This module exposes that store to the ``minir`` toolset as one JSON tool with five
query shapes, chosen explicitly (``mode``) or inferred from the arguments:

* ``search``   — FTS5 (``messages_fts`` / ``messages_fts_trigram``) or a ``LIKE`` fallback over
                 message content, with role/source/session/time filters and snippets;
* ``sessions`` — browse or filter session metadata (id/title substring, source, time, archived);
* ``messages`` — read one session's transcript, oldest/newest first, paged;
* ``around``   — a bounded window around one message id inside its session;
* ``stats``    — store aggregates: counts, roles, sources, FTS tables, size, oldest/newest row.

Everything is bounded and read-only. No model calls, no writes, no transcript copies: the tool
answers with the host's own rows, truncated per message. ``profile`` reads another Hermes
profile's database (``$HERMES_HOME/profiles/<name>/state.db``).

Two host details matter for correctness:

* only ``active = 1`` rows are current; compaction marks superseded rows inactive (``compacted``
  distinguishes archived display history from rewind/undo rows);
* ``role = 'user'`` also contains host-synthesized rows — ``[Assistant Rules]`` skill prompts,
  background-process notices, ``[System: …]`` switches and compaction handoffs. This plugin's own
  ``[minir summary …]`` and ``[minir memory notice]`` rows are hidden role-agnostically by
  default; ``include_synthetic`` exposes them explicitly.

Schema drift is handled per column: old stores without ``sessions``, ``active``, ``compacted``,
``message_uid`` or FTS tables still answer, and searches fall back to ``LIKE`` when FTS5 is not
usable.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .history import MINIR_SYNTHETIC_PREFIXES, SYNTHETIC_PREFIXES

logger = logging.getLogger(__name__)

TOOL_NAME = "minir_hermes_session_search"
TOOLSET = "minir"

#: ``initialize(platform=...)`` values are not the same enum as ``sessions.source``. This map is
#: the default filter for a host caller; a source outside it is reachable with an explicit
#: ``source`` argument or ``platform="*"``. The result always echoes the filter it applied.
_PLATFORM_SOURCES: dict[str, tuple[str, ...]] = {
    "cli": ("cli",),
    "tui": ("tui",),
    "desktop": ("desktop",),
    "local": ("local", "api_server"),
    "api_server": ("api_server", "local"),
    "plugin-agent": ("plugin-agent", "subagent"),
    "subagent": ("subagent", "plugin-agent"),
    "cron": ("cron",),
    "oneshot": ("oneshot",),
    "telegram": ("telegram",),
    "feishu": ("feishu",),
    "kanban": ("kanban",),
    "tool": ("tool",),
    "qqbot": ("qqbot",),
    "weixin": ("weixin",),
    "acp": ("acp",),
}

_MODE_DEFAULT = "auto"
_MODES = ("search", "sessions", "messages", "around", "stats")
_MATCHES = ("all", "any", "phrase", "raw")
_INDEXES = ("auto", "standard", "trigram")
_SORTS = ("relevance", "newest", "oldest")
_ORDERS = ("oldest", "newest")

_HIDDEN_SOURCES = ("kanban", "subagent", "tool")
_SESSION_FIELDS = ("source", "model", "title", "started_at", "ended_at", "last_activity_at",
                   "message_count", "tool_call_count", "parent_session_id", "end_reason",
                   "profile_name", "cwd", "archived", "hidden")
_OPTIONAL_MESSAGE_FIELDS = ("tool_name", "message_uid", "display_kind", "active", "compacted",
                            "_compressed_summary")

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 100
_DEFAULT_MESSAGES = 50
_MAX_MESSAGES = 200
_DEFAULT_MAX_CHARS = 1200
_MAX_MESSAGE_CHARS = 4000
_MAX_WINDOW = 50
_MAX_OFFSET = 100_000
_DEFAULT_PROFILE = "default"
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_WORD_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_RELATIVE_RE = re.compile(r"^(\d+)\s*(m|h|d|w)$", re.IGNORECASE)
_RELATIVE_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

_LIKE_ESCAPE = "\\"

#: Bound by the adviser around one review run (``<session>:<review id>``). The escape hatch for a
#: skeletonised tool event may read one ``tool_call_id`` once per review: a repeated read buys
#: the same bytes again, and the risk list in the parity draft names exactly that.
READ_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "minir_search_read_scope", default="")
_SEEN_LOCK = threading.Lock()
_SEEN_READS: "OrderedDict[str, set[str]]" = OrderedDict()
_SEEN_SCOPES = 64


def claim_tool_read(tool_call_id: str) -> bool:
    """True when this read may proceed; False on a repeat inside the same review scope.

    An unbound scope (a host caller, a test) always proceeds: the bound is for the reviewer's
    escape hatch, not for the operator's search tool.
    """
    scope = str(READ_SCOPE.get() or "")
    cid = str(tool_call_id or "").strip()
    if not scope or not cid:
        return True
    with _SEEN_LOCK:
        seen = _SEEN_READS.get(scope)
        if seen is None:
            seen = set()
            _SEEN_READS[scope] = seen
            while len(_SEEN_READS) > _SEEN_SCOPES:
                _SEEN_READS.popitem(last=False)
        if cid in seen:
            return False
        seen.add(cid)
        return True


def platform_sources(platform: str) -> list[str]:
    """Which ``sessions.source`` values a caller platform sees by default."""
    name = str(platform or "").strip()
    if not name:
        return []
    return list(_PLATFORM_SOURCES.get(name, (name,)))


class QueryError(Exception):
    """A user-facing failure: ``kind`` is the envelope's error type."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# --------------------------------------------------------------------------- state.db resolution


def _platform_default_home() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def hermes_home(explicit: Any = "") -> Path:
    """``explicit`` -> the host's context-aware resolver -> ``HERMES_HOME`` -> platform default."""
    if str(explicit or "").strip():
        return Path(os.path.expanduser(os.path.expandvars(str(explicit).strip())))
    try:  # host first: this carries the routed-profile ContextVar, not just the env var
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # health: allow BLE001 -- the fallback below is deliberate
        logger.debug("session_search: host home lookup failed", exc_info=True)
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(os.path.expanduser(os.path.expandvars(env))) if env else _platform_default_home()


def state_db_path(*, hermes_home_path: Any = "", profile: Any = "") -> Path:
    """Resolve ``<home>/state.db`` for the current or a named profile."""
    home = hermes_home(hermes_home_path)
    name = str(profile or "").strip()
    if not name or name.lower() in ("default", "current", "active"):
        return home / "state.db"
    if not _PROFILE_RE.fullmatch(name):
        raise QueryError("invalid_args",
                         f"invalid profile name {name!r}; use letters, digits, '.', '_' or '-'")
    return home / "profiles" / name / "state.db"


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise QueryError("state_db_missing", f"state.db not found at {path}")
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise QueryError("state_db_error", f"cannot open {path}: {exc}") from exc


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _tables(connection: sqlite3.Connection) -> set[str]:
    try:
        return {str(row["name"]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    except sqlite3.Error:
        return set()


# --------------------------------------------------------------------------- argument helpers


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _csv(value: Any) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
    out: list[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _roles(value: Any, default: Optional[Iterable[str]]) -> Optional[list[str]]:
    if value is None or value == "":
        return list(default) if default is not None else None
    roles = [role.lower() for role in _csv(value)]
    if any(role in ("all", "*", "any") for role in roles):
        return None
    return roles or None


def _match(value: Any) -> str:
    text = str(value or "all").strip().lower()
    if text in ("and", "all"):
        return "all"
    if text in ("or", "any"):
        return "any"
    if text in ("phrase", "exact"):
        return "phrase"
    if text == "raw":
        return "raw"
    raise QueryError("invalid_args", f"match must be one of {', '.join(_MATCHES)}")


def _sort(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    if text not in _SORTS:
        raise QueryError("invalid_args", f"sort must be one of {', '.join(_SORTS)}")
    return text


def _mode(args: dict[str, Any]) -> str:
    mode = str(args.get("mode") or "").strip().lower()
    if mode:
        if mode not in _MODES:
            raise QueryError("invalid_args", f"mode must be one of {', '.join(_MODES)}")
        return mode
    if args.get("session_id") and args.get("message_id") is not None:
        return "around"
    if args.get("session_id"):
        return "messages"
    if str(args.get("query") or "").strip():
        return "search"
    return "sessions"


def _parse_bound(value: Any, name: str) -> Optional[float]:
    """Epoch seconds, ISO date/datetime (UTC when naive), or ``7d`` / ``24h`` / ``2w`` ago."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if relative := _RELATIVE_RE.fullmatch(text):
        return time.time() - int(relative.group(1)) * _RELATIVE_SECONDS[relative.group(2).lower()]
    try:
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return float(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        raise QueryError(
            "invalid_args",
            f"invalid {name} bound {value!r}; use epoch seconds, ISO date/datetime, or 7d/24h/2w",
        ) from None


def _bounds(args: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    after = args.get("after")
    before = args.get("before")
    if after is None:
        after = args.get("since")
    if before is None:
        before = args.get("until")
    after_ts = _parse_bound(after, "after")
    before_ts = _parse_bound(before, "before")
    if after_ts is not None and before_ts is not None and after_ts >= before_ts:
        raise QueryError("invalid_args", "after must be earlier than before")
    return after_ts, before_ts


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _like_escape(text: str) -> str:
    return text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- query building


def _fts_query(text: str, match: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise QueryError("invalid_args", "query is required for mode=search")
    if match == "raw":
        return stripped
    if match == "phrase":
        return '"' + stripped.replace('"', '""') + '"'
    terms = _WORD_RE.findall(stripped)
    if not terms:
        raise QueryError("invalid_args", "query has no searchable terms")
    joiner = " OR " if match == "any" else " AND "
    return joiner.join('"' + term.replace('"', '""') + '"' for term in terms)


def _terms_for_like(text: str, match: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        raise QueryError("invalid_args", "query is required for mode=search")
    if match == "phrase":
        return [stripped]
    terms = _WORD_RE.findall(stripped)
    if not terms:
        raise QueryError("invalid_args", "query has no searchable terms")
    return terms


def _pick_index(connection: sqlite3.Connection, text: str, match: str, requested: str,
                roles: Optional[list[str]], sources: Optional[list[str]],
                include_hidden: bool = False) -> Optional[str]:
    tables = _tables(connection)
    standard = "messages_fts" if "messages_fts" in tables else None
    trigram = "messages_fts_trigram" if "messages_fts_trigram" in tables else None
    if requested == "standard":
        return standard
    if requested == "trigram":
        return trigram
    # Auto: a CJK run of at least three characters is only a substring match in the trigram
    # index. Its view excludes tool rows and cron/subagent sessions, so keep the standard index
    # when the caller asked for those.
    if trigram and match != "raw" and not include_hidden and _CJK_RUN_RE.search(text):
        terms = _WORD_RE.findall(text)
        if terms and all(len(term) >= 3 for term in terms) \
                and not (roles is None or "tool" in roles) and not sources:
            return trigram
    return standard or trigram


def _visibility_clauses(mcols: set[str], scols: set[str], *,
                        has_sessions: bool, include_inactive: bool, include_compacted: bool,
                        include_synthetic: bool, include_hidden: bool, include_archived: bool,
                        roles: Optional[list[str]], sources: list[str]
                        ) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        if include_compacted and {"active", "compacted"} <= mcols:
            clauses.append("(m.active = 1 OR m.compacted = 1)")
        elif "active" in mcols:
            clauses.append("COALESCE(m.active, 1) = 1")
    if not include_inactive and not include_compacted and "_compressed_summary" in mcols:
        clauses.append("COALESCE(m._compressed_summary, 0) = 0")
    if "display_kind" in mcols:
        clauses.append("COALESCE(m.display_kind, '') <> 'hidden'")
    if roles:
        clauses.append("m.role IN (" + ",".join("?" for _ in roles) + ")")
        params.extend(roles)
    if not include_synthetic and (not roles or "user" in roles):
        for prefix in SYNTHETIC_PREFIXES:
            clauses.append(
                "NOT (m.role = 'user' AND LTRIM(COALESCE(m.content, '')) LIKE ? ESCAPE '\\')")
            params.append(_like_escape(prefix) + "%")
    if not include_synthetic:
        # The compression summary is an assistant row; filtering it by role would let a later
        # review summarize the model's own summary. Markers are role-agnostic on purpose.
        for prefix in MINIR_SYNTHETIC_PREFIXES:
            clauses.append(
                "NOT (LTRIM(COALESCE(m.content, '')) LIKE ? ESCAPE '\\')")
            params.append(_like_escape(prefix) + "%")
    if has_sessions and "source" in scols:
        if sources:
            clauses.append("s.source IN (" + ",".join("?" for _ in sources) + ")")
            params.extend(sources)
        elif not include_hidden:
            clauses.append("COALESCE(s.source, '') NOT IN ("
                           + ",".join("?" for _ in _HIDDEN_SOURCES) + ")")
            params.extend(_HIDDEN_SOURCES)
    if has_sessions and not include_archived and "archived" in scols:
        clauses.append("COALESCE(s.archived, 0) = 0")
    if has_sessions and not include_hidden and "hidden" in scols:
        clauses.append("COALESCE(s.hidden, 0) = 0")
    return clauses, params


def _session_select(scols: set[str], alias: str = "s") -> str:
    parts = [f"{alias}.{field} AS {field}" if field in scols else f"NULL AS {field}"
             for field in _SESSION_FIELDS]
    return ", ".join(parts)


def _message_select(mcols: set[str]) -> str:
    parts = ["m.id AS message_id", "m.session_id AS session_id", "m.role AS role",
             "m.content AS content", "m.timestamp AS timestamp"]
    for field in _OPTIONAL_MESSAGE_FIELDS:
        if field in mcols:
            parts.append(f"m.{field} AS {field}")
    return ", ".join(parts)


def _shape_session(row: Iterable[Any]) -> dict[str, Any]:
    data = dict(row)
    session_id = data.get("session_id") or data.get("id") or ""
    started_at = _number(data.get("started_at"))
    ended_at = _number(data.get("ended_at"))
    last_active = _number(data.get("last_activity_at"))
    entry: dict[str, Any] = {
        "id": str(session_id),
        "title": data.get("title"),
        "source": data.get("source"),
        "model": data.get("model"),
        "started_at": started_at,
        "started_at_iso": _iso(started_at),
        "ended_at": ended_at,
        "ended_at_iso": _iso(ended_at),
        "last_activity_at": last_active,
        "last_activity_at_iso": _iso(last_active),
        "message_count": data.get("message_count"),
        "tool_call_count": data.get("tool_call_count"),
        "parent_session_id": data.get("parent_session_id"),
        "end_reason": data.get("end_reason"),
        "profile_name": data.get("profile_name"),
        "cwd": data.get("cwd"),
        "archived": bool(data.get("archived") or 0),
        "hidden": bool(data.get("hidden") or 0),
    }
    return entry


def _shape_message(row: Iterable[Any], *, max_chars: int, snippet: Optional[str] = None,
                   include_content: bool = True, anchor: bool = False) -> dict[str, Any]:
    data = dict(row)
    text = data.get("content")
    text = "" if text is None else str(text)
    original = len(text)
    truncated = False
    if include_content and max_chars > 0 and original > max_chars:
        text = text[:max_chars] + "…"
        truncated = True
    elif not include_content:
        text = ""
    timestamp = _number(data.get("timestamp"))
    entry: dict[str, Any] = {
        "message_id": data.get("message_id"),
        "session_id": data.get("session_id"),
        "role": data.get("role"),
        "timestamp": timestamp,
        "timestamp_iso": _iso(timestamp),
    }
    if snippet is not None:
        entry["snippet"] = snippet
    if include_content:
        entry["content"] = text
        entry["content_truncated"] = truncated
        entry["original_chars"] = original
    for field in ("tool_name", "message_uid"):
        if data.get(field):
            entry[field] = data[field]
    if anchor:
        entry["anchor"] = True
    return entry


def _fallback_snippet(text: str, terms: list[str]) -> str:
    body = text or ""
    lowered = body.lower()
    found = [(lowered.find(term.lower()), term) for term in terms]
    found = [(index, term) for index, term in found if index >= 0]
    if not found:
        return body[:240] + ("…" if len(body) > 240 else "")
    index, term = min(found)
    start = max(0, index - 100)
    end = min(len(body), index + len(term) + 160)
    inner = body[start:index] + "[" + body[index:index + len(term)] + "]" \
        + body[index + len(term):end]
    return ("…" if start else "") + inner + ("…" if end < len(body) else "")


# --------------------------------------------------------------------------- modes


def _search(connection: sqlite3.Connection, args: dict[str, Any], mcols: set[str],
            scols: set[str], path: Path) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise QueryError("invalid_args", "query is required for mode=search")
    match = _match(args.get("match"))
    requested_index = str(args.get("index") or "auto").strip().lower()
    if requested_index not in _INDEXES:
        raise QueryError("invalid_args", f"index must be one of {', '.join(_INDEXES)}")
    roles = _roles(args.get("role"), ("user", "assistant"))
    sources = _csv(args.get("source"))
    session_id = str(args.get("session_id") or "").strip()
    after_ts, before_ts = _bounds(args)
    limit = _int(args.get("limit"), _DEFAULT_LIMIT, 1, _MAX_LIMIT)
    offset = _int(args.get("offset"), 0, 0, _MAX_OFFSET)
    sort = _sort(args.get("sort"), "relevance")
    max_chars = _int(args.get("max_chars"), _DEFAULT_MAX_CHARS, 0, _MAX_MESSAGE_CHARS)
    include_content = _bool(args.get("include_content"), True)

    has_sessions = "id" in scols
    join = " LEFT JOIN sessions AS s ON s.id = m.session_id" if has_sessions else ""
    clauses, params = _visibility_clauses(
        mcols, scols, has_sessions=has_sessions,
        include_inactive=_bool(args.get("include_inactive")),
        include_compacted=_bool(args.get("include_compacted")),
        include_synthetic=_bool(args.get("include_synthetic")),
        include_hidden=_bool(args.get("include_hidden")),
        include_archived=_bool(args.get("include_archived")),
        roles=roles, sources=sources)
    if session_id:
        clauses.append("m.session_id = ?")
        params.append(session_id)
    if after_ts is not None:
        clauses.append("m.timestamp >= ?")
        params.append(after_ts)
    if before_ts is not None:
        clauses.append("m.timestamp < ?")
        params.append(before_ts)

    selected_index = _pick_index(connection, query, match, requested_index, roles, sources,
                                 include_hidden=_bool(args.get("include_hidden")))
    entries: list[dict[str, Any]] = []
    total = 0
    session_total = 0
    used: Optional[str] = selected_index
    if selected_index is not None:
        try:
            fts_query = _fts_query(query, match)
            if not _fts_has_rows(connection, selected_index):
                # A store whose FTS table is still empty (rebuild in progress) must not answer
                # "no matches" for content that is plainly there.
                raise sqlite3.OperationalError(f"{selected_index} is empty")
            rows, total, session_total = _run_fts(
                connection, selected_index, fts_query, clauses, params, join,
                has_sessions, mcols, scols, sort, limit, offset)
            for row in rows:
                data = dict(row)
                entries.append(_shape_message(
                    data, max_chars=max_chars, snippet=data.get("snippet"),
                    include_content=include_content))
                if has_sessions:
                    entries[-1]["session"] = _shape_session(row)
        except sqlite3.OperationalError as exc:
            if match == "raw":
                raise QueryError("invalid_query", f"FTS query rejected: {exc}") from exc
            logger.debug("session_search: FTS failed, falling back to LIKE: %s", exc)
            used = None
        except sqlite3.Error as exc:
            logger.debug("session_search: FTS read failed, falling back to LIKE: %s", exc)
            used = None
    if used is None:
        rows, total, session_total, used = _run_like(
            connection, query, match, clauses, params, join, has_sessions,
            mcols, scols, sort, limit, offset)
        for row in rows:
            data = dict(row)
            term_list = _terms_for_like(query, match)
            snippet = _fallback_snippet(str(data.get("content") or ""), term_list)
            entry = _shape_message(data, max_chars=max_chars, snippet=snippet,
                                   include_content=include_content)
            if has_sessions:
                entry["session"] = _shape_session(row)
            entries.append(entry)

    data = {
        "mode": "search",
        "db": str(path),
        "query": query,
        "match": match,
        "index": used or "none",
        "filters": {
            "role": roles,
            "source": sources or None,
            "session_id": session_id or None,
            "after": after_ts,
            "before": before_ts,
        },
        "count": len(entries),
        "total": total,
        "sessions": session_total,
        "offset": offset,
        "has_more": total > offset + len(entries),
        "matches": entries,
    }
    if total > offset + len(entries):
        data["next_offset"] = offset + len(entries)
    if not total:
        data["hint"] = (
            "No matches. FTS5 combines terms with AND by default: use match='any' for OR, "
            "match='phrase' for an exact phrase, match='raw' for FTS5 syntax (deploy* OR staging), "
            "or widen the role/source/time filters.")
    return data


def _fts_has_rows(connection: sqlite3.Connection, index: str) -> bool:
    try:
        return connection.execute(f"SELECT 1 FROM {index} LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return True  # let the real query surface the problem


def _run_fts(connection: sqlite3.Connection, index: str, fts_query: str,
             clauses: list[str], params: list[Any], join: str, has_sessions: bool,
             mcols: set[str], scols: set[str], sort: str,
             limit: int, offset: int) -> tuple[list[sqlite3.Row], int, int]:
    where = ["%s MATCH ?" % index] + clauses
    where_sql = " AND ".join(where)
    query_params = [fts_query, *params]
    session_columns = ", " + _session_select(scols) if has_sessions else ""
    base_from = f"FROM {index} JOIN messages AS m ON m.id = {index}.rowid{join}"
    count_sql = (f"SELECT COUNT(*) AS n, COUNT(DISTINCT m.session_id) AS sessions "
                 f"{base_from} WHERE {where_sql}")
    count_row = connection.execute(count_sql, query_params).fetchone()
    total = int(count_row["n"] or 0) if count_row is not None else 0
    sessions = int(count_row["sessions"] or 0) if count_row is not None else 0
    order = ("bm25(%s) ASC, m.timestamp DESC, m.id DESC" % index if sort == "relevance"
             else "m.timestamp ASC, m.id ASC" if sort == "oldest"
             else "m.timestamp DESC, m.id DESC")
    select = (f"SELECT {_message_select(mcols)}{session_columns}, "
              f"snippet({index}, 0, '[', ']', '…', 24) AS snippet, "
              f"bm25({index}) AS score {base_from} WHERE {where_sql} "
              f"ORDER BY {order} LIMIT ? OFFSET ?")
    rows = connection.execute(select, [*query_params, limit, offset]).fetchall()
    return rows, total, sessions


def _run_like(connection: sqlite3.Connection, query: str, match: str,
              clauses: list[str], params: list[Any], join: str, has_sessions: bool,
              mcols: set[str], scols: set[str], sort: str,
              limit: int, offset: int) -> tuple[list[sqlite3.Row], int, int, str]:
    terms = _terms_for_like(query, match)
    if match == "raw":
        raise QueryError("invalid_query",
                         "match='raw' needs a usable FTS5 index; use match='all'/'any'/'phrase'")
    term_clauses = ["m.content LIKE ? ESCAPE '\\'" for _ in terms]
    joiner = " OR " if match == "any" else " AND "
    where = ["(" + joiner.join(term_clauses) + ")"] + clauses
    where_sql = " AND ".join(where)
    query_params = ["%" + _like_escape(term) + "%" for term in terms]
    query_params.extend(params)
    session_columns = ", " + _session_select(scols) if has_sessions else ""
    base_from = f"FROM messages AS m{join}"
    count_sql = (f"SELECT COUNT(*) AS n, COUNT(DISTINCT m.session_id) AS sessions "
                 f"{base_from} WHERE {where_sql}")
    count_row = connection.execute(count_sql, query_params).fetchone()
    total = int(count_row["n"] or 0) if count_row is not None else 0
    sessions = int(count_row["sessions"] or 0) if count_row is not None else 0
    order = ("m.timestamp ASC, m.id ASC" if sort == "oldest"
             else "m.timestamp DESC, m.id DESC")
    select = (f"SELECT {_message_select(mcols)}{session_columns} {base_from} "
              f"WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?")
    rows = connection.execute(select, [*query_params, limit, offset]).fetchall()
    return rows, total, sessions, "like"


def _sessions(connection: sqlite3.Connection, args: dict[str, Any], scols: set[str],
              path: Path) -> dict[str, Any]:
    if "id" not in scols:
        raise QueryError("state_db_error", "the state.db has no sessions table")
    query = str(args.get("query") or "").strip()
    sources = _csv(args.get("source"))
    after_ts, before_ts = _bounds(args)
    limit = _int(args.get("limit"), _DEFAULT_LIMIT, 1, _MAX_LIMIT)
    offset = _int(args.get("offset"), 0, 0, _MAX_OFFSET)
    sort = _sort(args.get("sort"), "newest")
    include_archived = _bool(args.get("include_archived"))
    include_hidden = _bool(args.get("include_hidden"))

    clauses: list[str] = []
    params: list[Any] = []
    if query:
        if "title" in scols:
            clauses.append("(s.id LIKE ? ESCAPE '\\' OR s.title LIKE ? ESCAPE '\\')")
            pattern = "%" + _like_escape(query) + "%"
            params.extend([pattern, pattern])
        else:
            clauses.append("s.id LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(query) + "%")
    if sources and "source" in scols:
        clauses.append("s.source IN (" + ",".join("?" for _ in sources) + ")")
        params.extend(sources)
    elif not include_hidden and "source" in scols:
        clauses.append("COALESCE(s.source, '') NOT IN ("
                       + ",".join("?" for _ in _HIDDEN_SOURCES) + ")")
        params.extend(_HIDDEN_SOURCES)
    if not include_archived and "archived" in scols:
        clauses.append("COALESCE(s.archived, 0) = 0")
    if not include_hidden and "hidden" in scols:
        clauses.append("COALESCE(s.hidden, 0) = 0")
    time_column = "s.started_at" if "started_at" in scols else None
    if after_ts is not None and time_column:
        clauses.append(f"{time_column} >= ?")
        params.append(after_ts)
    if before_ts is not None and time_column:
        clauses.append(f"{time_column} < ?")
        params.append(before_ts)
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    if sort == "oldest":
        order = "s.started_at ASC, s.id ASC" if "started_at" in scols else "s.id ASC"
    else:
        recent = [f"s.{column}" for column in ("last_activity_at", "started_at")
                  if column in scols]
        order = (f"COALESCE({', '.join(recent)}, 0) DESC, s.id DESC"
                 if recent else "s.id DESC")
    count_row = connection.execute(
        f"SELECT COUNT(*) AS n FROM sessions AS s{where_sql}", params).fetchone()
    total = int(count_row["n"] or 0) if count_row is not None else 0
    select = (f"SELECT s.id AS session_id, {_session_select(scols)} FROM sessions AS s"
              f"{where_sql} ORDER BY {order} LIMIT ? OFFSET ?")
    rows = connection.execute(select, [*params, limit, offset]).fetchall()
    entries = [_shape_session(row) for row in rows]
    data = {
        "mode": "sessions",
        "db": str(path),
        "query": query or None,
        "filters": {"source": sources or None, "after": after_ts, "before": before_ts,
                    "include_archived": include_archived, "include_hidden": include_hidden},
        "count": len(entries),
        "total": total,
        "offset": offset,
        "has_more": total > offset + len(entries),
        "sessions": entries,
    }
    if total > offset + len(entries):
        data["next_offset"] = offset + len(entries)
    return data


def _session_meta(connection: sqlite3.Connection, scols: set[str],
                  session_id: str) -> Optional[dict[str, Any]]:
    if "id" not in scols:
        return None
    row = connection.execute(
        f"SELECT s.id AS session_id, {_session_select(scols)} FROM sessions AS s WHERE s.id = ?",
        (session_id,)).fetchone()
    return _shape_session(row) if row is not None else None


def _message_where(args: dict[str, Any], mcols: set[str], session_id: str,
                   roles: Optional[list[str]], after_ts: Optional[float],
                   before_ts: Optional[float]) -> tuple[list[str], list[Any]]:
    clauses, params = _visibility_clauses(
        mcols, set(), has_sessions=False,
        include_inactive=_bool(args.get("include_inactive")),
        include_compacted=_bool(args.get("include_compacted")),
        include_synthetic=_bool(args.get("include_synthetic")),
        include_hidden=True, include_archived=True,
        roles=roles, sources=[])
    clauses.append("m.session_id = ?")
    params.append(session_id)
    tool_call_id = str(args.get("tool_call_id") or "").strip()
    if tool_call_id:
        # One WHERE, no paging: the escape hatch for a skeletonised tool event. An old store
        # without the column cannot answer it, and returning unrelated rows would be worse
        # than refusing.
        if "tool_call_id" not in mcols:
            raise QueryError("state_db_error",
                             "this state.db has no messages.tool_call_id column")
        clauses.append("m.tool_call_id = ?")
        params.append(tool_call_id)
    if after_ts is not None:
        clauses.append("m.timestamp >= ?")
        params.append(after_ts)
    if before_ts is not None:
        clauses.append("m.timestamp < ?")
        params.append(before_ts)
    return clauses, params


def _messages(connection: sqlite3.Connection, args: dict[str, Any], mcols: set[str],
              scols: set[str], path: Path) -> dict[str, Any]:
    session_id = str(args.get("session_id") or "").strip()
    if not session_id:
        raise QueryError("invalid_args", "session_id is required for mode=messages")
    tool_call_id = str(args.get("tool_call_id") or "").strip()
    if tool_call_id and not claim_tool_read(tool_call_id):
        raise QueryError(
            "duplicate_read",
            f"this review already read tool_call_id {tool_call_id!r}; use the earlier tool result")
    roles = _roles(args.get("role"), None)
    after_ts, before_ts = _bounds(args)
    limit = _int(args.get("limit"), _DEFAULT_MESSAGES, 1, _MAX_MESSAGES)
    offset = _int(args.get("offset"), 0, 0, _MAX_OFFSET)
    order = str(args.get("order") or "").strip().lower()
    if order and order not in _ORDERS:
        raise QueryError("invalid_args", f"order must be one of {', '.join(_ORDERS)}")
    if not order:
        order = "newest" if str(args.get("sort") or "").lower() == "newest" else "oldest"
    max_chars = _int(args.get("max_chars"), _DEFAULT_MAX_CHARS, 0, _MAX_MESSAGE_CHARS)

    meta = _session_meta(connection, scols, session_id)
    clauses, params = _message_where(args, mcols, session_id, roles, after_ts, before_ts)
    where_sql = " AND ".join(clauses)
    count_row = connection.execute(
        f"SELECT COUNT(*) AS n FROM messages AS m WHERE {where_sql}", params).fetchone()
    total = int(count_row["n"] or 0) if count_row is not None else 0
    if meta is None and total == 0:
        raise QueryError("session_not_found", f"session {session_id!r} has no metadata or messages")
    direction = "DESC" if order == "newest" else "ASC"
    select = (f"SELECT {_message_select(mcols)} FROM messages AS m WHERE {where_sql} "
              f"ORDER BY m.id {direction} LIMIT ? OFFSET ?")
    rows = connection.execute(select, [*params, limit, offset]).fetchall()
    if order == "newest":
        rows = list(reversed(rows))
    entries = [_shape_message(row, max_chars=max_chars) for row in rows]
    data = {
        "mode": "messages",
        "db": str(path),
        "session": meta or {"id": session_id},
        "filters": {"role": roles, "after": after_ts, "before": before_ts,
                    "tool_call_id": str(args.get("tool_call_id") or "").strip() or None},
        "order": order,
        "count": len(entries),
        "total": total,
        "offset": offset,
        "has_more": total > offset + len(entries),
        "messages": entries,
    }
    if total > offset + len(entries):
        data["next_offset"] = offset + len(entries)
    return data


def _around(connection: sqlite3.Connection, args: dict[str, Any], mcols: set[str],
            scols: set[str], path: Path) -> dict[str, Any]:
    session_id = str(args.get("session_id") or "").strip()
    if not session_id:
        raise QueryError("invalid_args", "session_id is required for mode=around")
    try:
        message_id = int(args.get("message_id"))
    except (TypeError, ValueError):
        raise QueryError("invalid_args", "message_id must be an integer for mode=around") from None
    window = _int(args.get("window"), 5, 1, _MAX_WINDOW)
    max_chars = _int(args.get("max_chars"), _DEFAULT_MAX_CHARS, 0, _MAX_MESSAGE_CHARS)
    roles = _roles(args.get("role"), None)

    raw = connection.execute(
        "SELECT m.id AS message_id FROM messages AS m WHERE m.id = ? AND m.session_id = ?",
        (message_id, session_id)).fetchone()
    if raw is None:
        raise QueryError("message_not_found",
                         f"message {message_id} is not in session {session_id!r}")
    clauses, params = _message_where(args, mcols, session_id, roles, None, None)
    visible = connection.execute(
        f"SELECT 1 FROM messages AS m WHERE m.id = ? AND {' AND '.join(clauses)}",
        [message_id, *params]).fetchone()
    if visible is None:
        raise QueryError(
            "message_filtered",
            "the target message is inactive, compacted, synthetic, or filtered by role; "
            "set include_inactive/include_compacted/include_synthetic or widen role")
    base = f"FROM messages AS m WHERE {' AND '.join(clauses)}"
    before_rows = connection.execute(
        f"SELECT {_message_select(mcols)} {base} AND m.id <= ? ORDER BY m.id DESC LIMIT ?",
        [*params, message_id, window + 1]).fetchall()
    after_rows = connection.execute(
        f"SELECT {_message_select(mcols)} {base} AND m.id > ? ORDER BY m.id ASC LIMIT ?",
        [*params, message_id, window]).fetchall()
    rows = [*reversed(before_rows), *after_rows]
    entries = [_shape_message(row, max_chars=max_chars, anchor=(row["message_id"] == message_id))
               for row in rows]
    return {
        "mode": "around",
        "db": str(path),
        "session": _session_meta(connection, scols, session_id) or {"id": session_id},
        "anchor_message_id": message_id,
        "window": window,
        "messages_before": max(0, len(before_rows) - 1),
        "messages_after": len(after_rows),
        "count": len(entries),
        "messages": entries,
    }


def _stats(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    def scalar(sql: str, params: tuple = ()) -> Any:
        try:
            row = connection.execute(sql, params).fetchone()
            return row[0] if row is not None else None
        except sqlite3.Error:
            return None

    tables = _tables(connection)
    by_role = []
    try:
        by_role = [{"role": row["role"], "count": int(row["n"] or 0)} for row in
                   connection.execute("SELECT role, COUNT(*) AS n FROM messages "
                                      "GROUP BY role ORDER BY n DESC")]
    except sqlite3.Error:
        pass
    by_source = []
    if "sessions" in tables:
        try:
            by_source = [{"source": row["source"] or "", "sessions": int(row["n"] or 0)}
                         for row in connection.execute(
                             "SELECT source, COUNT(*) AS n FROM sessions "
                             "GROUP BY source ORDER BY n DESC LIMIT 20")]
        except sqlite3.Error:
            pass
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    oldest = scalar("SELECT MIN(timestamp) FROM messages")
    newest = scalar("SELECT MAX(timestamp) FROM messages")
    return {
        "mode": "stats",
        "db": str(path),
        "db_bytes": size,
        "sessions": scalar("SELECT COUNT(*) FROM sessions") if "sessions" in tables else None,
        "sessions_archived": (scalar("SELECT COUNT(*) FROM sessions WHERE archived = 1")
                              if "sessions" in tables and "archived" in _columns(connection, "sessions")
                              else None),
        "messages": scalar("SELECT COUNT(*) FROM messages"),
        "messages_active": (scalar("SELECT COUNT(*) FROM messages WHERE active = 1")
                            if "active" in _columns(connection, "messages") else None),
        "messages_compacted": (scalar("SELECT COUNT(*) FROM messages WHERE compacted = 1")
                               if "compacted" in _columns(connection, "messages") else None),
        "oldest_timestamp": _number(oldest),
        "oldest_iso": _iso(oldest),
        "newest_timestamp": _number(newest),
        "newest_iso": _iso(newest),
        "by_role": by_role,
        "by_source": by_source,
        "fts": {"messages_fts": "messages_fts" in tables,
                "messages_fts_trigram": "messages_fts_trigram" in tables},
        "schema_version": scalar("SELECT MAX(version) FROM schema_version")
        if "schema_version" in tables else None,
    }


# --------------------------------------------------------------------------- public surface


SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Query the host's durable session store ($HERMES_HOME/state.db, read-only). Five shapes, "
        "picked by `mode` or inferred from the args: `search` full-text over message content "
        "(FTS5, fallback LIKE) with role/source/time filters; `sessions` browse or filter session "
        "metadata by id/title, source, time, archived; `messages` read one session's transcript; "
        "`around` read a window around one message id; `stats` store aggregates. Returns bounded "
        "JSON with real DB rows and snippets; no model calls."),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "description": ("Query shape. Inferred when omitted: session_id+message_id -> "
                                "around, session_id -> messages, query -> search, else sessions."),
            },
            "query": {
                "type": "string",
                "description": ("search: text to find in message content. sessions: substring of "
                                "session id/title. FTS5 terms combine with `match`."),
            },
            "match": {
                "type": "string",
                "enum": list(_MATCHES),
                "description": ("search only. all (default, AND), any (OR), phrase (exact), raw "
                                "(FTS5 syntax: deploy* OR staging)."),
            },
            "index": {
                "type": "string",
                "enum": list(_INDEXES),
                "description": ("search only. auto (default), standard (messages_fts), trigram "
                                "(substring/CJK). Missing FTS falls back to LIKE."),
            },
            "session_id": {
                "type": "string",
                "description": "Session to read (messages/around) or restrict a search to.",
            },
            "message_id": {
                "type": "integer",
                "description": "Anchor message id for mode=around (use a match's message_id).",
            },
            "tool_call_id": {
                "type": "string",
                "description": ("messages/around only: one tool call's id, as shown on a "
                                "transcript event. Reads the full tool result that a skeleton "
                                "event elided; the only filter that reaches it."),
            },
            "window": {
                "type": "integer",
                "description": "around only: messages each side of the anchor (1-50, default 5).",
                "default": 5,
            },
            "role": {
                "type": "string",
                "description": ("Comma-separated roles (user,assistant,tool,system) or 'all'. "
                                "search defaults to user,assistant; messages/around default all."),
            },
            "source": {
                "type": "string",
                "description": ("Comma-separated session sources (cli, gateway, cron, ...). "
                                "Hidden sources (kanban, subagent, tool) are excluded by default."),
            },
            "platform": {
                "type": "string",
                "description": ("Defaults to the caller's platform and filters sessions.source "
                                "through the platform map. '*' disables the default filter; an "
                                "explicit value selects another platform's sources. Ignored for "
                                "an agent caller (always the current host session)."),
            },
            "profile": {
                "type": "string",
                "description": ("Read another Hermes profile's state.db instead of the active one "
                                "(default/current means the active profile)."),
            },
            "after": {
                "type": "string",
                "description": ("Inclusive lower bound: epoch seconds, ISO date/datetime, or "
                                "relative 7d/24h/2w. Search/messages use message timestamps; "
                                "sessions use started_at."),
            },
            "before": {
                "type": "string",
                "description": "Exclusive upper bound; same formats as `after`.",
            },
            "sort": {
                "type": "string",
                "enum": list(_SORTS),
                "description": ("search: relevance (default), newest, oldest. sessions: newest "
                                "(default) or oldest. messages: use `order`."),
            },
            "order": {
                "type": "string",
                "enum": list(_ORDERS),
                "description": "messages only: oldest (default) or newest; results return chronological.",
            },
            "limit": {
                "type": "integer",
                "description": ("Max rows. search/sessions default 10 (max 100); messages default "
                                "50 (max 200)."),
            },
            "offset": {
                "type": "integer",
                "description": "Rows to skip; pair with `has_more`/`next_offset`.",
            },
            "max_chars": {
                "type": "integer",
                "description": ("Per-message content cap (default 1200, max 4000; 0 omits the "
                                "message content, keeping the snippet/id)."),
            },
            "include_content": {
                "type": "boolean",
                "description": "search only: include the truncated message body (default true).",
            },
            "include_inactive": {
                "type": "boolean",
                "description": "Include inactive rows (rewind/undo and superseded compaction rows).",
            },
            "include_compacted": {
                "type": "boolean",
                "description": "Include compaction-archived rows and _compressed_summary rows.",
            },
            "include_synthetic": {
                "type": "boolean",
                "description": "Include host-authored user rows ([Assistant Rules], [System: ...]).",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include hidden session sources and sessions.hidden rows.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include sessions.archived rows (search/sessions).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def query(args: dict[str, Any], *, hermes_home_path: Any = "", state_db: Any = "",
          platform: str = "", caller: str = "host", session_id: str = "") -> dict[str, Any]:
    """Run one query shape and return the tool's ``data`` object (raises :class:`QueryError`).

    Two caller classes decide the default scope. A host caller is filtered to its own platform's
    session sources unless it passes ``source`` or ``platform="*"``. An agent caller (a plugin
    layer run) is scoped to the current host session unconditionally: the ``platform`` argument
    is ignored rather than rejected, so a model cannot widen the gate by retrying with another
    value. The applied filter is echoed in the envelope, and an empty filtered result says
    whether the database has rows at all.
    """
    if not isinstance(args, dict):
        raise QueryError("invalid_args", "arguments must be an object")
    args = dict(args)
    mode = _mode(args)
    platform_label = str(platform or "").strip() or "cli"
    agent_caller = str(caller or "host").strip() == "agent"
    host_session = str(session_id or "").strip()
    requested = str(args.get("platform") or "").strip()
    applied_sources: Optional[list[str]] = None
    if agent_caller:
        args.pop("platform", None)
        args.pop("source", None)
        if host_session and mode == "search":
            args["session_id"] = host_session
        elif host_session and mode == "sessions":
            args["query"] = host_session
    elif requested != "*" and (requested or str(platform or "").strip()):
        label = requested or platform_label
        applied_sources = platform_sources(label)
        if applied_sources and not args.get("source"):
            args["source"] = ",".join(applied_sources)
    if state_db:
        path = Path(str(state_db))
    else:
        path = state_db_path(hermes_home_path=hermes_home_path,
                             profile=args.get("profile"))
    connection = _connect(path)
    try:
        mcols = _columns(connection, "messages")
        if not {"session_id", "role", "content", "timestamp", "id"} <= mcols:
            raise QueryError("state_db_error",
                             "the state.db has no usable messages table "
                             "(session_id/role/content/timestamp/id)")
        scols = _columns(connection, "sessions")
        if mode == "search":
            data = _search(connection, args, mcols, scols, path)
        elif mode == "sessions":
            data = _sessions(connection, args, scols, path)
        elif mode == "messages":
            data = _messages(connection, args, mcols, scols, path)
        elif mode == "around":
            data = _around(connection, args, mcols, scols, path)
        else:
            data = _stats(connection, path)
        if agent_caller and host_session and mode == "sessions":
            entries = [entry for entry in data.get("sessions") or ()
                       if str(entry.get("id") or "") == host_session]
            data["sessions"] = entries
            data["count"] = len(entries)
            data["total"] = len(entries)
            data["has_more"] = False
        data["filter"] = {
            "platform": "*" if agent_caller or requested == "*" else (requested or platform_label),
            "sources": applied_sources,
            "session_id": host_session or str(args.get("session_id") or "") or None,
            "caller": "agent" if agent_caller else "host",
        }
        if not data.get("count") and applied_sources and "sessions" in _tables(connection):
            if connection.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None:
                data["hint"] = (
                    "No rows for this platform's sources (" + ", ".join(applied_sources)
                    + "). The store has rows: pass platform=\"*\" (or an explicit source) to "
                    "search every surface.")
        return data
    except sqlite3.Error as exc:
        logger.debug("session_search: state.db read failed", exc_info=True)
        raise QueryError("state_db_error", f"state.db read failed: {exc}") from exc
    finally:
        connection.close()


def _error(kind: str, message: str) -> str:
    return json.dumps({"success": False, "error": {"type": kind, "message": message}},
                      ensure_ascii=False)


def _payload(data: dict[str, Any]) -> str:
    return json.dumps({"success": True, "data": data}, ensure_ascii=False)


def handle(args: dict[str, Any], **kwargs: Any) -> str:
    """Sync tool handler — always returns a JSON string, never raises into the tool loop."""
    try:
        data = query(args or {}, hermes_home_path=kwargs.get("hermes_home") or "",
                     state_db=kwargs.get("state_db") or "",
                     platform=str(kwargs.get("platform") or ""),
                     caller=str(kwargs.get("caller") or "host"),
                     session_id=str(kwargs.get("session_id") or ""))
    except QueryError as exc:
        return _error(exc.kind, exc.message)
    except Exception as exc:  # BLE001 -- reported as JSON, never raised into the loop
        logger.exception("minir_hermes_session_search failed")
        return _error("session_search_failed", str(exc)[:500])
    return _payload(data)
