"""SQLite storage for the Minir memory plugin — stdlib only.

Three tables:

* ``turns`` — completed conversation turns, one row per user/assistant message,
  kept per ``session_id`` so recall can cite which session something came from.
* ``notes`` — explicit memories: ``minir_remember`` tool calls and mirrored
  built-in memory-tool writes (``MemoryProvider.on_memory_write``).
* ``jobs`` — one row per layer job the turn counter queued (see ``turn_counter.py``):
  the session and half-open time window ``(from_ts, to_ts]`` the job must reason over.
  Rows land as ``pending``; the worker that consumes them is not part of this template.

``search()`` is a portable "match any query token" scan over the ``turns`` and
``notes`` tables — good
enough for a template and dependency-free. Swap in SQLite FTS5, embeddings, or
a remote index when you outgrow it; only ``search()`` and the write methods are
part of the plugin's internal contract.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_MAX_CONTENT = 4000  # stored text is truncated to this many chars
_MAX_TERMS = 8       # query tokens considered per search
_MAX_SCAN = 200      # candidate rows fetched per table before scoring
_EXCERPT = 240       # search-result text length

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at DESC);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'tool',
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at DESC);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    from_ts REAL NOT NULL,
    to_ts REAL NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
"""


def _terms(query: str) -> list[str]:
    """Query -> lowercase search tokens (deduped, order kept, capped at _MAX_TERMS)."""
    tokens = re.findall(r"[\w-]+", (query or "").lower(), flags=re.UNICODE)
    seen: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token not in seen:
            seen.append(token)
        if len(seen) >= _MAX_TERMS:
            break
    return seen


def _excerpt(content: str) -> str:
    flat = " ".join(content.split())
    return flat if len(flat) <= _EXCERPT else flat[:_EXCERPT] + "…"


class MinirStore:
    """Thread-safe SQLite store.

    The host runs ``sync_turn`` and ``prefetch`` on different threads, so one
    connection (``check_same_thread=False``) is shared under an RLock and WAL
    mode keeps readers from blocking the writer.
    """

    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writes ---------------------------------------------------------------

    def add_turn(self, session_id: str, role: str, content: str,
                 author: str = "", created_at: Optional[float] = None) -> int:
        """Store one message; returns its row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO turns (session_id, role, content, author, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content[:_MAX_CONTENT], author, time.time() if created_at is None else created_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def remember(self, content: str, source: str = "tool") -> int:
        """Store an explicit note; returns its row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notes (source, content, created_at) VALUES (?, ?, ?)",
                (source, content[:_MAX_CONTENT], time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def forget(self, note_id: int) -> bool:
        """Delete a note by id; False when no such note exists."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
            self._conn.commit()
            return cur.rowcount > 0

    def forget_by_content(self, content: str) -> int:
        """Delete notes whose stored text equals *content*; returns rows deleted."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM notes WHERE content = ?", (content[:_MAX_CONTENT],))
            self._conn.commit()
            return int(cur.rowcount)

    def record_job(self, layer: str, session_id: str, from_ts: float, to_ts: float,
                   turns: int = 0, *, status: str = "pending") -> int:
        """Queue one layer job (session + reasoning window); returns its row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (layer, session_id, from_ts, to_ts, turns, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (layer, session_id, float(from_ts), float(to_ts), int(turns), status, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Oldest-first queued jobs, the handoff a layer worker consumes."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, layer, session_id, from_ts, to_ts, turns, status, created_at FROM jobs "
                "WHERE status = 'pending' ORDER BY id LIMIT ?", (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- reads ----------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Ranked matches across turns and notes.

        Scoring: number of distinct query tokens present in the row, then
        recency. Returns up to *limit* dicts with
        ``{kind, id, text, when, role, session_id, author, score}``.
        """
        terms = _terms(query)
        if not terms:
            return []
        where = " OR ".join("lower(content) LIKE ?" for _ in terms)
        params = [f"%{t}%" for t in terms]
        with self._lock:
            turn_rows = self._conn.execute(
                f"SELECT id, session_id, role, content, author, created_at FROM turns "
                f"WHERE {where} ORDER BY created_at DESC LIMIT {_MAX_SCAN}", params,
            ).fetchall()
            note_rows = self._conn.execute(
                f"SELECT id, source, content, created_at FROM notes "
                f"WHERE {where} ORDER BY created_at DESC LIMIT {_MAX_SCAN}", params,
            ).fetchall()

        rows: list[dict[str, Any]] = []
        for row in turn_rows:
            rows.append({
                "kind": "turn", "id": row["id"], "text": row["content"],
                "when": row["created_at"], "role": row["role"],
                "session_id": row["session_id"], "author": row["author"],
            })
        for row in note_rows:
            rows.append({
                "kind": "note", "id": row["id"], "text": row["content"],
                "when": row["created_at"], "role": "", "session_id": "",
                "author": row["source"],
            })
        for row in rows:
            lowered = row["text"].lower()
            row["score"] = sum(1 for term in terms if term in lowered)
        rows.sort(key=lambda r: (r["score"], r["when"], r["id"]), reverse=True)
        return rows[:max(1, min(int(limit), 20))]

    def stats(self) -> dict[str, Any]:
        """Row counts and on-disk size, for the ``minir_stats`` tool."""
        with self._lock:
            turns = self._conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
            notes = self._conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
            sessions = self._conn.execute("SELECT COUNT(DISTINCT session_id) AS n FROM turns").fetchone()["n"]
            jobs = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'").fetchone()["n"]
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {"turns": int(turns), "notes": int(notes), "sessions": int(sessions),
                "jobs": int(jobs), "pending_jobs": int(pending),
                "db_path": str(self.path), "db_bytes": size}

    def close(self) -> None:
        """Close the connection; safe to call twice."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]


def format_when(created_at: float) -> str:
    """Epoch seconds -> compact UTC timestamp for recall blocks (``2026-10-16 11:04``)."""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(created_at))
