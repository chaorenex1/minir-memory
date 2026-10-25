"""Read-only access to the host's durable conversation store.

The layer worker reasons over a ``(from_ts, to_ts]`` window of one session. The host already
keeps that history verbatim in ``$HERMES_HOME/state.db`` (``messages.timestamp`` is REAL epoch
seconds), so a job can be drained after a restart — unlike the in-memory ``TranscriptBuffer``,
which only covers content produced by this process.

Two host details matter for correctness:

* only ``active = 1`` rows are current; compaction marks superseded rows inactive;
* ``role = 'user'`` also contains host-synthesized rows — ``[Assistant Rules]`` skill prompts,
  background-process notices, ``[System: …]`` switches and compaction handoffs. This plugin's
  own ``[minir summary …]``, ``[minir memory notice]`` and adviser-mode blocks are filtered
  role-agnostically; they are derived context, never evidence for a later layer window. The
  adviser filter also strips a block appended to a tool result, not just a whole row.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 400
_MAX_IDENTITY_CHARS = 4_000
SYNTHETIC_PREFIXES = (
    "[Assistant Rules]",
    "[CONTEXT COMPACTION",
    "[IMPORTANT: Background process",
    "[System:",
    "[Your active task list",
    "[minir fold",
    "[WARN]",
)
# Our own derived rows: the compression summary is an assistant message by design, so the
# role-specific host filter above would let it recurse into the next layer window.
MINIR_SYNTHETIC_PREFIXES = (
    "[minir summary",
    "[minir memory notice",
    "[minir adviser",
    "[adviser — advisory only]",
    "[adviser-only mode",
)

# Adviser mode injects a block into the request (and, on some hosts, the persisted api_content).
# It is advice, not evidence; strip it even when it was appended to a tool result rather than
# written as its own row.
_ADVISER_BLOCKS = (
    ("[minir adviser review", "[end minir adviser review]"),
    ("[minir adviser — advisory only]", "[end minir adviser]"),
    ("[adviser — advisory only]", "[end adviser]"),
    ("[adviser-only mode", "[end adviser-only mode]"),
)


def strip_adviser_blocks(text: str) -> str:
    """Remove adviser-mode/review blocks from message content before it becomes layer evidence."""
    cleaned = text or ""
    for start_marker, end_marker in _ADVISER_BLOCKS:
        while start_marker in cleaned:
            start = cleaned.find(start_marker)
            end = cleaned.find(end_marker, start)
            if end < 0:
                cleaned = cleaned[:start].rstrip()
                break
            cleaned = (cleaned[:start] + cleaned[end + len(end_marker):]).strip()
    return cleaned


_PROFILE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def state_db_path(hermes_home: Any) -> Optional[Path]:
    home = str(hermes_home or "").strip()
    return Path(home) / "state.db" if home else None


def _connect(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        logger.debug("Minir history open failed: %s", exc)
        return None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def is_synthetic_user_text(text: str) -> bool:
    """True for host-authored rows that merely ride in the ``user`` role."""
    head = (text or "").lstrip()
    return any(head.startswith(prefix) for prefix in SYNTHETIC_PREFIXES)


def is_minir_synthetic_text(text: str) -> bool:
    """True for rows this plugin wrote as derived context (summary / first-fold notice)."""
    head = (text or "").lstrip()
    return any(head.startswith(prefix) for prefix in MINIR_SYNTHETIC_PREFIXES)


def read_messages(state_db: Any, session_id: str, from_ts: float, to_ts: float,
                  *, limit: int = _MAX_MESSAGES,
                  roles: tuple[str, ...] = ("user", "assistant")) -> list[dict[str, Any]]:
    """Messages of *session_id* in ``(from_ts, to_ts]``, oldest first; ``[]`` on any problem.

    ``roles`` lets the dialectic reviewer include tool calls/results; the base layer keeps the
    default because its cards model the user/assistant exchange only.
    """
    path = Path(str(state_db)) if state_db else None
    if path is None or not session_id:
        return []
    connection = _connect(path)
    if connection is None:
        return []
    try:
        columns = _columns(connection, "messages")
        if not {"session_id", "role", "content", "timestamp"} <= columns:
            return []
        wanted = tuple(str(role) for role in roles if str(role).strip()) or ("user", "assistant")
        marks = ",".join("?" * len(wanted))
        clauses = ["session_id = ?", "timestamp > ?", "timestamp <= ?",
                   f"role IN ({marks})",
                   "content IS NOT NULL", "TRIM(content) <> ''"]
        if "active" in columns:
            clauses.append("COALESCE(active, 1) = 1")
        if "_compressed_summary" in columns:
            clauses.append("COALESCE(_compressed_summary, 0) = 0")
        # Newest-first then reverse: a long boundary window must keep the newest evidence,
        # which is what the layer is meant to model, not the oldest 400 rows.
        sql = ("SELECT role, content, timestamp, "
               + ("message_uid, " if "message_uid" in columns else "NULL AS message_uid, ")
               + ("tool_name, " if "tool_name" in columns else "NULL AS tool_name, ")
               + "id FROM messages WHERE " + " AND ".join(clauses)
               + " ORDER BY id DESC LIMIT ?")
        rows = connection.execute(
            sql, (str(session_id), float(from_ts), float(to_ts), *wanted,
                  max(1, int(limit)))).fetchall()
        rows = list(reversed(rows))
    except sqlite3.Error as exc:
        logger.debug("Minir history read failed: %s", exc)
        return []
    finally:
        connection.close()

    messages: list[dict[str, Any]] = []
    for row in rows:
        role = str(row["role"] or "")
        text = strip_adviser_blocks(str(row["content"] or "").strip())
        if not text:
            continue
        if role == "user" and is_synthetic_user_text(text):
            continue
        if is_minir_synthetic_text(text):
            continue
        messages.append({
            "role": role,
            "content": text,
            "created_at": float(row["timestamp"] or 0.0),
            "message_uid": str(row["message_uid"] or ""),
            "tool_name": str(row["tool_name"] or ""),
            "author": "",
            "author_id": "",
        })
    return messages


def latest_ts(state_db: Any, session_id: str) -> float:
    """Newest active, non-synthetic message timestamp for a session (0.0 when unknown)."""
    path = Path(str(state_db)) if state_db else None
    if path is None or not session_id:
        return 0.0
    connection = _connect(path)
    if connection is None:
        return 0.0
    try:
        columns = _columns(connection, "messages")
        if not {"session_id", "timestamp"} <= columns:
            return 0.0
        clauses = ["session_id = ?", "role IN ('user', 'assistant')"]
        if "active" in columns:
            clauses.append("COALESCE(active, 1) = 1")
        if "_compressed_summary" in columns:
            clauses.append("COALESCE(_compressed_summary, 0) = 0")
        # A synthetic user row can be the newest row; walk the tail and apply the same filter
        # as read_messages instead of letting it extend the boundary window.
        rows = connection.execute(
            "SELECT role, content, timestamp FROM messages WHERE " + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT 100", (str(session_id),)).fetchall()
        for row in rows:
            text = strip_adviser_blocks(str(row["content"] or ""))
            if not text:
                continue
            if row["role"] == "user" and is_synthetic_user_text(text):
                continue
            if is_minir_synthetic_text(text):
                continue
            return float(row["timestamp"] or 0.0)
        return 0.0
    except sqlite3.Error:
        return 0.0
    finally:
        connection.close()


def profile_name(state_db: Any, session_id: str) -> str:
    """``sessions.profile_name`` for a session, or ``""`` (single-profile installs include it)."""
    path = Path(str(state_db)) if state_db else None
    if path is None or not session_id:
        return ""
    connection = _connect(path)
    if connection is None:
        return ""
    try:
        columns = _columns(connection, "sessions")
        if not {"id", "profile_name"} <= columns:
            return ""
        row = connection.execute(
            "SELECT profile_name FROM sessions WHERE id = ?", (str(session_id),)).fetchone()
        return str(row["profile_name"] or "").strip() if row is not None else ""
    except sqlite3.Error:
        return ""
    finally:
        connection.close()


def session_totals(state_db: Any, session_id: str) -> dict[str, Any]:
    """The host's own final token counters for one session (``{}`` when unavailable).

    ``sessions.input_tokens``/``output_tokens`` are the provider-confirmed aggregates the host
    updates after every API call; at a session boundary they are the authoritative total, even
    for calls this plugin never observed. ``cache_read_tokens`` is a subset of the input count,
    not an extra bucket.
    """
    path = Path(str(state_db)) if state_db else None
    if path is None or not session_id:
        return {}
    connection = _connect(path)
    if connection is None:
        return {}
    try:
        columns = _columns(connection, "sessions")
        if not {"id", "input_tokens", "output_tokens"} <= columns:
            return {}
        wanted = [name for name in ("input_tokens", "output_tokens", "cache_read_tokens",
                                    "cache_write_tokens", "reasoning_tokens", "api_call_count",
                                    "message_count")
                  if name in columns]
        row = connection.execute(
            "SELECT " + ", ".join(wanted) + " FROM sessions WHERE id = ?",
            (str(session_id),)).fetchone()
        if row is None:
            return {}
        totals = {name: int(row[name] or 0) for name in wanted}
        totals["total_tokens"] = totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
        return totals
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def identity_paths(hermes_home: Any, profile: str) -> tuple[Path, Path]:
    """``$HERMES_HOME/minir-memory/agents/<profile>/{identity,preference}.md``."""
    safe = _PROFILE_RE.sub("_", str(profile or "default").strip()).strip("._") or "default"
    base = Path(str(hermes_home or "")) / "minir-memory" / "agents" / safe
    return base / "identity.md", base / "preference.md"


def _card_body(text: str) -> str:
    """A card file's markdown body: front matter is metadata, never prompt text."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((index for index, line in enumerate(lines[1:], 1)
                    if line.strip() == "---"), None)
        if end is not None:
            return "\n".join(lines[end + 1:]).strip()
    return text.strip()


def load_identity(hermes_home: Any, profile: str) -> tuple[tuple[dict[str, Any], str],
                                                          tuple[dict[str, Any], str]]:
    """Read the identity/preference files as ``(meta, body)`` pairs; ``({}, "")`` when absent.

    The caller needs the recorded ``source`` to decide whether a card may be presented as an
    operator declaration, and it must never see the front matter as identity text.
    """
    empty: tuple[dict[str, Any], str] = ({}, "")
    if not str(hermes_home or "").strip():
        return empty, empty
    from . import card_review  # local import keeps this module stdlib-only at load time

    identity_path, preference_path = identity_paths(hermes_home, profile)
    out: list[tuple[dict[str, Any], str]] = []
    for path in (identity_path, preference_path):
        meta, body = card_review.read_card(path)
        out.append((dict(meta), str(body or "")[:_MAX_IDENTITY_CHARS]))
    return out[0], out[1]
