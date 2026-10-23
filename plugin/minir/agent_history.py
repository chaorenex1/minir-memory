"""``minir_aiagent_search`` — a plugin agent reading its own transcript stream.

The tool takes no layer argument on purpose. The layer comes from a ContextVar that
``agent_loop.run`` sets for the duration of one peer run, so a model cannot widen its own scope by
passing a name: an unbound caller (the host agent, a bare test) gets an error instead of every
layer. ``profile`` is the provider's partition key; cross-profile reads do not exist.

Shapes mirror the buffer's read parameters, not six files:

* ``messages`` — one stream's rows, paged or incremental (``cursor``) or rolling (``last_n``);
* ``search``   — substring search over ``text``/``result`` in the caller's layer;
* ``around``   — a window around one ``seq``;
* ``stats``    — what the layer's streams hold, and the file bytes under them.

Every answer echoes ``coverage`` (the seq range the cache holds), ``source``
(``buffer``/``disk``/``mixed``) and ``dropped_before`` — without them "no such row" and "not
cached" look identical.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any, Optional

from .transcript import KINDS, ROLES

TOOL_NAME = "minir_aiagent_search"
TOOLSET = "minir"

#: Bound by ``agent_loop.run(layer=...)`` around one peer run. An agent that starts its own
#: child (``minir_agents``) overwrites it for the child and restores it afterwards.
CURRENT_LAYER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "minir_agent_layer", default="")

#: The stream session this run belongs to when that differs from the provider's host session
#: (an adviser reviewer writes ``<session>--<name>``; a custom agent keeps the caller's id).
CURRENT_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "minir_agent_session", default="")

_MODES = ("auto", "messages", "search", "around", "stats")
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_CHARS = 4_000


def current_layer() -> str:
    return str(CURRENT_LAYER.get() or "")


def layer_context(layer: str, session: str = "") -> tuple[contextvars.Token, contextvars.Token]:
    """Bind the caller's layer (and its stream session) for one run."""
    return CURRENT_LAYER.set(str(layer or "")), CURRENT_SESSION.set(str(session or ""))


def reset_layer(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    layer_token, session_token = tokens
    CURRENT_SESSION.reset(session_token)
    CURRENT_LAYER.reset(layer_token)


def current_session() -> str:
    return str(CURRENT_SESSION.get() or "")


class AgentHistoryError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Read your own layer's transcript: user/system/assistant/tool/reasoning events kept as "
        "JSONL, memory-first, with disk fallback. `messages` reads a stream (paged, cursor or "
        "last_n), `search` greps text and tool results, `around` reads a window around one seq, "
        "`stats` reports what this layer holds. Layer and profile are fixed by the caller; a "
        "result always says which range it covers (`coverage`) and where it came from (`source`)."),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(_MODES),
                     "description": ("Query shape. Inferred when omitted: query -> search, "
                                     "seq -> around, else messages.")},
            "query": {"type": "string",
                      "description": "search: all terms must appear in the event text or result."},
            "scope": {"type": "string", "enum": ["session", "layer"],
                      "description": ("session (default): this session only. layer: every "
                                      "session this layer owns on this profile.")},
            "session_id": {"type": "string",
                           "description": "Own-layer session to read; defaults to yours."},
            "seq": {"type": "integer", "description": "around: anchor event sequence number."},
            "window": {"type": "integer", "default": 5,
                       "description": "around: events each side of the anchor (1-50)."},
            "cursor": {"type": "integer",
                       "description": "Incremental read: return rows with seq > cursor."},
            "last_n": {"type": "integer", "description": "Rolling read: the last N rows."},
            "since": {"type": "number", "description": "Inclusive lower bound, epoch seconds."},
            "until": {"type": "number", "description": "Exclusive upper bound, epoch seconds."},
            "turn": {"type": "integer", "description": "Only this host turn (0 = every turn)."},
            "step": {"type": "integer", "description": "Only this assistant step."},
            "role": {"type": "string",
                     "description": ("Comma-separated roles: " + ",".join(ROLES)
                                     + ", or 'all' (default).")},
            "kind": {"type": "string",
                     "description": "Comma-separated kinds: " + ",".join(KINDS) + " (default msg)."},
            "limit": {"type": "integer",
                      "description": f"Page size, default {_DEFAULT_LIMIT}, max {_MAX_LIMIT}."},
            "page": {"type": "integer", "description": "Zero-based page index."},
            "max_chars": {"type": "integer",
                          "description": f"Per-event body cap (default {_MAX_CHARS})."},
        },
        "required": [],
        "additionalProperties": False,
    },
}


def _bool_args(args: dict[str, Any]) -> tuple[Optional[list[str]], Optional[list[str]]]:
    roles_raw = str(args.get("role") or "").strip()
    kinds_raw = str(args.get("kind") or "").strip() or "msg"
    roles = None if not roles_raw or roles_raw.lower() == "all" else [
        part.strip() for part in roles_raw.split(",") if part.strip()]
    kinds = None if kinds_raw.lower() == "all" else [
        part.strip() for part in kinds_raw.split(",") if part.strip()]
    return roles, kinds


def _mode(args: dict[str, Any]) -> str:
    mode = str(args.get("mode") or "auto").strip().lower()
    if mode not in _MODES:
        raise AgentHistoryError("invalid_args", f"mode must be one of: {', '.join(_MODES)}")
    if mode != "auto":
        return mode
    if args.get("seq") is not None:
        return "around"
    if str(args.get("query") or "").strip():
        return "search"
    return "messages"


def _int(value: Any, default: int, *, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        if value is None:
            return default
        raise AgentHistoryError("invalid_args", f"{name} must be an integer") from exc
    if number < low or number > high:
        raise AgentHistoryError("invalid_args", f"{name} must be between {low} and {high}")
    return number


def _clip(row: dict[str, Any], limit: int) -> dict[str, Any]:
    out = dict(row)
    for key in ("text", "result"):
        body = str(out.get(key) or "")
        if len(body) > limit:
            out[key] = body[:limit] + f"… (elided, {len(body)} chars)"
    return out


def query(args: dict[str, Any], *, buffer: Any, profile: str, layer: str,
          session_id: str = "") -> dict[str, Any]:
    """Run one shape against the caller's layer; raises :class:`AgentHistoryError`."""
    if not layer:
        raise AgentHistoryError(
            "layer_unbound",
            "minir_aiagent_search is only available inside a minir agent run; no layer is bound")
    if buffer is None:
        raise AgentHistoryError("unavailable", "the transcript buffer is not initialized")
    if not isinstance(args, dict):
        raise AgentHistoryError("invalid_args", "arguments must be an object")
    mode = _mode(args)
    scope = str(args.get("scope") or "session").strip().lower()
    if scope not in {"session", "layer"}:
        raise AgentHistoryError("invalid_args", "scope must be 'session' or 'layer'")
    if mode == "stats":
        stats = buffer.stats(profile=profile, layer=layer)
        stats.update({"layer": layer, "profile": profile})
        return stats
    sessions = [str(args.get("session_id") or session_id or "")]
    if not sessions[0]:
        raise AgentHistoryError("invalid_args", "session_id is required when the caller has none")
    if scope == "layer":
        sessions = [row["session_id"] for row in
                    buffer.stats(profile=profile, layer=layer)["streams"]]
        if session_id and session_id not in sessions:
            sessions.insert(0, session_id)
        if not sessions:
            return {"rows": [], "coverage": [0, 0], "source": "disk", "dropped_before": 0,
                    "truncated": False, "layer": layer, "profile": profile,
                    "sessions": [], "mode": mode}
    limit = _int(args.get("limit"), _DEFAULT_LIMIT, low=1, high=_MAX_LIMIT, name="limit")
    max_chars = _int(args.get("max_chars"), _MAX_CHARS, low=0, high=100_000, name="max_chars")
    roles, kinds = _bool_args(args)
    rows: list[dict[str, Any]] = []
    sources: list[str] = []
    coverages: list[list[int]] = []
    dropped: list[int] = []
    truncated = False
    for session in sessions:
        if mode == "around":
            anchor = _int(args.get("seq"), 0, low=0, high=2 ** 62, name="seq")
            distance = _int(args.get("window"), 5, low=1, high=50, name="window")
            result = buffer.read_events(profile, layer, session,
                                        cursor=max(0, anchor - distance - 1),
                                        limit=distance * 2 + 1,
                                        roles=roles, kinds=kinds)
        elif mode == "search":
            # One part's worth of events, newest first: a search must not materialize a whole
            # permanent stream of multi-megabyte tool results. ``dropped_before`` says the scan
            # was bounded when it was.
            result = buffer.read_events(profile, layer, session, roles=roles, kinds=kinds,
                                        last_n=2_000)
        else:
            turn = args.get("turn")
            if str(turn or "").strip() in {"", "0"}:
                turn = None  # 0 means "every turn" in the schema
            result = buffer.read_events(
                profile, layer, session, cursor=args.get("cursor"),
                last_n=args.get("last_n"), since=args.get("since"), until=args.get("until"),
                turn=turn, step=args.get("step"),
                roles=roles, kinds=kinds, limit=args.get("limit"), page=args.get("page"))
        page_rows = list(result.get("rows") or ())
        if mode == "search":
            terms = [term for term in str(args.get("query") or "").casefold().split() if term]
            page_rows = [row for row in page_rows
                         if any(term in (str(row.get("text") or "") + " "
                                         + str(row.get("result") or "")).casefold()
                                for term in terms)]
            page_rows = page_rows[:limit]
        rows.extend(_clip(row, max_chars) if max_chars else
                    {key: value for key, value in row.items() if key not in ("text", "result")}
                    for row in page_rows)
        sources.append(str(result.get("source") or "disk"))
        coverages.append(list(result.get("coverage") or (0, 0)))
        if result.get("dropped_before"):
            dropped.append(int(result["dropped_before"]))
        truncated = truncated or bool(result.get("truncated"))
    source = ("buffer" if sources and all(item == "buffer" for item in sources)
              else "mixed" if any(item == "buffer" for item in sources) else "disk")
    coverage = ([min(item[0] for item in coverages), max(item[1] for item in coverages)]
                if coverages else [0, 0])
    return {"mode": mode, "layer": layer, "profile": profile, "sessions": sessions,
            "rows": rows, "count": len(rows), "coverage": coverage, "source": source,
            "dropped_before": max(dropped) if dropped else 0, "truncated": truncated}


def handle(args: dict[str, Any], *, provider: Any = None, **_kwargs: Any) -> str:
    """Tool handler: always a JSON string, never raises into the tool loop."""
    transcript = getattr(provider, "_transcript", None) if provider is not None else None
    profile = str(getattr(provider, "_profile", "") or "default") if provider is not None else "default"
    session = str(getattr(provider, "_session_id", "") or "") if provider is not None else ""
    try:
        data = query(args or {}, buffer=transcript, profile=profile, layer=current_layer(),
                     session_id=current_session() or session)
    except AgentHistoryError as exc:
        return json.dumps({"success": False, "error": {"type": exc.kind,
                                                       "message": exc.message}},
                          ensure_ascii=False)
    except Exception as exc:  # BLE001 -- reported as JSON, never raised into the loop
        return json.dumps({"success": False, "error": {"type": "agent_history_failed",
                                                       "message": str(exc)[:500]}},
                          ensure_ascii=False)
    return json.dumps({"success": True, "data": data}, ensure_ascii=False)
