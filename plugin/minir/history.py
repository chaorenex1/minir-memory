"""Read-only access to the host's durable conversation store.

The layer worker reasons over a ``(from_ts, to_ts]`` window of one session. The host already
keeps that history verbatim in ``$HERMES_HOME/state.db`` (``messages.timestamp`` is REAL epoch
seconds), so a job can be drained after a restart — unlike the in-memory ``TranscriptBuffer``,
which only covers content produced by this process.

Two host details matter for correctness:

* only ``active = 1`` rows are current; compaction marks superseded rows inactive;
* ``role = 'user'`` also contains host-synthesized rows — ``[Assistant Rules]`` skill prompts,
  background-process notices, ``[System: …]`` switches, compaction handoffs and this plugin's
  own ``[minir fold …]`` carriers. They are not user intent and must not reach the base layer.
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


def read_messages(state_db: Any, session_id: str, from_ts: float, to_ts: float,
                  *, limit: int = _MAX_MESSAGES) -> list[dict[str, Any]]:
    """Messages of *session_id* in ``(from_ts, to_ts]``, oldest first; ``[]`` on any problem."""
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
        clauses = ["session_id = ?", "timestamp > ?", "timestamp <= ?",
                   "role IN ('user', 'assistant')",
                   "content IS NOT NULL", "TRIM(content) <> ''"]
        if "active" in columns:
            clauses.append("COALESCE(active, 1) = 1")
        if "_compressed_summary" in columns:
            clauses.append("COALESCE(_compressed_summary, 0) = 0")
        # Newest-first then reverse: a long boundary window must keep the newest evidence,
        # which is what the layer is meant to model, not the oldest 400 rows.
        sql = ("SELECT role, content, timestamp, "
               + ("message_uid, " if "message_uid" in columns else "NULL AS message_uid, ")
               + "id FROM messages WHERE " + " AND ".join(clauses)
               + " ORDER BY id DESC LIMIT ?")
        rows = connection.execute(
            sql, (str(session_id), float(from_ts), float(to_ts), max(1, int(limit)))).fetchall()
        rows = list(reversed(rows))
    except sqlite3.Error as exc:
        logger.debug("Minir history read failed: %s", exc)
        return []
    finally:
        connection.close()

    messages: list[dict[str, Any]] = []
    for row in rows:
        role = str(row["role"] or "")
        text = str(row["content"] or "").strip()
        if not text:
            continue
        if role == "user" and is_synthetic_user_text(text):
            continue
        messages.append({
            "role": role,
            "content": text,
            "created_at": float(row["timestamp"] or 0.0),
            "message_uid": str(row["message_uid"] or ""),
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
            if row["role"] == "user" and is_synthetic_user_text(str(row["content"] or "")):
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


def identity_paths(hermes_home: Any, profile: str) -> tuple[Path, Path]:
    """``$HERMES_HOME/minir-memory/agents/<profile>/{identity,preference}.md``."""
    safe = _PROFILE_RE.sub("_", str(profile or "default").strip()).strip("._") or "default"
    base = Path(str(hermes_home or "")) / "minir-memory" / "agents" / safe
    return base / "identity.md", base / "preference.md"


def load_identity(hermes_home: Any, profile: str) -> tuple[str, str]:
    """Read the operator-authored identity/preference files; ``("", "")`` when absent."""
    if not str(hermes_home or "").strip():
        return "", ""
    identity_path, preference_path = identity_paths(hermes_home, profile)
    texts: list[str] = []
    for path in (identity_path, preference_path):
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            text = ""
        texts.append(text[:_MAX_IDENTITY_CHARS])
    return texts[0], texts[1]
