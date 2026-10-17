"""SQLite storage for the Minir memory plugin — stdlib only.

**No transcripts are stored.** The provider reads the host's conversation history read-only (see
``history.py``) and persists only what it derived from those turns:

* ``notes`` — explicit memories: ``minir_remember`` tool calls and mirrored built-in
  memory-tool writes (``MemoryProvider.on_memory_write``). These are deliberate writes, not a
  transcript.
* ``jobs`` — one row per layer job the turn counter queued (see ``turn_counter.py``): the
  session and half-open time window ``(from_ts, to_ts]`` the job reasons over, plus the worker's
  claim/outcome state. ``processed_ts`` is the content watermark the worker advances when it
  finishes a job: the next job of that layer/session starts there, not at ``from_ts``, so a
  window whose content landed *after* its dispatch stamp is still reasoned over exactly once.
  A job that outlives the process that queued it has no content left to read — the worker
  expires it (``expire_open_jobs``) instead of pretending to reason over it.
* ``layer_state`` — the merged artifact each layer publishes, one row per ``(layer, subject)``
  with a monotonically increasing ``version`` (see ``worker.py``).
* ``base_refreshes`` — one row per base-layer refresh *window*: the six structured outputs as
  JSON text (user representation, user peer card, AI self representation, AI identity card,
  local window summary, value markers) plus the session, time range and the turn range actually
  covered. ``is_latest`` marks the row with the greatest ``window_to_ts`` per
  ``(session_id, subject)``, not the row written last, so an older window that finishes late
  cannot become "latest"; the live row is a single indexed lookup while older windows stay
  queryable as history. ``base_refreshes_fts`` is an external-content FTS5 trigram index over
  the six outputs; short CJK queries fall back to a LIKE scan. The outputs are model-derived and
  may quote the messages they reasoned over even though the provider never copies the
  transcript itself.
* ``base_revisions`` — the pre-overwrite snapshot of a base refresh when the dialectic layer
  corrects it: the same six fields plus the advices that justified the correction, the changed
  field list, the affected refresh id and an ``is_latest`` flag. ``base_revisions_fts`` indexes
  the seven content fields (six snapshots + advices) with the same trigram design.
* ``advisories`` — the delivery ledger: normalized, deduped advices with severity/target and
  a ``pending``/``injected`` state. The revision table keeps the immutable audit copy; this
  table is what ``pre_llm_call`` drains.
* ``conclusions`` — every durable result split into individually searchable rows: notes, the
  six base-layer sections, the dialectic review, local
  compression chunks, and final session summaries. ``conclusions_fts`` is an FTS5 trigram index
  over it (CJK-safe); when FTS5 is unavailable, ``search()`` falls back to the token scan.
  ``session_chunk_summary`` is the canonical local-compression chain; each row records the time
  and session-turn range it covers. The base artifact's ``## Session Summary`` projects only the
  newest active chunk; older (including superseded) chunks stay as independent rows and remain
  searchable inside their own session.
* ``metrics`` — aggregated tool counters, flushed in batches rather than per call.

``search()`` enforces session isolation: session summaries, reviews, current-focus statements
and handoffs only match the session that produced them. User peer cards, AI identity, brain
entries and notes are durable and recall across sessions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_CONTENT = 4000  # stored text is truncated to this many chars
_MAX_TERMS = 8       # query tokens considered per search
_MAX_SCAN = 200      # candidate rows fetched per table before scoring
_EXCERPT = 240       # search-result text length

_SCHEMA = """
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
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_at REAL,
    finished_at REAL,
    error TEXT NOT NULL DEFAULT '',
    processed_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
CREATE TABLE IF NOT EXISTS compression_gates (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS layer_state (
    layer TEXT NOT NULL,
    subject TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (layer, subject)
);
CREATE INDEX IF NOT EXISTS idx_layer_state_updated ON layer_state(layer, updated_at DESC);
CREATE TABLE IF NOT EXISTS conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    from_ts REAL,
    to_ts REAL,
    from_turn INTEGER,
    to_turn INTEGER,
    version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(kind, key)
);
CREATE INDEX IF NOT EXISTS idx_conclusions_session ON conclusions(session_id, kind);
CREATE INDEX IF NOT EXISTS idx_conclusions_kind ON conclusions(kind, updated_at DESC);
CREATE TABLE IF NOT EXISTS base_refreshes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    window_from_ts REAL NOT NULL,
    window_to_ts REAL NOT NULL,
    window_from_turn INTEGER,
    window_to_turn INTEGER,
    user_representation TEXT NOT NULL DEFAULT '',
    user_peer_card TEXT NOT NULL DEFAULT '',
    ai_self_representation TEXT NOT NULL DEFAULT '',
    ai_identity_card TEXT NOT NULL DEFAULT '',
    session_chunk_summary TEXT NOT NULL DEFAULT '',
    value_markers TEXT NOT NULL DEFAULT '',
    is_latest INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_base_refreshes_session
    ON base_refreshes(session_id, subject, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_base_refreshes_window
    ON base_refreshes(session_id, subject, window_from_ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_base_refreshes_latest
    ON base_refreshes(session_id, subject) WHERE is_latest = 1;
CREATE TABLE IF NOT EXISTS base_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    base_refresh_id INTEGER NOT NULL DEFAULT 0,
    window_from_ts REAL NOT NULL,
    window_to_ts REAL NOT NULL,
    window_from_turn INTEGER,
    window_to_turn INTEGER,
    user_representation TEXT NOT NULL DEFAULT '',
    user_peer_card TEXT NOT NULL DEFAULT '',
    ai_self_representation TEXT NOT NULL DEFAULT '',
    ai_identity_card TEXT NOT NULL DEFAULT '',
    session_chunk_summary TEXT NOT NULL DEFAULT '',
    value_markers TEXT NOT NULL DEFAULT '',
    advices TEXT NOT NULL DEFAULT '[]',
    changed_fields TEXT NOT NULL DEFAULT '[]',
    source_job_id INTEGER NOT NULL DEFAULT 0,
    is_latest INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_base_revisions_session
    ON base_revisions(session_id, subject, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_base_revisions_latest
    ON base_revisions(session_id, subject) WHERE is_latest = 1;
CREATE TABLE IF NOT EXISTS advisories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'dialectic',
    severity TEXT NOT NULL DEFAULT 'concern',
    target TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    injected_at REAL,
    turn_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_advisories_pending
    ON advisories(session_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_advisories_fingerprint
    ON advisories(session_id, fingerprint);
CREATE TABLE IF NOT EXISTS metrics (
    tool TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    total_ms REAL NOT NULL DEFAULT 0,
    max_ms REAL NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""

# Columns added after the first release; ``_migrate`` adds whichever an existing database lacks.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "jobs": (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("claimed_at", "REAL"),
             ("finished_at", "REAL"), ("error", "TEXT NOT NULL DEFAULT ''"),
             ("processed_ts", "REAL")),
    "layer_state": (("session_id", "TEXT NOT NULL DEFAULT ''"),),
    "conclusions": (("from_ts", "REAL"), ("to_ts", "REAL"),
                    ("from_turn", "INTEGER"), ("to_turn", "INTEGER")),
    "base_refreshes": (("value_markers", "TEXT NOT NULL DEFAULT ''"),),
}

# Tables a previous release wrote that this one must not keep: earlier versions persisted every
# completed turn, which this provider deliberately no longer does.
_DROPPED_TABLES: tuple[str, ...] = ("turns",)

# Kinds that may be recalled across sessions. Everything else (session summaries, reviews,
# compression handoffs) is bound to the session that produced it: injecting another session's
# review as a high-weight conclusion would be actively wrong.
_LONG_TERM_KINDS: tuple[str, ...] = (
    "note",
    "base.user_peer_card",
    "base.ai_identity_card",
    "dialectic.constraints",
    "dialectic.preferences",
    "dialectic.second_brain",
)

# Fixed headings -> conclusion kind. The worker validates the same headings; publishing splits
# one artifact into individually searchable rows instead of one large document.
_ARTIFACT_SECTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "base": (
        # Derived projection of the session_chunk_summary chain; never an independent source.
        ("## Session Summary", "base.session_summary", "projection"),
        ("## User Representation", "base.user_representation", "session"),
        ("## User Peer Card", "base.user_peer_card", "user"),
        ("## AI Self Representation", "base.ai_self_representation", "session"),
        ("## AI Identity Card", "base.ai_identity_card", "agent"),
        ("## Value Markers", "base.value_markers", "session"),
    ),
    "dialectic": (
        # The six corrected cards live on the base layer; the dialectic artifact owns only the
        # review/advices so prefetch never leaks base cards into the prompt.
        ("## Adviser Review", "dialectic.review", "session"),
    ),
}

# Columns the dialectic layer may correct on a base refresh. The snapshot table stores these
# plus ``advices``; the FTS index covers all seven.
_BASE_REVISION_COLUMNS: tuple[str, ...] = (
    "user_representation", "user_peer_card", "ai_self_representation",
    "ai_identity_card", "session_chunk_summary", "value_markers",
)

_KIND_WEIGHT: dict[str, float] = {
    "base.ai_identity_card": 1.5,
    "base.user_peer_card": 1.5,
    "dialectic.review": 1.4,
    "base.user_representation": 1.2,
    "base.ai_self_representation": 1.0,
    "base.value_markers": 1.0,
    "dialectic.constraints": 1.0,
    "dialectic.preferences": 1.0,
    "dialectic.second_brain": 0.9,
    "session_summary": 0.8,
    "handoff": 0.8,
    "session_chunk_summary": 0.6,
    "note": 0.7,
}


def _split_sections(content: str, headings: tuple[tuple[str, str, str], ...]) -> dict[str, str]:
    """Split an artifact into ``heading -> body``; missing headings are simply omitted."""
    positions: list[tuple[int, str, str, str]] = []
    for heading, kind, scope in headings:
        position = content.find(heading)
        if position < 0:
            return {}
        positions.append((position, heading, kind, scope))
    positions.sort(key=lambda item: item[0])
    sections: dict[str, str] = {}
    for index, (position, heading, _kind, _scope) in enumerate(positions):
        start = position + len(heading)
        end = positions[index + 1][0] if index + 1 < len(positions) else len(content)
        sections[heading] = content[start:end].strip()
    return sections


def _scope_key(scope: str, subject: str, session_id: str, ai_subject: str = "") -> str:
    """Stable key for one conclusion: session-bound sections never leak into another session."""
    if scope == "session":
        return f"session:{session_id or subject}:{subject}"
    if scope == "user":
        return subject if subject.startswith("user:") else f"user:{subject}"
    agent = ai_subject or subject
    return agent if agent.startswith("agent:") else f"agent:{agent}"


_STOPWORDS = frozenset("""
a an and are as at be been but by for from had has have he her his i if in into is it its
of on or our she that the their them then there these they this to was were what when where
which who why will with would you your
这个 那个 我们 你们 他们 以及 但是 因为 所以 如果 然后 可以 需要 进行 已经 一个
没有 就是 不是 还是 可能 应该 现在 之前 之后 时候 什么 怎么 为什么 这样 那样
""".split())


def _terms(query: str) -> list[str]:
    """Query -> lowercase search tokens (deduped, stopwords dropped, order kept)."""
    tokens = re.findall(r"[\w-]+", (query or "").lower(), flags=re.UNICODE)
    seen: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token not in _STOPWORDS and token not in seen:
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
        self._fts = False
        self._base_fts = False
        self._base_revisions_fts = False
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._ensure_fts()
            self._ensure_base_refreshes_fts()
            self._ensure_base_revisions_fts()
            self._backfill_conclusions()
            self._conn.commit()

    def _migrate(self) -> None:
        """Bring a pre-existing database to this schema (idempotent; caller holds the lock).

        Adds columns a newer release introduced and drops the tables an older one wrote that
        this provider must not keep — a database from the version that captured every turn has
        its ``turns`` table removed here, on first open.
        """
        for table in _DROPPED_TABLES:
            self._conn.execute(f"DROP TABLE IF EXISTS {table}")
        # The rolling base Session Summary is a projection of the chunk chain now; an older
        # independent conclusion must not compete with it in FTS recall.
        self._conn.execute(
            "UPDATE conclusions SET status = 'superseded' WHERE kind = 'base.session_summary'")
        for table, columns in _ADDED_COLUMNS.items():
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns:
                if name not in present:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        # An older release flagged the row written last. Restore the invariant now that
        # ``is_latest`` means the greatest window: clear, then promote one row per subject.
        self._conn.execute("UPDATE base_refreshes SET is_latest = 0")
        self._conn.execute(
            "UPDATE base_refreshes SET is_latest = 1 WHERE id IN ("
            "SELECT b.id FROM base_refreshes AS b WHERE b.id = ("
            "SELECT x.id FROM base_refreshes AS x "
            "WHERE x.session_id = b.session_id AND x.subject = b.subject "
            "ORDER BY x.window_to_ts DESC, x.window_from_ts DESC, x.id DESC LIMIT 1))")

    def _ensure_fts(self) -> None:
        """Create the trigram FTS index (fallback: no FTS, scan in ``search``).

        ``unicode61`` treats a whole CJK sentence as one token, so it cannot recall Chinese
        conclusions. ``trigram`` handles CJK substrings and mixed text; SQLite builds without
        FTS5 simply fall back to the token scan below.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'conclusions_fts'"
        ).fetchone()
        current = str(row["sql"] or "").lower() if row is not None else ""
        if current and "trigram" not in current:
            # An older index with a different tokenizer cannot be fixed in place.
            for trigger in ("conclusions_ai", "conclusions_ad", "conclusions_au"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self._conn.execute("DROP TABLE IF EXISTS conclusions_fts")
            current = ""
        if not current:
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE conclusions_fts USING fts5("
                    "content, content='conclusions', content_rowid='id', tokenize='trigram')")
            except sqlite3.OperationalError:
                try:
                    self._conn.execute(
                        "CREATE VIRTUAL TABLE conclusions_fts USING fts5("
                        "content, content='conclusions', content_rowid='id')")
                except sqlite3.OperationalError as exc:
                    self._fts = False
                    logger.debug("Minir FTS5 unavailable, using token scan: %s", exc)
                    return
            self._conn.execute(
                "INSERT INTO conclusions_fts(conclusions_fts) VALUES('rebuild')")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS conclusions_ai AFTER INSERT ON conclusions BEGIN "
            "INSERT INTO conclusions_fts(rowid, content) VALUES (new.id, new.content); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS conclusions_ad AFTER DELETE ON conclusions BEGIN "
            "INSERT INTO conclusions_fts(conclusions_fts, rowid, content) "
            "VALUES ('delete', old.id, old.content); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS conclusions_au AFTER UPDATE ON conclusions BEGIN "
            "INSERT INTO conclusions_fts(conclusions_fts, rowid, content) "
            "VALUES ('delete', old.id, old.content); "
            "INSERT INTO conclusions_fts(rowid, content) VALUES (new.id, new.content); END")
        self._fts = True

    def _ensure_base_refreshes_fts(self) -> None:
        """Create/rebuild the FTS index over the six base-refresh outputs.

        External-content FTS: the text lives once in ``base_refreshes``; the virtual table
        indexes it. ``trigram`` matches CJK substrings, while queries whose terms are all
        shorter than three characters fall back to a scan in ``search_base_refreshes``.
        """
        columns = ("user_representation", "user_peer_card", "ai_self_representation",
                   "ai_identity_card", "session_chunk_summary", "value_markers")
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'base_refreshes_fts'"
        ).fetchone()
        current = str(row["sql"] or "").lower() if row is not None else ""
        existing = {item["name"] for item in self._conn.execute(
            "PRAGMA table_info(base_refreshes_fts)")} if current else set()
        if current and ("trigram" not in current or not set(columns) <= existing):
            for trigger in ("base_refreshes_ai", "base_refreshes_ad", "base_refreshes_au"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self._conn.execute("DROP TABLE IF EXISTS base_refreshes_fts")
            current = ""
        if not current:
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE base_refreshes_fts USING fts5("
                    + ", ".join(columns)
                    + ", content='base_refreshes', content_rowid='id', tokenize='trigram')")
            except sqlite3.OperationalError:
                try:
                    self._conn.execute(
                        "CREATE VIRTUAL TABLE base_refreshes_fts USING fts5("
                        + ", ".join(columns)
                        + ", content='base_refreshes', content_rowid='id')")
                except sqlite3.OperationalError as exc:
                    for trigger in ("base_refreshes_ai", "base_refreshes_ad",
                                    "base_refreshes_au"):
                        self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                    self._base_fts = False
                    logger.debug("Minir base-refresh FTS unavailable, using scan: %s", exc)
                    return
        values = ", ".join(f"new.{name}" for name in columns)
        old_values = ", ".join(f"old.{name}" for name in columns)
        column_list = ", ".join(columns)
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS base_refreshes_ai AFTER INSERT ON base_refreshes BEGIN "
            f"INSERT INTO base_refreshes_fts(rowid, {column_list}) VALUES (new.id, {values}); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS base_refreshes_ad AFTER DELETE ON base_refreshes BEGIN "
            "INSERT INTO base_refreshes_fts(base_refreshes_fts, rowid, "
            f"{column_list}) VALUES ('delete', old.id, {old_values}); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS base_refreshes_au AFTER UPDATE ON base_refreshes BEGIN "
            "INSERT INTO base_refreshes_fts(base_refreshes_fts, rowid, "
            f"{column_list}) VALUES ('delete', old.id, {old_values}); "
            f"INSERT INTO base_refreshes_fts(rowid, {column_list}) VALUES (new.id, {values}); END")
        self._conn.execute(
            "INSERT INTO base_refreshes_fts(base_refreshes_fts) VALUES('rebuild')")
        self._base_fts = True

    def _ensure_base_revisions_fts(self) -> None:
        """Create/rebuild the FTS index over the six snapshot fields plus advices.

        Same external-content trigram design as ``base_refreshes_fts``: the revision row is the
        immutable audit copy, so the index never needs a second text copy.
        """
        columns = ("user_representation", "user_peer_card", "ai_self_representation",
                   "ai_identity_card", "session_chunk_summary", "value_markers", "advices")
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'base_revisions_fts'"
        ).fetchone()
        current = str(row["sql"] or "").lower() if row is not None else ""
        existing = {item["name"] for item in self._conn.execute(
            "PRAGMA table_info(base_revisions_fts)")} if current else set()
        if current and ("trigram" not in current or not set(columns) <= existing):
            for trigger in ("base_revisions_ai", "base_revisions_ad", "base_revisions_au"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self._conn.execute("DROP TABLE IF EXISTS base_revisions_fts")
            current = ""
        if not current:
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE base_revisions_fts USING fts5("
                    + ", ".join(columns)
                    + ", content='base_revisions', content_rowid='id', tokenize='trigram')")
            except sqlite3.OperationalError:
                try:
                    self._conn.execute(
                        "CREATE VIRTUAL TABLE base_revisions_fts USING fts5("
                        + ", ".join(columns)
                        + ", content='base_revisions', content_rowid='id')")
                except sqlite3.OperationalError as exc:
                    for trigger in ("base_revisions_ai", "base_revisions_ad",
                                    "base_revisions_au"):
                        self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                    self._base_revisions_fts = False
                    logger.debug("Minir base-revision FTS unavailable, using scan: %s", exc)
                    return
        values = ", ".join(f"new.{name}" for name in columns)
        old_values = ", ".join(f"old.{name}" for name in columns)
        column_list = ", ".join(columns)
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS base_revisions_ai AFTER INSERT ON base_revisions BEGIN "
            f"INSERT INTO base_revisions_fts(rowid, {column_list}) VALUES (new.id, {values}); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS base_revisions_ad AFTER DELETE ON base_revisions BEGIN "
            "INSERT INTO base_revisions_fts(base_revisions_fts, rowid, "
            f"{column_list}) VALUES ('delete', old.id, {old_values}); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS base_revisions_au AFTER UPDATE ON base_revisions BEGIN "
            "INSERT INTO base_revisions_fts(base_revisions_fts, rowid, "
            f"{column_list}) VALUES ('delete', old.id, {old_values}); "
            f"INSERT INTO base_revisions_fts(rowid, {column_list}) VALUES (new.id, {values}); END")
        self._conn.execute(
            "INSERT INTO base_revisions_fts(base_revisions_fts) VALUES('rebuild')")
        self._base_revisions_fts = True

    def _backfill_conclusions(self) -> None:
        """Index notes written before ``conclusions`` existed (idempotent, once)."""
        existing = self._conn.execute("SELECT COUNT(*) AS n FROM conclusions").fetchone()["n"]
        if int(existing):
            return
        for row in self._conn.execute(
                "SELECT id, source, content, created_at FROM notes ORDER BY id").fetchall():
            self._put_conclusion_locked(
                "note", f"note:{row['id']}", row["content"], session_id="",
                subject=row["source"] or "tool", evidence="[]", status="active",
                created_at=row["created_at"], version=1)
        for row in self._conn.execute(
                "SELECT layer, subject, version, content, session_id FROM layer_state").fetchall():
            sections = _split_sections(row["content"], _ARTIFACT_SECTIONS.get(row["layer"], ()))
            for heading, kind, scope in _ARTIFACT_SECTIONS.get(row["layer"], ()):
                if heading not in sections or scope == "projection":
                    continue
                key = _scope_key(scope, row["subject"], row["session_id"])
                self._put_conclusion_locked(
                    kind, key, sections[heading], session_id=row["session_id"] if scope == "session" else "",
                    subject=row["subject"], evidence="[]", status="active",
                    version=int(row["version"] or 0))

    # -- writes ---------------------------------------------------------------

    def remember(self, content: str, source: str = "tool") -> int:
        """Store an explicit note; returns its row id. Also indexes it as a conclusion."""
        stamp = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notes (source, content, created_at) VALUES (?, ?, ?)",
                (source, content[:_MAX_CONTENT], stamp),
            )
            note_id = int(cur.lastrowid)
            self._put_conclusion_locked(
                "note", f"note:{note_id}", content[:_MAX_CONTENT], session_id="",
                subject=source or "tool", evidence="[]", status="active",
                created_at=stamp, version=1)
            self._conn.commit()
            return note_id

    def forget(self, note_id: int) -> bool:
        """Delete a note by id; False when no such note exists."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
            self._conn.execute("DELETE FROM conclusions WHERE kind = 'note' AND key = ?",
                               (f"note:{int(note_id)}",))
            self._conn.commit()
            return cur.rowcount > 0

    def forget_by_content(self, content: str) -> int:
        """Delete notes whose stored text equals *content*; returns rows deleted."""
        with self._lock:
            ids = [row["id"] for row in self._conn.execute(
                "SELECT id FROM notes WHERE content = ?", (content[:_MAX_CONTENT],)).fetchall()]
            cur = self._conn.execute("DELETE FROM notes WHERE content = ?", (content[:_MAX_CONTENT],))
            for note_id in ids:
                self._conn.execute("DELETE FROM conclusions WHERE kind = 'note' AND key = ?",
                                   (f"note:{note_id}",))
            self._conn.commit()
            return int(cur.rowcount)

    def _put_conclusion_locked(self, kind: str, key: str, content: str, *, session_id: str = "",
                               subject: str = "", evidence: str = "[]", status: str = "active",
                               created_at: Optional[float] = None, version: int = 0,
                               from_ts: Optional[float] = None, to_ts: Optional[float] = None,
                               from_turn: Optional[int] = None,
                               to_turn: Optional[int] = None) -> int:
        """Upsert one conclusion under the caller's lock; returns the resulting version."""
        stamp = time.time()
        self._conn.execute(
            "INSERT INTO conclusions (kind, key, session_id, subject, content, evidence, "
            "from_ts, to_ts, from_turn, to_turn, version, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET content = excluded.content, "
            "session_id = excluded.session_id, subject = excluded.subject, "
            "evidence = excluded.evidence, from_ts = excluded.from_ts, "
            "to_ts = excluded.to_ts, from_turn = excluded.from_turn, "
            "to_turn = excluded.to_turn, version = conclusions.version + 1, "
            "status = excluded.status, updated_at = excluded.updated_at",
            (kind, key, session_id or "", subject or "", (content or "")[:_MAX_CONTENT],
             evidence or "[]", from_ts, to_ts, from_turn, to_turn, max(0, int(version)),
             status, created_at or stamp, stamp))
        row = self._conn.execute(
            "SELECT version FROM conclusions WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        return int(row["version"]) if row is not None else 0

    def put_conclusion(self, kind: str, key: str, content: str, *, session_id: str = "",
                       subject: str = "", evidence: str = "[]", status: str = "active",
                       created_at: Optional[float] = None, version: int = 0,
                       from_ts: Optional[float] = None, to_ts: Optional[float] = None,
                       from_turn: Optional[int] = None, to_turn: Optional[int] = None) -> int:
        """Publish one conclusion and index it for FTS recall."""
        with self._lock:
            result = self._put_conclusion_locked(
                kind, key, content, session_id=session_id, subject=subject, evidence=evidence,
                status=status, created_at=created_at, version=version,
                from_ts=from_ts, to_ts=to_ts, from_turn=from_turn, to_turn=to_turn)
            self._conn.commit()
            return result

    def get_conclusion(self, kind: str, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conclusions WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        return dict(row) if row is not None else None

    def newest_conclusion(self, kind: str, *, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Newest active conclusion of a kind (optionally pinned to one session)."""
        with self._lock:
            if session_id is None:
                row = self._conn.execute(
                    "SELECT * FROM conclusions WHERE kind = ? AND status = 'active' "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1", (kind,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM conclusions WHERE kind = ? AND session_id = ? "
                    "AND status = 'active' ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (kind, session_id)).fetchone()
        return dict(row) if row is not None else None

    def delete_conclusion(self, kind: str, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conclusions WHERE kind = ? AND key = ?", (kind, key))
            self._conn.commit()
            return cur.rowcount > 0

    def promote_summary_run(self, session_id: str, run_id: str) -> int:
        """Promote the pending compression summary identified by its marker run id."""
        key = f"session_summary:{session_id}:{run_id}"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conclusions SET status = 'active', updated_at = ? "
                "WHERE kind = 'session_summary' AND session_id = ? AND key = ? "
                "AND status = 'pending'", (time.time(), session_id, key))
            self._conn.commit()
            return int(cur.rowcount)

    # -- session chunk chain --------------------------------------------------

    def add_chunk_summary(self, session_id: str, from_ts: float, to_ts: float, content: str,
                          *, evidence: str = "[]", from_turn: Optional[int] = None,
                          to_turn: Optional[int] = None) -> int:
        """Append one local compression summary to the session's canonical chain.

        Idempotent by ``(session, from_ts)``: windows are edge-to-edge, so the start edge
        identifies a window even when a retry resolves a later ``to_ts`` (the newest job drains
        to ``now``). Reusing the existing row is what keeps a failed publish from duplicating
        the chunk or shifting its turn range. A window already superseded by a final summary is
        not reopened. ``from_turn``/``to_turn`` are the session turn range this local
        compression covers; timestamps alone cannot express it after a /resume.
        """
        with self._lock:
            existing = self._conn.execute(
                "SELECT key, status FROM conclusions WHERE kind = 'session_chunk_summary' "
                "AND session_id = ? AND from_ts = ? ORDER BY id DESC LIMIT 1",
                (session_id, float(from_ts))).fetchone()
            if existing is not None and existing["status"] == "superseded":
                return 0
            key = (str(existing["key"]) if existing is not None else
                   f"chunk:{session_id}:{float(from_ts):.6f}:{float(to_ts):.6f}")
            version = self._put_conclusion_locked(
                "session_chunk_summary", key, content, session_id=session_id,
                subject="session", evidence=evidence, status="active",
                from_ts=float(from_ts), to_ts=float(to_ts),
                from_turn=from_turn, to_turn=to_turn)
            self._conn.commit()
            return version

    def chunk_summaries(self, session_id: str, *,
                        statuses: tuple[str, ...] = ("active",)) -> list[dict[str, Any]]:
        """The session's local compression chain, oldest first."""
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conclusions WHERE kind = 'session_chunk_summary' "
                f"AND session_id = ? AND status IN ({marks}) ORDER BY from_ts, id",
                (session_id, *statuses)).fetchall()
        return [dict(row) for row in rows]

    def latest_chunk_end(self, session_id: str) -> float:
        """Furthest end timestamp any chunk (including a superseded one) already covers."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(to_ts) AS ts FROM conclusions "
                "WHERE kind = 'session_chunk_summary' AND session_id = ?",
                (session_id,)).fetchone()
        return float(row["ts"]) if row is not None and row["ts"] is not None else 0.0

    def latest_chunk_turn(self, session_id: str) -> int:
        """Highest turn any chunk of this session covers, active or superseded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(to_turn) AS turn FROM conclusions "
                "WHERE kind = 'session_chunk_summary' AND session_id = ?",
                (session_id,)).fetchone()
        value = row["turn"] if row is not None else None
        return int(value) if value is not None else 0

    def chunk_turn_range(self, session_id: str,
                         from_ts: float) -> tuple[Optional[int], Optional[int]]:
        """The stored turn range for the window starting at *from_ts*, or ``(None, None)``.

        Used by the retry path: the chunk is written before ``base_refreshes``, so a failed
        refresh must reuse the range already attached to that window instead of treating it as
        the previous window. The start edge is the window identity; a retry may resolve a
        later ``to_ts`` when the newest job drains to ``now``.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT from_turn, to_turn FROM conclusions "
                "WHERE kind = 'session_chunk_summary' AND session_id = ? "
                "AND from_ts = ? ORDER BY id DESC LIMIT 1",
                (session_id, float(from_ts))).fetchone()
        if row is None:
            return None, None
        return row["from_turn"], row["to_turn"]

    def latest_chunk_turn_before(self, session_id: str, before_ts: float) -> int:
        """Highest turn covered by a chunk that ends at or before *before_ts*."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(to_turn) AS turn FROM conclusions "
                "WHERE kind = 'session_chunk_summary' AND session_id = ? AND to_ts <= ?",
                (session_id, float(before_ts))).fetchone()
        value = row["turn"] if row is not None else None
        return int(value) if value is not None else 0

    def latest_chunk_summary(self, session_id: str, *, max_chars: int = 4_000) -> str:
        """Projection for the base artifact's ``## Session Summary`` section.

        Only the newest active chunk is projected: it is the current window record. Older
        chunks stay as independent rows (with their turn range) for same-session FTS recall.
        """
        chunks = self.chunk_summaries(session_id)
        if not chunks:
            return ""
        chunk = chunks[-1]
        body = str(chunk.get("content") or "").strip()
        if not body:
            return ""
        header = "### Local chunk"
        if chunk.get("from_turn") is not None and chunk.get("to_turn") is not None:
            header += f" · turns {int(chunk['from_turn'])}-{int(chunk['to_turn'])}"
        elif chunk.get("from_ts") is not None and chunk.get("to_ts") is not None:
            header += (f" · {format_when(float(chunk['from_ts']))}"
                       f"–{format_when(float(chunk['to_ts']))}")
        return f"{header}\n{body}"[:max_chars].strip()

    def supersede_chunk_summaries(self, session_id: str) -> int:
        """Close the chain after a final summary has been adopted."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conclusions SET status = 'superseded', updated_at = ? "
                "WHERE kind = 'session_chunk_summary' AND session_id = ? AND status = 'active'",
                (time.time(), session_id))
            self._conn.commit()
            return int(cur.rowcount)

    def expire_pending_handoffs(self, session_id: str, *, before: float) -> int:
        """Drop pending summaries whose committed message never appeared (TTL sweep)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conclusions SET status = 'expired', updated_at = ? "
                "WHERE kind IN ('session_summary', 'handoff') "
                "AND session_id = ? AND status = 'pending' AND created_at < ?",
                (time.time(), session_id, float(before)))
            self._conn.commit()
            return int(cur.rowcount)

    def record_job(self, layer: str, session_id: str, from_ts: float, to_ts: float,
                   turns: int = 0, *, status: str = "pending") -> int:
        """Queue one layer job (session + reasoning window); returns its row id.

        Base-layer windows are unique: one window has one job. A second dispatch of the same
        ``(session, from_ts, to_ts)`` returns the existing queued/running/done job instead of
        appending a duplicate, so a boundary hook racing the tick cannot refresh one window
        twice. A failed job is not reused: a fresh row lets the window be retried.
        """
        window = (float(from_ts), float(to_ts))
        with self._lock:
            if layer in ("base", "dialectic"):
                existing = self._conn.execute(
                    "SELECT id FROM jobs WHERE layer = ? AND session_id = ? "
                    "AND from_ts = ? AND to_ts = ? "
                    "AND status IN ('pending', 'running', 'done') ORDER BY id LIMIT 1",
                    (layer, session_id, *window)).fetchone()
                if existing is not None:
                    return int(existing["id"])
            cur = self._conn.execute(
                "INSERT INTO jobs (layer, session_id, from_ts, to_ts, turns, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (layer, session_id, *window, int(turns), status, time.time()),
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

    def claim_jobs(self, limit: int = 2, *, session_id: str = "") -> list[dict[str, Any]]:
        """Mark the oldest *limit* pending jobs ``running`` and return them.

        One UPDATE round-trips the claim, so two workers (or a restarted process racing its
        own predecessor) can never process the same row twice. ``attempts`` counts claims.
        A non-empty *session_id* scopes the claim to one session, which is what the
        pre-compression synchronous drain needs.
        """
        with self._lock:
            # A review is queued only after its base job published, and it owns the window
            # that base just produced. Claim it before any later base job so the review sees
            # the live artifact and its correction can carry into the next base merge.
            order = ("ORDER BY CASE layer WHEN 'dialectic' THEN 0 ELSE 1 END, id")
            where = "WHERE status = 'pending'"
            params: list[Any] = []
            if session_id:
                where += " AND session_id = ?"
                params.append(session_id)
            ids = [row["id"] for row in self._conn.execute(
                f"SELECT id FROM jobs {where} {order} LIMIT ?",
                (*params, max(1, min(int(limit), 50))),
            ).fetchall()]
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            self._conn.execute(
                f"UPDATE jobs SET status = 'running', claimed_at = ?, attempts = attempts + 1 "
                f"WHERE id IN ({marks})", (time.time(), *ids),
            )
            self._conn.commit()
            rows = self._conn.execute(
                f"SELECT id, layer, session_id, from_ts, to_ts, turns, status, attempts, created_at "
                f"FROM jobs WHERE id IN ({marks}) {order}", ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_job(self, job_id: int, processed_ts: float, *, error: str = "") -> None:
        """Finish a job and advance its layer/session content watermark to *processed_ts*."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = 'done', finished_at = ?, processed_ts = ?, error = ? WHERE id = ?",
                (time.time(), float(processed_ts), error[:500], int(job_id)),
            )
            self._conn.commit()

    def fail_job(self, job_id: int, error: str, *, max_attempts: int = 2) -> str:
        """Record a failure; re-queue the job while it has attempts left. Returns the new status.

        A re-queued job keeps its window — the content watermark never moved, so the retry
        reads the same turns. A job that exhausts its attempts stays ``failed`` and the *next*
        job of that layer/session re-covers the same content, so an error cannot lose it.
        """
        with self._lock:
            row = self._conn.execute("SELECT attempts FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            attempts = int(row["attempts"]) if row is not None else max_attempts
            status = "pending" if attempts < max(1, int(max_attempts)) else "failed"
            self._conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, error[:500], time.time(), int(job_id)),
            )
            self._conn.commit()
            return status

    def expire_open_jobs(self, reason: str, *, before: float) -> int:
        """Close jobs left ``pending``/``running`` by a *previous* process; returns the count.

        Their window's content only ever lived in that process's memory (see ``transcript.py``),
        so nothing can reason over them any more: a fresh process closes them instead of
        re-queueing a job it can never complete. ``before`` is the process start — a job created
        since then belongs to a live instance in this same process (the host can build the
        provider twice) and is left alone.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'expired', error = ?, finished_at = ? "
                "WHERE status IN ('pending', 'running') AND created_at < ?",
                (reason[:500], time.time(), float(before)),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def last_processed_ts(self, layer: str, session_id: str) -> float:
        """Content watermark for one layer/session: how far its completed jobs have reasoned."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(processed_ts) AS ts FROM jobs "
                "WHERE layer = ? AND session_id = ? AND status = 'done' AND processed_ts IS NOT NULL",
                (layer, session_id),
            ).fetchone()
        return float(row["ts"]) if row is not None and row["ts"] is not None else 0.0

    def has_newer_job(self, layer: str, session_id: str, job_id: int) -> bool:
        """True when a later job of the same layer/session exists (it owns the open end)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM jobs WHERE layer = ? AND session_id = ? AND id > ? LIMIT 1",
                (layer, session_id, int(job_id)),
            ).fetchone()
        return row is not None

    def has_open_job(self, layer: str, session_id: str) -> bool:
        """True when a job of this layer/session is still queued or running.

        A boundary drain asks this so it does not queue a second job for content a queued job
        already owns — that job reads to ``now`` when it runs, so the content is covered.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM jobs WHERE layer = ? AND session_id = ? AND status IN ('pending', 'running') "
                "LIMIT 1", (layer, session_id),
            ).fetchone()
        return row is not None

    def pending_job_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE session_id = ? AND status = 'pending'",
                (session_id,)).fetchone()
        return int(row["n"] or 0) if row is not None else 0

    def open_job_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE session_id = ? "
                "AND status IN ('pending', 'running')", (session_id,)).fetchone()
        return int(row["n"] or 0) if row is not None else 0

    # -- compression gate -----------------------------------------------------

    def set_compression_gate(self, session_id: str, *, expires_at: float,
                             run_id: str = "") -> None:
        """Pause periodic layer scheduling for one session while compression runs."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO compression_gates (session_id, run_id, expires_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "run_id = excluded.run_id, expires_at = excluded.expires_at",
                (session_id, str(run_id or ""), float(expires_at)))
            self._conn.commit()

    def clear_compression_gate(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM compression_gates WHERE session_id = ?", (session_id,))
            self._conn.commit()

    def compression_gate_active(self, session_id: str, *, now: Optional[float] = None) -> bool:
        """True while a live gate exists; an expired gate is treated as resumed."""
        stamp = time.time() if now is None else float(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT expires_at FROM compression_gates WHERE session_id = ?",
                (session_id,)).fetchone()
        return row is not None and float(row["expires_at"] or 0.0) > stamp

    def open_job_end(self, layer: str, session_id: str) -> Optional[float]:
        """Furthest ``to_ts`` among this layer's queued/running jobs, or ``None``.

        A compression fold uses this to start its follow-up base job at the open job's edge:
        ``has_open_job`` alone would skip a window whose end already sits behind the content
        the fold just moved out.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(to_ts) AS ts FROM jobs WHERE layer = ? AND session_id = ? "
                "AND status IN ('pending', 'running')", (layer, session_id),
            ).fetchone()
        value = row["ts"] if row is not None else None
        return float(value) if value is not None else None

    # -- layer state ----------------------------------------------------------

    def put_layer_state(self, layer: str, subject: str, content: str, *,
                        session_id: str = "", ai_subject: str = "",
                        commit: bool = True) -> int:
        """Publish a layer artifact; returns the new version (1 for the first write).

        The artifact is also split into its fixed sections and each section is upserted as a
        conclusion, so ``search`` can recall a single section instead of a whole document.
        ``commit=False`` lets ``apply_base_revision`` keep the snapshot, refresh row and
        artifact in one ``BEGIN IMMEDIATE`` transaction.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO layer_state (layer, subject, version, content, session_id, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(layer, subject) DO UPDATE SET "
                "version = version + 1, content = excluded.content, "
                "session_id = excluded.session_id, updated_at = excluded.updated_at",
                (layer, subject, content[:_MAX_CONTENT], session_id or "", time.time()),
            )
            row = self._conn.execute(
                "SELECT version FROM layer_state WHERE layer = ? AND subject = ?", (layer, subject)).fetchone()
            version = int(row["version"]) if row is not None else 0
            sections = _split_sections(content, _ARTIFACT_SECTIONS.get(layer, ()))
            for heading, kind, scope in _ARTIFACT_SECTIONS.get(layer, ()):
                if heading not in sections or scope == "projection":
                    continue
                key = _scope_key(scope, subject, session_id, ai_subject)
                self._put_conclusion_locked(
                    kind, key, sections[heading], session_id=session_id if scope == "session" else "",
                    subject=subject, evidence="[]", status="active", version=version)
            if commit:
                self._conn.commit()
        return version

    def layer_state(self, layer: str, subject: str) -> Optional[dict[str, Any]]:
        """One merged artifact, or ``None`` when that layer never published for *subject*."""
        with self._lock:
            row = self._conn.execute(
                "SELECT layer, subject, version, content, session_id, updated_at FROM layer_state "
                "WHERE layer = ? AND subject = ?", (layer, subject)).fetchone()
        return dict(row) if row is not None else None

    def newest_layer_state(self, layer: str, *, subject: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Most recently published artifact of a layer (optionally pinned to one subject).

        Two artifacts published inside the same clock tick tie on ``updated_at``; the subject
        breaks the tie so the answer is stable rather than whatever SQLite returns first.
        """
        with self._lock:
            if subject is None:
                row = self._conn.execute(
                    "SELECT layer, subject, version, content, session_id, updated_at FROM layer_state "
                    "WHERE layer = ? ORDER BY updated_at DESC, subject DESC LIMIT 1", (layer,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT layer, subject, version, content, session_id, updated_at FROM layer_state "
                    "WHERE layer = ? AND subject = ?", (layer, subject)).fetchone()
        return dict(row) if row is not None else None

    def all_layer_states(self) -> list[dict[str, Any]]:
        """Every artifact as ``{layer, subject, version, session_id, updated_at}`` — content out."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT layer, subject, version, session_id, updated_at FROM layer_state "
                "ORDER BY layer, updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    # -- base refresh history -------------------------------------------------

    def add_base_refresh(self, session_id: str, subject: str, *, window_from_ts: float,
                         window_to_ts: float, window_from_turn: Optional[int],
                         window_to_turn: Optional[int], user_representation: str,
                         user_peer_card: str, ai_self_representation: str,
                         ai_identity_card: str, session_chunk_summary: str,
                         value_markers: str = "") -> int:
        """Record one base-layer refresh window; ``is_latest`` follows window order.

        A refresh is identified by ``(session_id, subject, window_from_ts)`` — windows are
        edge-to-edge, so the start edge is unique. Re-processing a re-queued job updates the
        same row instead of appending a duplicate. ``is_latest`` is decided by
        ``window_to_ts``, not by completion order: an older window that finishes after a newer
        one is retained as history with ``is_latest = 0``. ``BEGIN IMMEDIATE`` serializes the
        read-then-write across provider instances, so two workers cannot both promote a row.
        """
        now = time.time()
        session_key = session_id or ""
        subject_key = subject or ""
        values = (session_key, subject_key, float(window_from_ts), float(window_to_ts),
                  window_from_turn, window_to_turn, user_representation or "",
                  user_peer_card or "", ai_self_representation or "", ai_identity_card or "",
                  session_chunk_summary or "", value_markers or "", now)
        with self._lock:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._conn.execute(
                    "SELECT id, window_to_ts FROM base_refreshes "
                    "WHERE session_id = ? AND subject = ? AND is_latest = 1 LIMIT 1",
                    (session_key, subject_key)).fetchone()
                existing = self._conn.execute(
                    "SELECT id, is_latest, window_to_ts FROM base_refreshes "
                    "WHERE session_id = ? AND subject = ? AND window_from_ts = ?",
                    (session_key, subject_key, float(window_from_ts))).fetchone()
                # A retry may extend the same window; never shrink it.
                effective_to = float(window_to_ts)
                if existing is not None:
                    effective_to = max(effective_to, float(existing["window_to_ts"]))
                current_to = float(current["window_to_ts"]) if current is not None else None
                if existing is not None and int(existing["is_latest"]) == 1:
                    is_latest = 1
                elif current_to is None or effective_to > current_to:
                    is_latest = 1
                else:
                    is_latest = 0
                if is_latest:
                    self._conn.execute(
                        "UPDATE base_refreshes SET is_latest = 0 "
                        "WHERE session_id = ? AND subject = ? AND is_latest = 1 "
                        "AND window_from_ts <> ?",
                        (session_key, subject_key, float(window_from_ts)))
                self._conn.execute(
                    "INSERT INTO base_refreshes (session_id, subject, window_from_ts, window_to_ts, "
                    "window_from_turn, window_to_turn, user_representation, user_peer_card, "
                    "ai_self_representation, ai_identity_card, session_chunk_summary, "
                    "value_markers, is_latest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, subject, window_from_ts) DO UPDATE SET "
                    "window_to_ts = MAX(base_refreshes.window_to_ts, excluded.window_to_ts), "
                    "window_from_turn = excluded.window_from_turn, "
                    "window_to_turn = excluded.window_to_turn, "
                    "user_representation = excluded.user_representation, "
                    "user_peer_card = excluded.user_peer_card, "
                    "ai_self_representation = excluded.ai_self_representation, "
                    "ai_identity_card = excluded.ai_identity_card, "
                    "session_chunk_summary = excluded.session_chunk_summary, "
                    "value_markers = excluded.value_markers, "
                    "is_latest = excluded.is_latest, created_at = excluded.created_at",
                    (*values[:12], is_latest, values[12]))
                row = self._conn.execute(
                    "SELECT id FROM base_refreshes WHERE session_id = ? AND subject = ? "
                    "AND window_from_ts = ?",
                    values[:3]).fetchone()
                self._conn.commit()
                return int(row["id"]) if row is not None else 0
            except Exception:
                self._conn.rollback()
                raise

    def latest_base_refresh(self, session_id: str,
                            subject: str = "") -> Optional[dict[str, Any]]:
        """The active refresh row for a session (optionally pinned to one subject)."""
        with self._lock:
            if subject:
                row = self._conn.execute(
                    "SELECT * FROM base_refreshes WHERE session_id = ? AND subject = ? "
                    "AND is_latest = 1 LIMIT 1", (session_id, subject)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM base_refreshes WHERE session_id = ? AND is_latest = 1 "
                    "ORDER BY window_to_ts DESC, window_from_ts DESC, id DESC LIMIT 1",
                    (session_id,)).fetchone()
        return dict(row) if row is not None else None

    def base_refreshes(self, session_id: str, subject: str = "",
                       limit: int = 20) -> list[dict[str, Any]]:
        """The session's refresh history, newest first (latest flags preserved)."""
        limit = max(1, min(200, int(limit or 20)))
        with self._lock:
            if subject:
                rows = self._conn.execute(
                    "SELECT * FROM base_refreshes WHERE session_id = ? AND subject = ? "
                    "ORDER BY window_to_ts DESC, id DESC LIMIT ?",
                    (session_id, subject, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM base_refreshes WHERE session_id = ? "
                    "ORDER BY window_to_ts DESC, id DESC LIMIT ?",
                    (session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def base_refresh_for_window(self, session_id: str, subject: str,
                                window_from_ts: float) -> Optional[dict[str, Any]]:
        """The refresh row for one window (the row a dialectic correction targets)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM base_refreshes WHERE session_id = ? AND subject = ? "
                "AND window_from_ts = ? LIMIT 1",
                (session_id, subject, float(window_from_ts))).fetchone()
        return dict(row) if row is not None else None

    def apply_base_revision(self, session_id: str, subject: str, *, window_from_ts: float,
                            changes: dict[str, str], advices: str, changed_fields: list[str],
                            content: str, source_job_id: int = 0,
                            ai_subject: str = "") -> Optional[int]:
        """Snapshot the current base row, apply a dialectic correction, republish the artifact.

        One ``BEGIN IMMEDIATE`` transaction covers the snapshot, the ``base_refreshes`` update
        and the ``layer_state`` rewrite (via ``put_layer_state(..., commit=False)``), so a
        failure cannot leave the audit row and the live artifact disagreeing. ``is_latest`` is
        left alone: a correction never changes which window is current.
        """
        allowed = {name: value for name, value in changes.items() if name in _BASE_REVISION_COLUMNS}
        with self._lock:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM base_refreshes WHERE session_id = ? AND subject = ? "
                    "AND window_from_ts = ? LIMIT 1",
                    (session_id, subject, float(window_from_ts))).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                self._conn.execute(
                    "UPDATE base_revisions SET is_latest = 0 "
                    "WHERE session_id = ? AND subject = ? AND is_latest = 1",
                    (session_id, subject))
                cur = self._conn.execute(
                    "INSERT INTO base_revisions (session_id, subject, base_refresh_id, "
                    "window_from_ts, window_to_ts, window_from_turn, window_to_turn, "
                    "user_representation, user_peer_card, ai_self_representation, "
                    "ai_identity_card, session_chunk_summary, value_markers, advices, "
                    "changed_fields, source_job_id, is_latest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (session_id, subject, int(row["id"]), float(row["window_from_ts"]),
                     float(row["window_to_ts"]), row["window_from_turn"], row["window_to_turn"],
                     row["user_representation"], row["user_peer_card"],
                     row["ai_self_representation"], row["ai_identity_card"],
                     row["session_chunk_summary"], row["value_markers"],
                     advices or "[]", json.dumps(changed_fields), int(source_job_id),
                     time.time()))
                revision_id = int(cur.lastrowid)
                if allowed:
                    assignments = ", ".join(f"{name} = ?" for name in allowed)
                    self._conn.execute(
                        f"UPDATE base_refreshes SET {assignments} WHERE id = ?",
                        (*allowed.values(), int(row["id"])))
                # Only the live (greatest) window owns ``layer_state``. A late review of an
                # older window still snapshots and updates its own row, but must not replace
                # the artifact a newer base job already published.
                if int(row["is_latest"] or 0) == 1:
                    self.put_layer_state("base", subject, content, session_id=session_id,
                                         ai_subject=ai_subject, commit=False)
                self._conn.commit()
                return revision_id
            except Exception:
                self._conn.rollback()
                raise

    def base_revisions(self, session_id: str, subject: str = "",
                       limit: int = 20) -> list[dict[str, Any]]:
        """Revision audit rows, newest first."""
        limit = max(1, min(200, int(limit or 20)))
        with self._lock:
            if subject:
                rows = self._conn.execute(
                    "SELECT * FROM base_revisions WHERE session_id = ? AND subject = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (session_id, subject, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM base_revisions WHERE session_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def search_base_revisions(self, query: str, limit: int = 5, *,
                              session_id: str = "") -> list[dict[str, Any]]:
        """Search the six pre-overwrite fields plus advices (trigram FTS, LIKE fallback)."""
        terms = _terms(query)
        if not terms:
            return []
        capped = max(1, min(int(limit), 20))
        columns = (*_BASE_REVISION_COLUMNS, "advices")
        filters: list[str] = []
        filter_params: list[Any] = []
        if session_id:
            filters.append("r.session_id = ?")
            filter_params.append(session_id)
        where = (" AND " + " AND ".join(filters)) if filters else ""
        rows: list[dict[str, Any]] = []
        fts_terms = [term for term in terms if len(term) >= 3]
        if self._base_revisions_fts and fts_terms:
            match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms)
            sql = (
                "SELECT r.*, bm25(base_revisions_fts) AS rank FROM base_revisions_fts "
                "JOIN base_revisions r ON r.id = base_revisions_fts.rowid "
                f"WHERE base_revisions_fts MATCH ?{where} ORDER BY rank LIMIT ?")
            try:
                with self._lock:
                    found = self._conn.execute(
                        sql, [match, *filter_params, _MAX_SCAN]).fetchall()
                rows = [dict(row) for row in found]
            except sqlite3.OperationalError as exc:
                logger.debug("Minir base-revision FTS query failed, falling back: %s", exc)
        if not rows:
            clauses = []
            for _term in terms:
                clauses.append(" OR ".join(f"lower(r.{name}) LIKE ?" for name in columns))
            sql = (
                f"SELECT r.*, 0 AS rank FROM base_revisions r "
                f"WHERE ({' OR '.join(clauses)}){where} "
                "ORDER BY r.created_at DESC, r.id DESC LIMIT ?")
            like_params = [f"%{term}%" for term in terms for _name in columns]
            with self._lock:
                found = self._conn.execute(
                    sql, [*like_params, *filter_params, _MAX_SCAN]).fetchall()
            rows = [dict(row) for row in found]
        results: list[dict[str, Any]] = []
        for row in rows:
            text = " ".join(str(row.get(name) or "") for name in columns).lower()
            if not any(term in text for term in terms):
                continue
            entry = dict(row)
            entry["rank"] = float(row.get("rank") or 0)
            results.append(entry)
        return results[:capped]

    # -- advice delivery ------------------------------------------------------

    def add_advisories(self, session_id: str, advices: list[dict[str, Any]], *,
                       revision_id: int = 0, source: str = "dialectic") -> list[int]:
        """Insert normalized advices, deduped by session + normalized text fingerprint."""
        inserted: list[int] = []
        with self._lock:
            for item in advices:
                content = " ".join(str(item.get("content") or item.get("text") or "").split())
                if not content:
                    continue
                fingerprint = hashlib.sha256(
                    f"{session_id}\0{content.lower()}".encode("utf-8")).hexdigest()[:32]
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO advisories (revision_id, session_id, source, "
                    "severity, target, content, decision, evidence, fingerprint, status, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (int(revision_id), session_id, str(source),
                     str(item.get("severity") or "concern"), str(item.get("target") or ""),
                     content, str(item.get("decision") or ""),
                     json.dumps(item.get("evidence") or []), fingerprint, time.time()))
                if cur.rowcount:
                    inserted.append(int(cur.lastrowid))
            self._conn.commit()
        return inserted

    def pending_advisories(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """Undelivered advices, blockers first, then concerns, then nits."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM advisories WHERE session_id = ? AND status = 'pending' "
                "ORDER BY CASE severity WHEN 'blocker' THEN 0 WHEN 'concern' THEN 1 ELSE 2 END, "
                "id LIMIT ?", (session_id, max(1, min(int(limit), 32)))).fetchall()
        return [dict(row) for row in rows]

    def mark_advisories_injected(self, ids: list[int], turn_id: str = "") -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE advisories SET status = 'injected', injected_at = ?, turn_id = ? "
                f"WHERE id IN ({marks}) AND status = 'pending'",
                (time.time(), turn_id or "", *[int(item) for item in ids]))
            self._conn.commit()
            return int(cur.rowcount)

    def has_advisories(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM advisories WHERE session_id = ? LIMIT 1", (session_id,)).fetchone()
        return row is not None

    def search_base_refreshes(self, query: str, limit: int = 5, *, session_id: str = "",
                              latest_only: bool = False) -> list[dict[str, Any]]:
        """Search the six refresh outputs with the table-level FTS index.

        Mirrors ``search``: trigram FTS is used only when at least one term is >= 3 chars;
        a query made entirely of 1-2 char terms (common for Chinese, e.g. ``工具``) falls back
        to a LIKE scan, as does a missing FTS5 build. ``latest_only`` narrows to the currently
        active window; ``session_id`` keeps the history session-scoped.
        """
        terms = _terms(query)
        if not terms:
            return []
        capped = max(1, min(int(limit), 20))
        columns = ("user_representation", "user_peer_card", "ai_self_representation",
                   "ai_identity_card", "session_chunk_summary", "value_markers")
        filters: list[str] = []
        filter_params: list[Any] = []
        if session_id:
            filters.append("b.session_id = ?")
            filter_params.append(session_id)
        if latest_only:
            filters.append("b.is_latest = 1")
        where = (" AND " + " AND ".join(filters)) if filters else ""
        rows: list[dict[str, Any]] = []
        fts_terms = [term for term in terms if len(term) >= 3]
        if self._base_fts and fts_terms:
            match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms)
            sql = (
                "SELECT b.*, bm25(base_refreshes_fts) AS rank FROM base_refreshes_fts "
                "JOIN base_refreshes b ON b.id = base_refreshes_fts.rowid "
                f"WHERE base_refreshes_fts MATCH ?{where} "
                "ORDER BY rank LIMIT ?")
            try:
                with self._lock:
                    found = self._conn.execute(
                        sql, [match, *filter_params, _MAX_SCAN]).fetchall()
                rows = [dict(row) for row in found]
            except sqlite3.OperationalError as exc:
                logger.debug("Minir base-refresh FTS query failed, falling back to scan: %s", exc)
        if not rows:
            clauses = []
            for _term in terms:
                clauses.append(" OR ".join(f"lower(b.{name}) LIKE ?" for name in columns))
            sql = (
                f"SELECT b.*, 0 AS rank FROM base_refreshes b WHERE ({' OR '.join(clauses)})"
                f"{where} ORDER BY b.window_to_ts DESC, b.id DESC LIMIT ?")
            like_params = [f"%{term}%" for term in terms for _name in columns]
            with self._lock:
                found = self._conn.execute(
                    sql, [*like_params, *filter_params, _MAX_SCAN]).fetchall()
            rows = [dict(row) for row in found]

        results: list[dict[str, Any]] = []
        for row in rows:
            text = " ".join(str(row.get(name) or "") for name in columns).lower()
            overlap = sum(1 for term in terms if term in text)
            if not overlap:
                continue
            entry = dict(row)
            entry["score"] = float(overlap) + (0.5 if row.get("is_latest") else 0.0)
            entry["rank"] = float(row.get("rank") or 0)
            results.append(entry)
        results.sort(key=lambda item: (
            item["score"], -item["rank"], float(item.get("window_to_ts") or 0),
            int(item.get("id") or 0)), reverse=True)
        return results[:capped]

    # -- reads ----------------------------------------------------------------

    @staticmethod
    def _visibility_sql(session_id: str) -> tuple[str, list[Any]]:
        """SQL predicate + params: session-bound kinds only for the matching session.

        ``dialectic.review`` and ``base.session_summary`` are statements about one conversation;
        recalling another session's review as a high-weight conclusion would mislead the model.
        Cross-session recall is limited to durable user/agent/brain/note conclusions.
        """
        marks = ",".join("?" * len(_LONG_TERM_KINDS))
        if session_id:
            return (f"(c.session_id = ? OR c.kind IN ({marks}) OR c.kind LIKE 'brain.%')",
                    [session_id, *_LONG_TERM_KINDS])
        return (f"(c.kind IN ({marks}) OR c.kind LIKE 'brain.%')", list(_LONG_TERM_KINDS))

    def search(self, query: str, limit: int = 5, *, session_id: str = "") -> list[dict[str, Any]]:
        """Ranked matches across conclusions, scoped to *session_id* where required.

        FTS5 with the trigram tokenizer is the primary index (CJK-safe). A scan over the same
        rows is the fallback when FTS5 is unavailable or the query terms are too short for
        trigram. Returns ``{kind, id, note_id?, text, when, author, subject, session_id, score}``.
        """
        terms = _terms(query)
        if not terms:
            return []
        capped = max(1, min(int(limit), 20))
        visibility, visible_params = self._visibility_sql(session_id)
        rows: list[dict[str, Any]] = []
        # Historical chunk summaries stay recallable inside their own session after a final
        # summary supersedes them; the visibility predicate keeps them from leaking elsewhere.
        status = "(c.status = 'active' OR c.kind = 'session_chunk_summary')"
        fts_terms = [term for term in terms if len(term) >= 3]
        if self._fts and fts_terms:
            match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms)
            sql = (
                "SELECT c.*, bm25(conclusions_fts) AS rank FROM conclusions_fts "
                "JOIN conclusions c ON c.id = conclusions_fts.rowid "
                f"WHERE conclusions_fts MATCH ? AND {status} AND {visibility} "
                "ORDER BY rank LIMIT ?")
            try:
                with self._lock:
                    found = self._conn.execute(sql, [match, *visible_params, _MAX_SCAN]).fetchall()
                rows = [dict(row) for row in found]
            except sqlite3.OperationalError as exc:
                logger.debug("Minir FTS query failed, falling back to scan: %s", exc)
        if not rows:
            where = " OR ".join("lower(c.content) LIKE ?" for _ in terms)
            sql = (
                f"SELECT c.*, 0 AS rank FROM conclusions c WHERE {status} "
                f"AND {visibility} AND ({where}) "
                "ORDER BY c.updated_at DESC LIMIT ?")
            with self._lock:
                found = self._conn.execute(
                    sql, [*visible_params, *[f"%{term}%" for term in terms], _MAX_SCAN]).fetchall()
            rows = [dict(row) for row in found]

        results: list[dict[str, Any]] = []
        for row in rows:
            text = str(row.get("content") or "")
            lowered = text.lower()
            overlap = sum(1 for term in terms if term in lowered)
            if not overlap:
                continue
            kind = str(row.get("kind") or "")
            when = float(row.get("updated_at") or row.get("created_at") or 0)
            note_id = None
            if kind == "note":
                try:
                    note_id = int(str(row.get("key") or "").split(":", 1)[1])
                except (IndexError, ValueError):
                    note_id = None
            results.append({
                "kind": kind,
                "id": note_id if note_id is not None else int(row.get("id") or 0),
                "conclusion_id": int(row.get("id") or 0),
                "note_id": note_id,
                "key": row.get("key") or "",
                "text": text,
                "when": when,
                "author": row.get("subject") or kind,
                "subject": row.get("subject") or "",
                "session_id": row.get("session_id") or "",
                "from_turn": row.get("from_turn"),
                "to_turn": row.get("to_turn"),
                "score": float(overlap) + _KIND_WEIGHT.get(kind, 0.5),
                "rank": float(row.get("rank") or 0),
            })
        results.sort(key=lambda item: (item["score"], -item["rank"], item["when"], item["id"]),
                     reverse=True)
        return results[:capped]

    def stats(self) -> dict[str, Any]:
        """Row counts and on-disk size, for the ``minir_stats`` tool."""
        with self._lock:
            notes = self._conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
            sessions = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM jobs").fetchone()["n"]
            jobs = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'").fetchone()["n"]
            failed = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = 'failed'").fetchone()["n"]
            states = self._conn.execute("SELECT COUNT(*) AS n FROM layer_state").fetchone()["n"]
            refreshes = self._conn.execute("SELECT COUNT(*) AS n FROM base_refreshes").fetchone()["n"]
            revisions = self._conn.execute("SELECT COUNT(*) AS n FROM base_revisions").fetchone()["n"]
            advices = self._conn.execute("SELECT COUNT(*) AS n FROM advisories").fetchone()["n"]
            conclusions = self._conn.execute("SELECT COUNT(*) AS n FROM conclusions").fetchone()["n"]
            metrics = self._conn.execute("SELECT COUNT(*) AS n FROM metrics").fetchone()["n"]
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {"notes": int(notes), "sessions": int(sessions),
                "jobs": int(jobs), "pending_jobs": int(pending), "failed_jobs": int(failed),
                "layer_states": int(states), "base_refreshes": int(refreshes),
                "base_revisions": int(revisions), "advisories": int(advices),
                "conclusions": int(conclusions),
                "metrics": int(metrics), "fts": bool(self._fts),
                "db_path": str(self.path), "db_bytes": size}

    def record_metrics(self, entries: dict[str, dict[str, Any]]) -> None:
        """Upsert aggregated tool counters (one write per flush, not per tool call)."""
        stamp = time.time()
        with self._lock:
            for tool, values in entries.items():
                self._conn.execute(
                    "INSERT INTO metrics (tool, count, total_ms, max_ms, errors, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tool) DO UPDATE SET "
                    "count = metrics.count + excluded.count, "
                    "total_ms = metrics.total_ms + excluded.total_ms, "
                    "max_ms = MAX(metrics.max_ms, excluded.max_ms), "
                    "errors = metrics.errors + excluded.errors, updated_at = excluded.updated_at",
                    (tool, int(values.get("count", 0)), float(values.get("total_ms", 0.0)),
                     float(values.get("max_ms", 0.0)), int(values.get("errors", 0)), stamp))
            self._conn.commit()

    def close(self) -> None:
        """Close the connection; safe to call twice."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]


def format_when(created_at: float) -> str:
    """Epoch seconds -> compact UTC timestamp for recall blocks (``2026-10-16 11:04``)."""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(created_at))
