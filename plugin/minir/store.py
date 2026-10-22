"""SQLite storage for the Minir memory plugin — stdlib only.

**No transcripts are stored.** The provider reads the host's conversation history read-only (see
``history.py``) and persists only what it derived from those turns:

* ``conclusions`` — the single memory table. One row per conclusion and ``kind`` is the only
  type axis: the seven tool kinds (``note``, ``entities``, ``trajectories``, ``experiences``,
  ``tools``, ``projects``, ``todo``) plus the internal kinds the layers publish (``base.*``,
  ``dialectic.review``, ``session_summary``, ``window_summary``). A conclusion carries no
  window columns; only ``base_refreshes`` records which span of the conversation a row covers.
  ``conclusions_fts`` is an FTS5 trigram index over the content (CJK-safe); when FTS5 is
  unavailable, ``search()`` falls back to the token scan. Explicit ``minir_remember`` markers,
  the mirrored built-in memory-tool writes, the per-session todo row, the calibrated base
  fields, the dialectic review and the compression chain all live here.
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
* ``dialectic_refreshes`` — the pre-overwrite snapshot of a base refresh when the dialectic
  layer corrects it: the same six fields, the changed field list, the affected refresh id and an
  ``is_latest`` flag. ``dialectic_refreshes_fts`` indexes the six content fields. The table was
  called ``base_revisions`` and carried an ``advices`` column; the dialectic no longer emits
  advice, so both the name and the column are gone.
* ``base_refreshes.conclusions_published_at`` — when this session's six calibrated conclusions
  were published, scanned across the session so the publish runs once even across restarts.
* ``advisories`` — the delivery ledger for the single advice producer (the live reviewer):
  normalized, deduped advices with severity/target and a ``pending``/``injected`` state.
* ``metrics`` — aggregated tool counters, flushed in batches rather than per call.

``search()`` enforces session isolation by default: session summaries, reviews, focus
statements, todos and session-bound markers only match the session that produced them. The four
durable card kinds and durable markers are recallable across sessions; ``scope="all"``
additionally exposes every session's rows for explicit cross-session recall.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from uuid import uuid4
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_CONTENT = 12000  # stored text is truncated to this many chars
_MAX_TERMS = 8       # query tokens considered per search
_MAX_SCAN = 200      # candidate rows fetched per table before scoring

#: The five value-marker kinds the base layer merges; ``minir_remember``/``minir_forget`` and
#: the marker evidence paths use this vocabulary so search and deletion stay type-safe.
_MARKER_KINDS: tuple[str, ...] = (
    "entities", "trajectories", "experiences", "tools", "projects")

#: Every kind the tool surface may write through ``minir_remember`` / ``minir_forget`` /
#: ``minir_search(type=)``. ``note`` is the fallback kind (mirrored memory-tool writes, internal
#: rows without a better kind) and ``todo`` is the one per-session task list. Internal kinds
#: (``base.*``, ``dialectic.review``, ``session_summary``, ``window_summary``) are written by the
#: layers only and are not in this whitelist.
CONCLUSION_KINDS: tuple[str, ...] = ("note", *_MARKER_KINDS, "todo")

_SCHEMA = """
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
    window_summary TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS card_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT '',
    part TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    reviewed_at REAL NOT NULL,
    before_hash TEXT NOT NULL DEFAULT '',
    after_hash TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    changed_fields TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'review',
    evidence TEXT NOT NULL DEFAULT '[]',
    reviewer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'applied'
);
CREATE INDEX IF NOT EXISTS idx_card_revisions_part
    ON card_revisions(scope, part, reviewed_at DESC);
CREATE TABLE IF NOT EXISTS advisories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL DEFAULT 0,
    review_id INTEGER NOT NULL DEFAULT 0,
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
    delivered_at REAL,
    request_id TEXT NOT NULL DEFAULT '',
    expires_at REAL,
    turn_id TEXT NOT NULL DEFAULT '',
    turn INTEGER NOT NULL DEFAULT 0,
    deferred INTEGER NOT NULL DEFAULT 0,
    adviser TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_advisories_pending
    ON advisories(session_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_advisories_fingerprint
    ON advisories(session_id, fingerprint);
CREATE TABLE IF NOT EXISTS adviser_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    from_seq INTEGER NOT NULL DEFAULT 0,
    to_seq INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    api_calls INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    adviser TEXT NOT NULL DEFAULT '',
    notes_dropped INTEGER NOT NULL DEFAULT 0,
    drop_detail TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adviser_reviews_session
    ON adviser_reviews(session_id, created_at DESC);
CREATE TABLE IF NOT EXISTS metrics (
    tool TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    total_ms REAL NOT NULL DEFAULT 0,
    max_ms REAL NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_extractions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    due_at REAL NOT NULL,
    markers TEXT NOT NULL DEFAULT '{}',
    window_from_ts REAL NOT NULL DEFAULT 0,
    window_to_ts REAL NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    summary_chars INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_session_extractions_due
    ON session_extractions(status, due_at);
CREATE TABLE IF NOT EXISTS turn_usage (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    api_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    first_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    final_at REAL,
    PRIMARY KEY (session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_turn_usage_session
    ON turn_usage(session_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS session_usage (
    session_id TEXT PRIMARY KEY,
    turns INTEGER NOT NULL DEFAULT 0,
    api_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    host_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    host_completion_tokens INTEGER NOT NULL DEFAULT 0,
    host_total_tokens INTEGER NOT NULL DEFAULT 0,
    finalized_at REAL NOT NULL
);
"""

# The dialectic's pre-overwrite snapshot. Created by ``_migrate`` rather than ``_SCHEMA``: an
# existing database still has it as ``base_revisions``, and ``CREATE TABLE IF NOT EXISTS`` here
# would fabricate an empty table before the rename could run.
_DIALECTIC_REFRESHES_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialectic_refreshes (
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
    window_summary TEXT NOT NULL DEFAULT '',
    value_markers TEXT NOT NULL DEFAULT '',
    changed_fields TEXT NOT NULL DEFAULT '[]',
    source_job_id INTEGER NOT NULL DEFAULT 0,
    is_latest INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_base_revisions_session
    ON dialectic_refreshes(session_id, subject, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_base_revisions_latest
    ON dialectic_refreshes(session_id, subject) WHERE is_latest = 1;
"""

# Columns added after the first release; ``_migrate`` adds whichever an existing database lacks.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "jobs": (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("claimed_at", "REAL"),
             ("finished_at", "REAL"), ("error", "TEXT NOT NULL DEFAULT ''"),
             ("processed_ts", "REAL")),
    "layer_state": (("session_id", "TEXT NOT NULL DEFAULT ''"),),
    "base_refreshes": (("value_markers", "TEXT NOT NULL DEFAULT ''"),
                       # Set once per session when the six calibrated conclusions were
                       # published; the guard the four boundary hooks share.
                       ("conclusions_published_at", "REAL")),
    "advisories": (("review_id", "INTEGER NOT NULL DEFAULT 0"),
                   ("delivered_at", "REAL"),
                   ("request_id", "TEXT NOT NULL DEFAULT ''"),
                   ("expires_at", "REAL"),
                   # Which adviser in the roster produced the note; '' on pre-roster rows.
                   ("adviser", "TEXT NOT NULL DEFAULT ''"),
                   # Numeric turn the note was produced in (turns_ago) and whether it waits
                   # for the turn-end aggregate instead of a mid-turn channel.
                   ("turn", "INTEGER NOT NULL DEFAULT 0"),
                   ("deferred", "INTEGER NOT NULL DEFAULT 0")),
    "adviser_reviews": (("adviser", "TEXT NOT NULL DEFAULT ''"),
                        # Emission-guard outcome, so a silent reviewer is distinguishable
                        # from one that produced only noise/duplicates.
                        ("notes_dropped", "INTEGER NOT NULL DEFAULT 0"),
                        ("drop_detail", "TEXT NOT NULL DEFAULT '{}'"),
                        # Per-review target coverage: target -> clean / found / n/a.
                        ("coverage", "TEXT NOT NULL DEFAULT '{}'")),
}

# Tables a previous release wrote that this one must not keep: earlier versions persisted every
# completed turn, which this provider deliberately no longer does.
_DROPPED_TABLES: tuple[str, ...] = ("turns", "notes")

# Kinds that may be recalled across sessions. Everything else (session summaries, reviews,
# compression summaries, focus statements) is bound to the session that produced it: injecting
# another session's review as a high-weight conclusion would be actively wrong.
_LONG_TERM_KINDS: tuple[str, ...] = (
    "base.user_representation",
    "base.user_peer_card",
    "base.ai_identity_card",
)

# Fixed headings -> conclusion kind. The worker validates the same headings; publishing splits
# one artifact into individually searchable rows instead of one large document.
_ARTIFACT_SECTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    # The base layer has no entry: its six calibrated fields are published one per column by
    # ``publish_base_conclusions``, and splitting the artifact as well would duplicate them.
    "dialectic": (
        # The corrected parts live on the base layer; the dialectic artifact owns only the
        # revision note, so the live layer injection never leaks a base card or advice.
        ("## Base Revision", "dialectic.review", "session"),
    ),
}

# Columns the dialectic layer may correct on a base refresh. The snapshot table stores these
# six; the FTS index covers all of them.
_BASE_REVISION_COLUMNS: tuple[str, ...] = (
    "user_representation", "user_peer_card", "ai_self_representation",
    "ai_identity_card", "window_summary", "value_markers",
)

_KIND_WEIGHT: dict[str, float] = {
    "base.user_representation": 1.5,
    "base.user_peer_card": 1.5,
    "base.ai_self_representation": 1.4,
    "base.ai_identity_card": 1.4,
    "dialectic.review": 1.4,
    "base.window_summary": 1.0,
    "base.value_markers": 1.0,
    "session_summary": 0.8,
    "window_summary": 0.6,
    "note": 0.7,
    "entities": 0.9,
    "trajectories": 0.9,
    "experiences": 0.9,
    "tools": 0.9,
    "projects": 0.9,
    "todo": 1.2,
}

#: One conclusions kind per calibrated base column, with the scope its key is built for.
_BASE_CONCLUSION_KINDS: tuple[tuple[str, str, str], ...] = (
    ("user_representation", "base.user_representation", "user"),
    ("user_peer_card", "base.user_peer_card", "user"),
    # The AI self representation drifts per conversation; it is a ruler for the session that
    # produced it, not a cross-session card.
    ("ai_self_representation", "base.ai_self_representation", "session"),
    ("ai_identity_card", "base.ai_identity_card", "agent"),
    ("window_summary", "base.window_summary", "session"),
    ("value_markers", "base.value_markers", "session"),
)

#: Kinds whose keys carry the producing session. Reading one without a session id must fail
#: loudly instead of returning an empty list, and ``scope="all"`` must not leak them.
_SESSION_BOUND_KINDS: tuple[str, ...] = tuple(
    kind for _column, kind, scope in _BASE_CONCLUSION_KINDS if scope == "session")


def is_session_bound(kinds: Any) -> bool:
    """True when every requested kind is owned by the session that produced it."""
    requested = frozenset(str(kind) for kind in kinds or ())
    return bool(requested) and requested <= frozenset(_SESSION_BOUND_KINDS)


class SessionScopeError(ValueError):
    """A session-bound kind was requested without the session that owns it."""


def _session_scope_message(kinds: frozenset[str], scope: str, session_id: str) -> str:
    kind = sorted(kinds)[0] if kinds else _SESSION_BOUND_KINDS[0]
    label = "AI self representation" if kind == "base.ai_self_representation" else kind
    return (
        "session_scope_required: " + f"{label} is session-scoped. Provide the main session id "
        "(minir_search session_id or the provider session) to read it. "
        f"[kind={kind} scope={scope} session_id={session_id or ''}]")


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
        its ``turns`` table removed here, on first open, and one from the two-table release loses
        ``notes`` now that ``conclusions`` is the only memory table.
        """
        for table in _DROPPED_TABLES:
            self._conn.execute(f"DROP TABLE IF EXISTS {table}")
        # ``base_revisions`` is ``dialectic_refreshes`` now and lost its dead ``advices`` column.
        # First, before the column loop below: it also retires the legacy FTS and its triggers.
        self._migrate_dialectic_refreshes()
        # The window summary used to be called ``session_chunk_summary`` in both the live and the
        # snapshot table. Rename it in place; the FTS tables are rebuilt later because their
        # external-content schemas still name the old column.
        for table in ("base_refreshes", "dialectic_refreshes"):
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if "session_chunk_summary" in present and "window_summary" not in present:
                self._conn.execute(
                    f"ALTER TABLE {table} RENAME COLUMN session_chunk_summary TO window_summary")
            elif "session_chunk_summary" in present and "window_summary" in present:
                self._conn.execute(
                    f"UPDATE {table} SET window_summary = session_chunk_summary "
                    "WHERE (window_summary IS NULL OR window_summary = '') "
                    "AND session_chunk_summary IS NOT NULL AND session_chunk_summary <> ''")
        # One kind for the compression chain. Ignore a collision with a row already migrated.
        self._conn.execute(
            "UPDATE OR IGNORE conclusions SET kind = 'window_summary' "
            "WHERE kind = 'session_chunk_summary'")
        # Old snapshots and changed_fields lists named the parts by their pre-rename keys.
        self._conn.execute(
            "UPDATE dialectic_refreshes SET changed_fields = "
            "REPLACE(REPLACE(REPLACE(changed_fields, "
            "'\"user_self\"', '\"user_self_representation\"'), "
            "'\"ai_self\"', '\"ai_self_representation\"'), "
            "'\"session_chunk_summary\"', '\"window_summary\"') "
            "WHERE changed_fields LIKE '%\"user_self\"%' "
            "OR changed_fields LIKE '%\"ai_self\"%' "
            "OR changed_fields LIKE '%\"session_chunk_summary\"%'")
        # The rolling base Session Summary is a projection of the chunk chain now; an older
        # independent conclusion must not compete with it in FTS recall.
        self._conn.execute(
            "UPDATE conclusions SET status = 'superseded' WHERE kind = 'base.session_summary'")
        for table, columns in _ADDED_COLUMNS.items():
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns:
                if name not in present:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        # Conclusions no longer express a window: only ``base_refreshes`` does. The FTS index
        # covers ``content`` alone, but it is dropped and rebuilt anyway because an ALTER can
        # leave an external-content index's row map stale.
        present = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(conclusions)")}
        dropped = [name for name in ("from_ts", "to_ts", "from_turn", "to_turn")
                   if name in present]
        for name in dropped:
            self._conn.execute(f"ALTER TABLE conclusions DROP COLUMN {name}")
        if dropped:
            for trigger in ("conclusions_ai", "conclusions_ad", "conclusions_au"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self._conn.execute("DROP TABLE IF EXISTS conclusions_fts")
        # After the ALTER loop: an old ``advisories`` table has no ``request_id`` yet. A request
        # staged by a process that died was never confirmed delivered; make it pending again so
        # the next request can carry it.
        self._conn.execute(
            "UPDATE advisories SET status = 'pending', request_id = '' WHERE status = 'staged'")
        # An older release flagged the row written last. Restore the invariant now that
        # ``is_latest`` means the greatest window: clear, then promote one row per subject.
        self._conn.execute("UPDATE base_refreshes SET is_latest = 0")
        self._conn.execute(
            "UPDATE base_refreshes SET is_latest = 1 WHERE id IN ("
            "SELECT b.id FROM base_refreshes AS b WHERE b.id = ("
            "SELECT x.id FROM base_refreshes AS x "
            "WHERE x.session_id = b.session_id AND x.subject = b.subject "
            "ORDER BY x.window_to_ts DESC, x.window_from_ts DESC, x.id DESC LIMIT 1))")

    def _migrate_dialectic_refreshes(self) -> None:
        """Rename ``base_revisions`` and drop its dead ``advices`` column (idempotent).

        The external-content FTS names its target in the ``content=`` module argument, which a
        table rename does not rewrite, so the old index and its three triggers go first and
        ``_ensure_base_revisions_fts`` rebuilds them against the new table.
        """
        tables = {row["name"] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "base_revisions" in tables:
            for trigger in ("base_revisions_ai", "base_revisions_ad", "base_revisions_au"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self._conn.execute("DROP TABLE IF EXISTS base_revisions_fts")
            if "dialectic_refreshes" in tables:
                self._conn.execute("DROP TABLE base_revisions")
            else:
                self._conn.execute(
                    "ALTER TABLE base_revisions RENAME TO dialectic_refreshes")
        self._conn.executescript(_DIALECTIC_REFRESHES_SCHEMA)
        present = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(dialectic_refreshes)")}
        if "advices" not in present:
            return
        try:
            self._conn.execute("ALTER TABLE dialectic_refreshes DROP COLUMN advices")
        except sqlite3.OperationalError:
            # SQLite < 3.35 cannot drop a column: rebuild the table from the same DDL.
            kept = ("id, session_id, subject, base_refresh_id, window_from_ts, window_to_ts, "
                    "window_from_turn, window_to_turn, user_representation, user_peer_card, "
                    "ai_self_representation, ai_identity_card, window_summary, value_markers, "
                    "changed_fields, source_job_id, is_latest, created_at")
            self._conn.execute(
                "ALTER TABLE dialectic_refreshes RENAME TO dialectic_refreshes_old")
            self._conn.executescript(_DIALECTIC_REFRESHES_SCHEMA)
            self._conn.execute(
                f"INSERT INTO dialectic_refreshes ({kept}) "
                f"SELECT {kept} FROM dialectic_refreshes_old")
            self._conn.execute("DROP TABLE dialectic_refreshes_old")

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
                   "ai_identity_card", "window_summary", "value_markers")
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
        """Create/rebuild the FTS index over the six snapshot fields.

        Same external-content trigram design as ``base_refreshes_fts``: the revision row is the
        immutable audit copy, so the index never needs a second text copy. The table it indexes
        is ``dialectic_refreshes``; the method keeps its pre-rename name, the object names follow
        the table.
        """
        columns = _BASE_REVISION_COLUMNS
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'dialectic_refreshes_fts'"
        ).fetchone()
        current = str(row["sql"] or "").lower() if row is not None else ""
        existing = {item["name"] for item in self._conn.execute(
            "PRAGMA table_info(dialectic_refreshes_fts)")} if current else set()
        if current and ("trigram" not in current or not set(columns) <= existing):
            for trigger in ("dialectic_refreshes_ai", "dialectic_refreshes_ad",
                            "dialectic_refreshes_au"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self._conn.execute("DROP TABLE IF EXISTS dialectic_refreshes_fts")
            current = ""
        if not current:
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE dialectic_refreshes_fts USING fts5("
                    + ", ".join(columns)
                    + ", content='dialectic_refreshes', content_rowid='id', tokenize='trigram')")
            except sqlite3.OperationalError:
                try:
                    self._conn.execute(
                        "CREATE VIRTUAL TABLE dialectic_refreshes_fts USING fts5("
                        + ", ".join(columns)
                        + ", content='dialectic_refreshes', content_rowid='id')")
                except sqlite3.OperationalError as exc:
                    for trigger in ("dialectic_refreshes_ai", "dialectic_refreshes_ad",
                                    "dialectic_refreshes_au"):
                        self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                    self._base_revisions_fts = False
                    logger.debug("Minir dialectic-refresh FTS unavailable, using scan: %s", exc)
                    return
        values = ", ".join(f"new.{name}" for name in columns)
        old_values = ", ".join(f"old.{name}" for name in columns)
        column_list = ", ".join(columns)
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dialectic_refreshes_ai AFTER INSERT ON "
            "dialectic_refreshes BEGIN "
            "INSERT INTO dialectic_refreshes_fts(rowid, "
            f"{column_list}) VALUES (new.id, {values}); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dialectic_refreshes_ad AFTER DELETE ON "
            "dialectic_refreshes BEGIN "
            "INSERT INTO dialectic_refreshes_fts(dialectic_refreshes_fts, rowid, "
            f"{column_list}) VALUES ('delete', old.id, {old_values}); END")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dialectic_refreshes_au AFTER UPDATE ON "
            "dialectic_refreshes BEGIN "
            "INSERT INTO dialectic_refreshes_fts(dialectic_refreshes_fts, rowid, "
            f"{column_list}) VALUES ('delete', old.id, {old_values}); "
            "INSERT INTO dialectic_refreshes_fts(rowid, "
            f"{column_list}) VALUES (new.id, {values}); END")
        self._conn.execute(
            "INSERT INTO dialectic_refreshes_fts(dialectic_refreshes_fts) VALUES('rebuild')")
        self._base_revisions_fts = True

    def _backfill_conclusions(self) -> None:
        """Index a layer artifact published before its sections became conclusions (once)."""
        existing = self._conn.execute("SELECT COUNT(*) AS n FROM conclusions").fetchone()["n"]
        if int(existing):
            return
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

    def save_conclusion(self, kind: str, content: str, *, key: str = "",
                        session_id: str = "", subject: str = "", evidence: str = "[]",
                        status: str = "active", created_at: Optional[float] = None,
                        version: int = 0) -> int:
        """Upsert one conclusion and return its row id — the single public write entry.

        ``content`` is opaque: text stays text, a JSON payload stays the JSON string it was
        given, and nothing is parsed or reformatted here. ``kind`` is the only type axis; an
        empty ``key`` becomes ``<kind>:<uuid>`` so an unnamed row cannot collide with another.
        The table's uniqueness is ``(kind, key)``; ``session_id`` is a column, not part of it.
        """
        kind = str(kind or "").strip()
        if not kind:
            raise ValueError("conclusion kind is required")
        row_key = str(key or "").strip() or f"{kind}:{uuid4().hex}"
        with self._lock:
            row_id = self._put_conclusion_locked(
                kind, row_key, content, session_id=session_id, subject=subject,
                evidence=evidence, status=status, created_at=created_at, version=version)
            self._conn.commit()
            return row_id

    def forget_conclusion(self, kind: str, conclusions_id: int) -> bool:
        """Delete one conclusion by id and kind; a wrong kind deletes nothing."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conclusions WHERE id = ? AND kind = ?",
                (int(conclusions_id), str(kind or "")))
            self._conn.commit()
            return cur.rowcount > 0

    def newest_conclusion_by_content(self, kind: str, content: str, *,
                                     session_id: str = "") -> Optional[dict[str, Any]]:
        """Newest row of *kind* whose stored text equals *content* (the mirror's undo path)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conclusions WHERE kind = ? AND content = ? AND session_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (str(kind or ""), str(content or "")[:_MAX_CONTENT],
                 str(session_id or ""))).fetchone()
        return dict(row) if row is not None else None

    def conclusions_by_kind(self, session_id: str) -> dict[str, int]:
        """Row counts per kind for one host session; durable rows carry no session id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM conclusions WHERE session_id = ? "
                "GROUP BY kind", (str(session_id or ""),)).fetchall()
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def next_session_summary_index(self, session_id: str) -> int:
        """1-based index of this session's next compression summary."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM conclusions WHERE kind = 'session_summary' "
                "AND session_id = ?", (str(session_id or ""),)).fetchone()
        return int(row["n"]) + 1 if row is not None else 1

    def _put_conclusion_locked(self, kind: str, key: str, content: str, *, session_id: str = "",
                               subject: str = "", evidence: str = "[]", status: str = "active",
                               created_at: Optional[float] = None,
                               version: int = 0) -> int:
        """Upsert one conclusion under the caller's lock; returns ``conclusions.id``.

        The transaction-internal primitive: ``put_layer_state`` and ``apply_base_revision``
        write several rows inside one transaction and must not commit per row.
        """
        stamp = time.time()
        self._conn.execute(
            "INSERT INTO conclusions (kind, key, session_id, subject, content, evidence, "
            "version, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET content = excluded.content, "
            "session_id = excluded.session_id, subject = excluded.subject, "
            "evidence = excluded.evidence, version = conclusions.version + 1, "
            "status = excluded.status, updated_at = excluded.updated_at",
            (kind, key, session_id or "", subject or "", (content or "")[:_MAX_CONTENT],
             evidence or "[]", max(0, int(version)), status, created_at or stamp, stamp))
        row = self._conn.execute(
            "SELECT id FROM conclusions WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        return int(row["id"]) if row is not None else 0

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

    # -- session chunk chain --------------------------------------------------

    def add_chunk_summary(self, session_id: str, from_ts: float, content: str,
                          *, evidence: str = "[]") -> int:
        """Append one local compression summary to the session's canonical chain.

        The key ``chunk:<sid>:<from_ts>`` is the window identity and the idempotency key: a
        retry that drains a later end still updates the same row instead of duplicating the
        chunk or shifting its window. A window already superseded by a final summary is not
        reopened. The row itself carries no window — ``base_refreshes`` owns that — so the
        window start lives in the key, which is what every reader parses.
        """
        key = f"chunk:{session_id}:{float(from_ts):.6f}"
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM conclusions WHERE kind = 'window_summary' AND key = ?",
                (key,)).fetchone()
            if existing is not None and str(existing["status"]) == "superseded":
                return 0
            row_id = self._put_conclusion_locked(
                "window_summary", key, content, session_id=session_id, subject="session",
                evidence=evidence, status="active")
            self._conn.commit()
            return row_id

    def chunk_summaries(self, session_id: str, *,
                        statuses: tuple[str, ...] = ("active",)) -> list[dict[str, Any]]:
        """The session's local compression chain, oldest first (insertion order is window order)."""
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conclusions WHERE kind = 'window_summary' "
                f"AND session_id = ? AND status IN ({marks}) ORDER BY id",
                (session_id, *statuses)).fetchall()
        return [dict(row) for row in rows]

    def latest_chunk_end(self, session_id: str) -> float:
        """Furthest end timestamp the session's refresh windows already cover."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(window_to_ts) AS ts FROM base_refreshes WHERE session_id = ?",
                (session_id,)).fetchone()
        return float(row["ts"]) if row is not None and row["ts"] is not None else 0.0

    def chunk_turn_range(self, session_id: str,
                         from_ts: float) -> tuple[Optional[int], Optional[int]]:
        """The stored turn range for the window starting at *from_ts*, or ``(None, None)``.

        Read from ``base_refreshes``, the window owner. Used by the retry path: the chunk is
        written before the refresh row, so a failed refresh must reuse the range already
        attached to that window instead of treating it as the previous window.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT window_from_turn, window_to_turn FROM base_refreshes "
                "WHERE session_id = ? AND window_from_ts = ? LIMIT 1",
                (session_id, float(from_ts))).fetchone()
        if row is None:
            return None, None
        return row["window_from_turn"], row["window_to_turn"]

    def latest_chunk_turn_before(self, session_id: str, before_ts: float) -> int:
        """Highest turn covered by a window that starts before *before_ts*."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(window_to_turn) AS turn FROM base_refreshes "
                "WHERE session_id = ? AND window_from_ts < ?",
                (session_id, float(before_ts))).fetchone()
        value = row["turn"] if row is not None else None
        return int(value) if value is not None else 0

    def latest_chunk_summary(self, session_id: str, *, max_chars: int = 4_000) -> str:
        """Projection of the newest active chunk for a base artifact's summary section.

        Only the newest active chunk is projected: it is the current window record. Older
        chunks stay as independent rows and remain searchable inside their own session.
        """
        chunks = self.chunk_summaries(session_id)
        if not chunks:
            return ""
        chunk = chunks[-1]
        body = str(chunk.get("content") or "").strip()
        if not body:
            return ""
        return f"### Local chunk\n{body}"[:max_chars].strip()

    def supersede_chunk_summaries(self, session_id: str) -> int:
        """Close the chain after a final summary has been adopted."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conclusions SET status = 'superseded', updated_at = ? "
                "WHERE kind = 'window_summary' AND session_id = ? AND status = 'active'",
                (time.time(), session_id))
            self._conn.commit()
            return int(cur.rowcount)

    def expire_pending_handoffs(self, session_id: str, *, before: float) -> int:
        """Drop pending summaries whose committed message never appeared (TTL sweep).

        ``handoff`` rows only exist in databases from an older release; the current compression
        pipeline writes ``session_summary`` already ``active`` and has no pending phase.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conclusions SET status = 'expired', updated_at = ? "
                "WHERE kind = 'handoff' "
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

        Claiming is strictly FIFO *within a session*: a job is only eligible once every
        earlier job of the same session finished. That is what makes a same-turn base and
        review safe — the base row is published before the review reads it — even when the
        synchronous drain and the background worker compete for the queue.
        """
        with self._lock:
            if self._conn is None:
                return []
            order = "ORDER BY id"
            where = (
                "WHERE layer IN ('base', 'dialectic') AND status = 'pending' AND NOT EXISTS ("
                "SELECT 1 FROM jobs AS older WHERE older.session_id = jobs.session_id "
                "AND older.id < jobs.id AND older.status IN ('pending', 'running'))"
            )
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
        """Finish a job and advance its layer/session content watermark to *processed_ts*.

        A closed store is a no-op: a drain still running when the host tears the provider down
        must not raise out of the worker thread. The job stays ``running`` and is reclaimed by
        the next process.
        """
        with self._lock:
            if self._conn is None:
                return
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
        ``closed`` means the provider was torn down mid-drain; the row is left ``running`` for
        the next process to reclaim.
        """
        with self._lock:
            if self._conn is None:
                return "closed"
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

    def reclaim_open_jobs(self, *, before: float, max_attempts: int = 2) -> int:
        """Re-queue jobs a dead process left ``running``; returns the count.

        A claimed job's owner is a thread in one process: if that process ended, nothing will
        ever finish the row. Unlike ``expire_open_jobs`` this is safe when host history exists
        — the window is still readable — so the next drain retries it (and marks it failed once
        its attempts are spent). Jobs claimed by *this* process (``claimed_at >= before``) and
        jobs that were merely queued are left alone.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = CASE WHEN attempts < ? THEN 'pending' ELSE 'failed' END,"
                " claimed_at = NULL,"
                " error = CASE WHEN attempts < ? THEN '' ELSE 'abandoned: the process that claimed it ended' END,"
                " finished_at = CASE WHEN attempts < ? THEN NULL ELSE ? END"
                " WHERE status = 'running' AND COALESCE(claimed_at, created_at) < ?",
                (max(1, int(max_attempts)), max(1, int(max_attempts)),
                 max(1, int(max_attempts)), time.time(), float(before)),
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
            if self._conn is None:
                return 0
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE session_id = ? AND status = 'pending'",
                (session_id,)).fetchone()
        return int(row["n"] or 0) if row is not None else 0

    def open_job_count(self, session_id: str) -> int:
        with self._lock:
            if self._conn is None:
                return 0
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
                         ai_identity_card: str, window_summary: str,
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
                  window_summary or "", value_markers or "", now)
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
                    "ai_self_representation, ai_identity_card, window_summary, "
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
                    "window_summary = excluded.window_summary, "
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

    def latest_base_refresh_for_subject(self, subject: str) -> Optional[dict[str, Any]]:
        """The subject's newest refresh across sessions; the base layer's cross-session baseline.

        The user card and AI identity outlive a session, so a new session starts from the last
        published card rather than from an empty one. The global card files (three-day review)
        will outrank this; until they exist this is the inheritance path.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM base_refreshes WHERE subject = ? "
                "ORDER BY window_to_ts DESC, window_from_ts DESC, id DESC LIMIT 1",
                (subject or "",)).fetchone()
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

    def base_refreshes_since(self, since_ts: float, *, per_session: int = 1,
                             max_sessions: int = 20) -> list[dict[str, Any]]:
        """Newest refresh per session published at or after *since_ts* (the review's evidence)."""
        limit = max(1, min(500, int(max_sessions) * max(1, int(per_session)) * 5))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM base_refreshes WHERE window_to_ts >= ? "
                "ORDER BY window_to_ts DESC, id DESC LIMIT ?",
                (float(since_ts), limit)).fetchall()
        kept: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for row in rows:
            session = str(row["session_id"] or "")
            if counts.get(session, 0) >= max(1, int(per_session)):
                continue
            counts[session] = counts.get(session, 0) + 1
            kept.append(dict(row))
            if len(kept) >= max(1, int(max_sessions)):
                break
        return kept

    def marker_conclusions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Typed explicit markers, newest first (the review's second evidence source)."""
        limit = max(1, min(200, int(limit or 50)))
        marks = ",".join("?" * len(_MARKER_KINDS))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id AS conclusions_id, kind, content, created_at FROM conclusions "
                f"WHERE kind IN ({marks}) ORDER BY created_at DESC, id DESC LIMIT ?",
                (*_MARKER_KINDS, limit)).fetchall()
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

    def publish_base_conclusions(self, session_id: str, *, user_subject: str = "",
                                 ai_subject: str = "") -> int:
        """Publish the calibrated base fields as conclusions; once per session.

        One conclusion per calibrated column (``_BASE_CONCLUSION_KINDS``), content taken
        verbatim from the
        column (a JSON column stays the JSON string it holds). User and agent rows carry no
        session id so they stay durable; the window rows and the AI self representation carry
        the session. The guard is
        ``base_refreshes.conclusions_published_at`` scanned across the whole session, not an
        in-memory flag: a provider restart or a second instance cannot publish twice, and even a
        losing race only repeats the same six idempotent upserts. Returns the rows written.
        """
        with self._lock:
            row = self.latest_base_refresh(session_id)
            if row is None:
                return 0
            published = self._conn.execute(
                "SELECT 1 FROM base_refreshes WHERE session_id = ? "
                "AND conclusions_published_at IS NOT NULL LIMIT 1",
                (session_id,)).fetchone()
            if published is not None:
                return 0
            subject = user_subject or str(row.get("subject") or "")
            agent = ai_subject or subject
            written = 0
            for column, kind, scope in _BASE_CONCLUSION_KINDS:
                content = str(row.get(column) or "")
                if not content.strip():
                    continue
                self._put_conclusion_locked(
                    kind, _scope_key(scope, subject, session_id, agent), content,
                    session_id=session_id if scope == "session" else "",
                    subject=str(row.get("subject") or ""), evidence="[]", status="active",
                    version=1)
                written += 1
            self._conn.execute(
                "UPDATE base_refreshes SET conclusions_published_at = ? WHERE id = ?",
                (time.time(), int(row["id"])))
            self._conn.commit()
            return written

    def apply_base_revision(self, session_id: str, subject: str, *, window_from_ts: float,
                            changes: dict[str, str], changed_fields: list[str],
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
                    "UPDATE dialectic_refreshes SET is_latest = 0 "
                    "WHERE session_id = ? AND subject = ? AND is_latest = 1",
                    (session_id, subject))
                cur = self._conn.execute(
                    "INSERT INTO dialectic_refreshes (session_id, subject, base_refresh_id, "
                    "window_from_ts, window_to_ts, window_from_turn, window_to_turn, "
                    "user_representation, user_peer_card, ai_self_representation, "
                    "ai_identity_card, window_summary, value_markers, "
                    "changed_fields, source_job_id, is_latest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (session_id, subject, int(row["id"]), float(row["window_from_ts"]),
                     float(row["window_to_ts"]), row["window_from_turn"], row["window_to_turn"],
                     row["user_representation"], row["user_peer_card"],
                     row["ai_self_representation"], row["ai_identity_card"],
                     row["window_summary"], row["value_markers"],
                     json.dumps(changed_fields), int(source_job_id), time.time()))
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
                    "SELECT * FROM dialectic_refreshes WHERE session_id = ? AND subject = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (session_id, subject, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM dialectic_refreshes WHERE session_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def search_base_revisions(self, query: str, limit: int = 5, *,
                              session_id: str = "") -> list[dict[str, Any]]:
        """Search the six pre-overwrite fields (trigram FTS, LIKE fallback)."""
        terms = _terms(query)
        if not terms:
            return []
        capped = max(1, min(int(limit), 20))
        columns = _BASE_REVISION_COLUMNS
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
                "SELECT r.*, bm25(dialectic_refreshes_fts) AS rank FROM dialectic_refreshes_fts "
                "JOIN dialectic_refreshes r ON r.id = dialectic_refreshes_fts.rowid "
                f"WHERE dialectic_refreshes_fts MATCH ?{where} ORDER BY rank LIMIT ?")
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
                f"SELECT r.*, 0 AS rank FROM dialectic_refreshes r "
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
                       revision_id: int = 0, source: str = "dialectic",
                       review_id: int = 0, expires_at: float = 0.0,
                       adviser: str = "") -> list[int]:
        """Insert normalized advices, deduped by session + normalized text fingerprint.

        An advice may carry the emission guard's normalized ``fingerprint`` key; without it the
        fingerprint falls back to the lowercased text. ``adviser`` records which roster member
        produced the row (empty for dialectic candidates and pre-roster advice).
        """
        inserted: list[int] = []
        with self._lock:
            for item in advices:
                content = " ".join(str(item.get("content") or item.get("text") or "").split())
                if not content:
                    continue
                key = str(item.get("fingerprint") or content.lower())
                fingerprint = hashlib.sha256(
                    f"{session_id}\0{key}".encode("utf-8")).hexdigest()[:32]
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO advisories (revision_id, review_id, session_id, "
                    "source, severity, target, content, decision, evidence, fingerprint, status, "
                    "created_at, expires_at, turn, deferred, adviser) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (int(revision_id), int(review_id), session_id, str(source),
                     str(item.get("severity") or "concern"), str(item.get("target") or ""),
                     content, str(item.get("decision") or ""),
                     json.dumps(item.get("evidence") or []), fingerprint, time.time(),
                     float(expires_at or 0.0) or None, int(item.get("turn") or 0),
                     1 if str(item.get("deferred")).lower() in {"1", "true", "yes", "on"} else 0,
                     str(adviser or "")))
                if cur.rowcount:
                    inserted.append(int(cur.lastrowid))
            self._conn.commit()
        return inserted

    def advisory_fingerprints(self, session_id: str, keys: list[str]) -> dict[str, str]:
        """Map normalized advice key -> severity for rows already in this session's ledger.

        The emission guard asks once per review so a repeat of a delivered note is dropped
        while an escalation (same key, higher severity) still passes.
        """
        out: dict[str, str] = {}
        wanted = [str(key) for key in keys or [] if key]
        if not wanted or not session_id:
            return out
        hashes = {
            key: hashlib.sha256(f"{session_id}\0{key}".encode("utf-8")).hexdigest()[:32]
            for key in wanted
        }
        marks = ",".join("?" * len(hashes))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT fingerprint, severity FROM advisories "
                f"WHERE session_id = ? AND fingerprint IN ({marks})",
                (session_id, *hashes.values())).fetchall()
        by_hash = {value: key for key, value in hashes.items()}
        for row in rows:
            key = by_hash.get(str(row["fingerprint"]))
            if key:
                out[key] = str(row["severity"])
        return out

    def pending_blocker(self, session_id: str, *, source: str = "") -> Optional[dict[str, Any]]:
        """The oldest live blocker, if any. Used by the tool gate and the verify nudge."""
        clauses = ["session_id = ?", "status IN ('pending', 'staged')", "severity = 'blocker'",
                   "(expires_at IS NULL OR expires_at > ?)"]
        params: list[Any] = [session_id, time.time()]
        if source:
            clauses.append("source = ?")
            params.append(str(source))
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM advisories WHERE " + " AND ".join(clauses) + " ORDER BY id LIMIT 1",
                params).fetchone()
        return dict(row) if row else None

    def pending_advisories(self, session_id: str, limit: int = 8, *,
                           source: str = "") -> list[dict[str, Any]]:
        """Undelivered advices, blockers first, then concerns, then nits.

        ``source`` narrows the ledger to one producer (adviser mode mid-turn delivery uses it
        so it never re-injects a turn-boundary dialectic advice). Expired rows are excluded.
        """
        clauses = ["session_id = ?", "status = 'pending'",
                   "(expires_at IS NULL OR expires_at > ?)"]
        params: list[Any] = [session_id, time.time()]
        if source:
            clauses.append("source = ?")
            params.append(str(source))
        params.append(max(1, min(int(limit), 32)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM advisories WHERE " + " AND ".join(clauses) + " "
                "ORDER BY CASE severity WHEN 'blocker' THEN 0 WHEN 'concern' THEN 1 ELSE 2 END, "
                "id LIMIT ?", params).fetchall()
        return [dict(row) for row in rows]

    def mark_advisories_injected(self, ids: list[int], turn_id: str = "") -> int:
        if not ids:
            return 0
        marks = ", ".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE advisories SET status = 'injected', injected_at = ?, turn_id = ? "
                f"WHERE id IN ({marks}) AND status IN ('pending', 'staged')",
                (time.time(), turn_id or "", *[int(item) for item in ids]))
            self._conn.commit()
            return int(cur.rowcount)

    def release_advisories(self, ids: list[int]) -> int:
        """Return injected rows to ``pending`` after an accepted host delivery did not land.

        A continuation is accepted while the turn is live but drained later by the stop gate;
        if that turn ends first (error, interrupt, a new surface), the advice never reached the
        model. Only ``injected`` rows are touched, so an independent delivery is not undone.
        """
        if not ids:
            return 0
        marks = ", ".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE advisories SET status = 'pending', request_id = '', turn_id = '', "
                f"injected_at = NULL "
                f"WHERE id IN ({marks}) AND status = 'injected'",
                (*[int(item) for item in ids],))
            self._conn.commit()
            return int(cur.rowcount)

    def stage_advisories(self, ids: list[int], request_id: str) -> int:
        """Mark a request's injected advice as staged; ``post_api_request`` confirms delivery."""
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE advisories SET status = 'staged', request_id = ? "
                f"WHERE id IN ({marks}) AND status IN ('pending', 'staged')",
                (str(request_id or ""), *[int(item) for item in ids]))
            self._conn.commit()
            return int(cur.rowcount)

    def staged_advisories(self, request_id: str) -> list[dict[str, Any]]:
        """Advice already staged for this request (a retry re-injects the same block)."""
        if not request_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM advisories WHERE status = 'staged' AND request_id = ? "
                "ORDER BY CASE severity WHEN 'blocker' THEN 0 WHEN 'concern' THEN 1 ELSE 2 END, "
                "id", (str(request_id),)).fetchall()
        return [dict(row) for row in rows]

    def mark_advisories_delivered(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE advisories SET status = 'delivered', delivered_at = ? "
                f"WHERE id IN ({marks}) AND status IN ('pending', 'staged')",
                (time.time(), *[int(item) for item in ids]))
            self._conn.commit()
            return int(cur.rowcount)

    def reset_staged(self, ids: list[int]) -> int:
        """A failed provider attempt did not deliver the advice; make it pending again."""
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE advisories SET status = 'pending', request_id = '' "
                f"WHERE id IN ({marks}) AND status = 'staged'",
                (*[int(item) for item in ids],))
            self._conn.commit()
            return int(cur.rowcount)

    def expire_adviser_advices(self, session_id: str) -> int:
        """Drop adviser-mode advice when its session/mode ends; dialectic audit rows stay."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE advisories SET status = 'expired' "
                "WHERE session_id = ? AND source = 'adviser_mode' "
                "AND status IN ('pending', 'staged')", (session_id,))
            self._conn.commit()
            return int(cur.rowcount)

    def review_stats(self, session_id: str, window: int = 8,
                     targets: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Per-target coverage and advice counters over this session's last *window* reviews.

        Counts only: no transcript, no advice text. ``follow_up`` is re-observation, not
        adoption — the plugin cannot see whether the operator acted on a note. Safety targets
        sort first, then by how strongly the target asks to be rechecked.
        """
        names = tuple(str(name) for name in targets if str(name).strip())
        if not names:
            return []
        limit = max(1, min(int(window or 8), 50))
        stats: dict[str, dict[str, Any]] = {
            name: {"target": name, "reviews": 0, "clean": 0, "found": 0, "n/a": 0,
                   "adv": 0, "deliv": 0, "recur": 0, "follow_up": "no_followup"}
            for name in names}
        with self._lock:
            if self._conn is None:
                return []
            reviews = self._conn.execute(
                "SELECT id, coverage, drop_detail FROM adviser_reviews "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
            review_ids = [int(row["id"]) for row in reviews]
            for row in reviews:
                try:
                    coverage = json.loads(str(row["coverage"] or "{}"))
                except (TypeError, ValueError):
                    coverage = {}
                try:
                    dropped = json.loads(str(row["drop_detail"] or "{}"))
                except (TypeError, ValueError):
                    dropped = {}
                for name in names:
                    state = str((coverage or {}).get(name) or "").strip().lower()
                    if state in {"clean", "found", "n/a"}:
                        stats[name]["reviews"] += 1
                        stats[name][state] += 1
                    stats[name]["recur"] += int((dropped or {}).get(f"duplicate/{name}") or 0)
            if review_ids:
                marks = ", ".join("?" * len(review_ids))
                for row in self._conn.execute(
                        f"SELECT target, COUNT(*) AS n, "
                        f"SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS d "
                        f"FROM advisories WHERE review_id IN ({marks}) GROUP BY target",
                        review_ids).fetchall():
                    name = str(row["target"] or "")
                    if name in stats:
                        stats[name]["adv"] += int(row["n"] or 0)
                        stats[name]["deliv"] += int(row["d"] or 0)
        rows: list[dict[str, Any]] = []
        for name in names:
            item = stats[name]
            if item["recur"]:
                item["follow_up"] = "recurred"
            elif item["adv"]:
                item["follow_up"] = "not_reobserved"
            rows.append(item)
        safety = {"soul_tool_policy_check": 0, "soul_workflow_check": 0}
        rank = {"recurred": 0, "not_reobserved": 2, "no_followup": 3}
        rows.sort(key=lambda item: (safety.get(item["target"], 1),
                                    rank.get(item["follow_up"], 3),
                                    names.index(item["target"])))
        return rows

    def start_adviser_review(self, session_id: str, turn_id: str, trigger: str,
                             from_seq: int, to_seq: int, adviser: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO adviser_reviews (session_id, turn_id, trigger, status, "
                "from_seq, to_seq, adviser, created_at) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
                (session_id, turn_id or "", trigger or "", int(from_seq), int(to_seq),
                 str(adviser or ""), time.time()))
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_adviser_review(self, review_id: int, *, status: str, model: str = "",
                              api_calls: int = 0, latency_ms: float = 0.0,
                              error: str = "", notes_dropped: int = 0,
                              drop_detail: str = "", coverage: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE adviser_reviews SET status = ?, model = ?, api_calls = ?, "
                "latency_ms = ?, error = ?, notes_dropped = ?, drop_detail = ?, coverage = ? "
                "WHERE id = ?",
                (status, model or "", int(api_calls or 0), float(latency_ms or 0.0),
                 (error or "")[:500], int(notes_dropped or 0),
                 (drop_detail or "{}")[:2000], (coverage or "{}")[:2000], int(review_id)))
            self._conn.commit()

    def adviser_reviews(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM adviser_reviews WHERE session_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (session_id, max(1, min(50, int(limit or 10))))).fetchall()
        return [dict(row) for row in rows]

    def adviser_mode_stats(self, session_id: str) -> dict[str, Any]:
        """Per-adviser review/advice counters for ``minir_stats``.

        Review rows carry the emission guard's drop counts, so an adviser whose output is
        entirely noise is visible instead of looking like a quiet reviewer.
        """
        with self._lock:
            reviews = self._conn.execute(
                "SELECT adviser, status, notes_dropped, drop_detail FROM adviser_reviews "
                "WHERE session_id = ?", (session_id,)).fetchall()
            advice_rows = self._conn.execute(
                "SELECT adviser, severity, status, COUNT(*) AS n FROM advisories "
                "WHERE session_id = ? AND source = 'adviser_mode' "
                "GROUP BY adviser, severity, status", (session_id,)).fetchall()
        stats: dict[str, Any] = {"reviews": len(reviews), "by_adviser": {},
                                 "advisories": {}, "dropped": {}}
        for row in reviews:
            name = str(row["adviser"] or "default")
            entry = stats["by_adviser"].setdefault(
                name, {"reviews": 0, "advised": 0, "silent": 0, "failed": 0, "dropped": 0})
            entry["reviews"] += 1
            status = str(row["status"] or "")
            if status in entry:
                entry[status] += 1
            entry["dropped"] += int(row["notes_dropped"] or 0)
            try:
                detail = json.loads(row["drop_detail"] or "{}")
            except (TypeError, ValueError):
                detail = {}
            if isinstance(detail, dict):
                for reason, count in detail.items():
                    stats["dropped"][str(reason)] = (
                        int(stats["dropped"].get(str(reason), 0)) + int(count or 0))
        for row in advice_rows:
            key = f"{row['severity']}/{row['status']}"
            stats["advisories"][key] = int(stats["advisories"].get(key, 0)) + int(row["n"])
        return stats

    def has_advisories(self, session_id: str) -> bool:
        """Whether a non-expired advice ledger exists (expired adviser rows must not suppress
        the legacy review forever after the mode is switched off)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM advisories WHERE session_id = ? AND status <> 'expired' LIMIT 1",
                (session_id,)).fetchone()
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
                   "ai_identity_card", "window_summary", "value_markers")
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
    def _visibility_sql(session_id: str, scope: str = "session") -> tuple[str, list[Any]]:
        """SQL predicate + params for the requested recall scope.

        ``scope="session"`` (the default) is the current conversation's view: its own rows plus
        every durable row (no session id) whose kind is cross-session by contract. A new
        session-level kind is therefore private by default; a new durable kind has to be added
        to this predicate. ``scope="all"`` drops the isolation; ``search`` then excludes the
        session-bound kinds itself, because this predicate cannot see the requested ``kinds``.
        """
        if scope == "all":
            return "1=1", []
        durable_kinds = (*_LONG_TERM_KINDS, *CONCLUSION_KINDS)
        marks = ",".join("?" * len(durable_kinds))
        durable = f"(c.session_id = '' AND c.kind IN ({marks}))"
        if session_id:
            return f"(c.session_id = ? OR {durable})", [session_id, *durable_kinds]
        return durable, list(durable_kinds)

    def search(self, query: str, limit: int = 5, *, session_id: str = "",
               scope: str = "session",
               kinds: Optional[frozenset[str]] = None) -> list[dict[str, Any]]:
        """Ranked matches across conclusions, scoped to *session_id* or every session.

        FTS5 with the trigram tokenizer is the primary index (CJK-safe). A scan over the same
        rows is the fallback when FTS5 is unavailable or the query terms are too short for
        trigram. Returns ``{conclusions_id, kind, key, text, when, author, subject, session_id,
        ``score, rank}``; there is no window field because a conclusion has none. ``scope="all"``
        searches every session but never returns a session-bound kind; naming only session-bound
        kinds there, or reading one with no session id, raises :class:`SessionScopeError`
        (``session_scope_required``) instead of returning an empty list. ``kinds`` restricts the
        SQL candidate set, so a filtered recall cannot be crowded out by rows the caller would
        discard.
        """
        terms = _terms(query)
        if not terms:
            return []
        if kinds is not None and not kinds:
            return []
        capped = max(1, min(int(limit), 20))
        scope = str(scope).lower()
        if scope != "all":
            scope = "session"
        bound = frozenset(_SESSION_BOUND_KINDS)
        explicit = frozenset(kinds) if kinds is not None else None
        if explicit:
            if explicit <= bound and (scope == "all" or not session_id):
                raise SessionScopeError(_session_scope_message(explicit, scope, session_id))
            if scope == "all" and explicit & bound:
                explicit = explicit - bound
                if not explicit:
                    return []
        visibility, visible_params = self._visibility_sql(session_id, scope)
        if scope == "all":
            # A generic cross-session query must not surface another conversation's ruler;
            # only an explicit session scope (with its id) may read it.
            marks = ", ".join("?" * len(bound))
            visibility = f"({visibility} AND c.kind NOT IN ({marks}))"
            visible_params = [*visible_params, *bound]
        kinds = explicit
        kind_sql, kind_params = "", []
        if kinds is not None:
            marks = ",".join("?" * len(kinds))
            kind_sql = f" AND c.kind IN ({marks})"
            kind_params = list(kinds)
        rows: list[dict[str, Any]] = []
        # Historical chunk summaries stay recallable inside their own session after a final
        # summary supersedes them; the visibility predicate keeps them from leaking elsewhere.
        status = "(c.status = 'active' OR c.kind = 'window_summary')"
        fts_terms = [term for term in terms if len(term) >= 3]
        if self._fts and fts_terms:
            match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms)
            sql = (
                "SELECT c.*, bm25(conclusions_fts) AS rank FROM conclusions_fts "
                "JOIN conclusions c ON c.id = conclusions_fts.rowid "
                f"WHERE conclusions_fts MATCH ? AND {status} AND {visibility}{kind_sql} "
                "ORDER BY rank LIMIT ?")
            try:
                with self._lock:
                    found = self._conn.execute(
                        sql, [match, *visible_params, *kind_params, _MAX_SCAN]).fetchall()
                rows = [dict(row) for row in found]
            except sqlite3.OperationalError as exc:
                logger.debug("Minir FTS query failed, falling back to scan: %s", exc)
        if not rows:
            where = " OR ".join("lower(c.content) LIKE ?" for _ in terms)
            sql = (
                f"SELECT c.*, 0 AS rank FROM conclusions c WHERE {status} "
                f"AND {visibility}{kind_sql} AND ({where}) "
                "ORDER BY c.updated_at DESC LIMIT ?")
            with self._lock:
                found = self._conn.execute(
                    sql, [*visible_params, *kind_params,
                          *[f"%{term}%" for term in terms], _MAX_SCAN]).fetchall()
            rows = [dict(row) for row in found]

        results: list[dict[str, Any]] = []
        for row in rows:
            matched = str(row.get("content") or "")
            lowered = matched.lower()
            overlap = sum(1 for term in terms if term in lowered)
            if not overlap:
                continue
            kind = str(row.get("kind") or "")
            results.append({
                "conclusions_id": int(row.get("id") or 0),
                "kind": kind,
                "key": row.get("key") or "",
                "text": matched,
                "when": float(row.get("updated_at") or row.get("created_at") or 0),
                "author": row.get("subject") or kind,
                "subject": row.get("subject") or "",
                "session_id": row.get("session_id") or "",
                "score": float(overlap) + _KIND_WEIGHT.get(kind, 0.5),
                "rank": float(row.get("rank") or 0),
            })
        results.sort(key=lambda item: (item["score"], -item["rank"], item["when"],
                                       item["conclusions_id"]), reverse=True)
        return results[:capped]

    # -- card revisions (three-day review) -----------------------------------

    def add_card_revision(self, scope: str, part: str, path: str, *, before_hash: str,
                          after_hash: str, diff: str = "", changed_fields: Any = None,
                          source: str = "review", evidence: Any = None, reviewer: str = "",
                          status: str = "applied") -> int:
        """Record one card-file write. The file is canonical; this row is its history."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO card_revisions (scope, part, path, reviewed_at, before_hash, "
                "after_hash, diff, changed_fields, source, evidence, reviewer, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scope or "", part or "", path or "", time.time(), before_hash or "",
                 after_hash or "", diff or "", json.dumps(changed_fields or []),
                 source or "review", json.dumps(evidence or []), reviewer or "", status or "applied"))
            self._conn.commit()
            return int(cur.lastrowid)

    def card_revisions(self, scope: str = "", part: str = "", limit: int = 20
                       ) -> list[dict[str, Any]]:
        """Revision history, newest first, optionally filtered by scope and part."""
        limit = max(1, min(200, int(limit or 20)))
        with self._lock:
            if scope or part:
                rows = self._conn.execute(
                    "SELECT * FROM card_revisions WHERE scope = ? AND part = ? "
                    "ORDER BY reviewed_at DESC, id DESC LIMIT ?",
                    (scope or "", part or "", limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM card_revisions ORDER BY reviewed_at DESC, id DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(row) for row in rows]

    def latest_card_revision(self, scope: str, part: str) -> Optional[dict[str, Any]]:
        rows = self.card_revisions(scope, part, limit=1)
        return rows[0] if rows else None

    # -- session finalisation: idle timer + per-turn usage ---------------------

    def session_markers(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Typed explicit markers written by one session, oldest first (the extraction input)."""
        limit = max(1, min(200, int(limit or 50)))
        marks = ",".join("?" * len(_MARKER_KINDS))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id AS conclusions_id, kind, content, created_at FROM conclusions "
                f"WHERE session_id = ? AND kind IN ({marks}) "
                "ORDER BY created_at, id LIMIT ?",
                (str(session_id or ""), *_MARKER_KINDS, limit)).fetchall()
        return [dict(row) for row in rows]

    def schedule_extraction(self, session_id: str, *, due_at: float, markers: str = "{}",
                            window_from_ts: float = 0.0, window_to_ts: float = 0.0,
                            turns: int = 0) -> None:
        """Arm (or re-arm) one session's idle timer; re-ending a session resets its attempt count."""
        session = str(session_id or "")
        if not session:
            return
        stamp = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO session_extractions "
                "(session_id, status, due_at, markers, window_from_ts, window_to_ts, turns, "
                "attempts, pages, summary_chars, error, created_at, updated_at, started_at, "
                "finished_at) VALUES (?, 'pending', ?, ?, ?, ?, ?, 0, 0, 0, '', ?, ?, NULL, NULL) "
                "ON CONFLICT(session_id) DO UPDATE SET status = 'pending', "
                "due_at = excluded.due_at, markers = excluded.markers, "
                "window_from_ts = excluded.window_from_ts, "
                "window_to_ts = excluded.window_to_ts, turns = excluded.turns, attempts = 0, "
                "pages = 0, summary_chars = 0, error = '', "
                "updated_at = excluded.updated_at, started_at = NULL, finished_at = NULL",
                (session, float(due_at), str(markers or "{}"), float(window_from_ts),
                 float(window_to_ts), max(0, int(turns or 0)), stamp, stamp))
            self._conn.commit()

    def cancel_extraction(self, session_id: str) -> int:
        """Cancel a *pending* timer because the operator typed in the session again."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM session_extractions WHERE session_id = ? AND status = 'pending'",
                (str(session_id or ""),))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def extraction(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_extractions WHERE session_id = ?",
                (str(session_id or ""),)).fetchone()
        return dict(row) if row is not None else None

    def due_extractions(self, now: float, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit or 5)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM session_extractions WHERE status = 'pending' AND due_at <= ? "
                "ORDER BY due_at, session_id LIMIT ?",
                (float(now), limit)).fetchall()
        return [dict(row) for row in rows]

    def claim_extraction(self, session_id: str, *, now: Optional[float] = None) -> bool:
        """pending -> running atomically; False when another process already claimed it."""
        stamp = time.time() if now is None else float(now)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE session_extractions SET status = 'running', started_at = ?, "
                "updated_at = ? WHERE session_id = ? AND status = 'pending'",
                (stamp, stamp, str(session_id or "")))
            self._conn.commit()
            return bool(cur.rowcount)

    def finish_extraction(self, session_id: str, *, pages: int = 0,
                          summary_chars: int = 0) -> None:
        stamp = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE session_extractions SET status = 'done', pages = ?, summary_chars = ?, "
                "error = '', finished_at = ?, updated_at = ? WHERE session_id = ?",
                (max(0, int(pages or 0)), max(0, int(summary_chars or 0)), stamp, stamp,
                 str(session_id or "")))
            self._conn.commit()

    def fail_extraction(self, session_id: str, error: str, *,
                        retry_at: Optional[float] = None, max_attempts: int = 2) -> str:
        """Record a failed run: retry once when *retry_at* is given, then park it as failed."""
        session = str(session_id or "")
        stamp = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM session_extractions WHERE session_id = ?",
                (session,)).fetchone()
            attempts = int(row["attempts"] or 0) + 1 if row is not None else 1
            retry = retry_at is not None and attempts < max(1, int(max_attempts))
            self._conn.execute(
                "UPDATE session_extractions SET attempts = ?, status = ?, due_at = ?, error = ?, "
                "updated_at = ?, finished_at = ? WHERE session_id = ?",
                (attempts, "pending" if retry else "failed",
                 float(retry_at) if retry else stamp, str(error or "")[:1_000], stamp,
                 None if retry else stamp, session))
            self._conn.commit()
        return "retried" if retry else "failed"

    def pending_extraction_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM session_extractions "
                "WHERE status IN ('pending', 'running')").fetchone()
        return int(row["n"] or 0)

    def record_turn_usage(self, session_id: str, turn_id: str, *,
                          prompt_tokens: int = 0, completion_tokens: int = 0,
                          total_tokens: int = 0, cache_read_tokens: int = 0,
                          cache_write_tokens: int = 0, reasoning_tokens: int = 0,
                          api_calls: int = 1, model: str = "") -> None:
        """Fold one API request's usage into its turn's running total."""
        session = str(session_id or "")
        turn = str(turn_id or "")
        if not session or not turn:
            return
        stamp = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO turn_usage (session_id, turn_id, model, api_calls, prompt_tokens, "
                "completion_tokens, total_tokens, cache_read_tokens, cache_write_tokens, "
                "reasoning_tokens, first_at, updated_at, final_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(session_id, turn_id) DO UPDATE SET "
                "model = CASE WHEN excluded.model <> '' THEN excluded.model "
                "ELSE turn_usage.model END, "
                "api_calls = turn_usage.api_calls + excluded.api_calls, "
                "prompt_tokens = turn_usage.prompt_tokens + excluded.prompt_tokens, "
                "completion_tokens = turn_usage.completion_tokens + excluded.completion_tokens, "
                "total_tokens = turn_usage.total_tokens + excluded.total_tokens, "
                "cache_read_tokens = turn_usage.cache_read_tokens + excluded.cache_read_tokens, "
                "cache_write_tokens = turn_usage.cache_write_tokens + excluded.cache_write_tokens, "
                "reasoning_tokens = turn_usage.reasoning_tokens + excluded.reasoning_tokens, "
                "updated_at = excluded.updated_at, final_at = NULL",
                (session, turn, str(model or "")[:120], max(0, int(api_calls or 0)),
                 max(0, int(prompt_tokens or 0)), max(0, int(completion_tokens or 0)),
                 max(0, int(total_tokens or 0)), max(0, int(cache_read_tokens or 0)),
                 max(0, int(cache_write_tokens or 0)), max(0, int(reasoning_tokens or 0)),
                 stamp, stamp))
            self._conn.commit()

    def finalize_session_usage(self, session_id: str) -> dict[str, Any]:
        """Close every turn row for the session and return the per-session totals."""
        session = str(session_id or "")
        stamp = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE turn_usage SET final_at = ? WHERE session_id = ? AND final_at IS NULL",
                (stamp, session))
            row = self._conn.execute(
                "SELECT COUNT(*) AS turns, COALESCE(SUM(api_calls), 0) AS api_calls, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
                "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
                "COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens, "
                "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens "
                "FROM turn_usage WHERE session_id = ?", (session,)).fetchone()
            self._conn.commit()
        return {key: int(row[key] or 0) for key in (
            "turns", "api_calls", "prompt_tokens", "completion_tokens", "total_tokens",
            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")} if row else {}

    def record_session_usage(self, session_id: str, totals: dict[str, Any], *,
                             host: Optional[dict[str, Any]] = None) -> None:
        """Store one session's final usage, with the host's own aggregate beside it."""
        session = str(session_id or "")
        if not session:
            return
        values = totals or {}
        host_row = host or {}

        def number(source: dict[str, Any], key: str, default: Any = 0) -> int:
            try:
                return max(0, int(source.get(key) if source.get(key) is not None else default))
            except (TypeError, ValueError):
                return 0

        stamp = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO session_usage (session_id, turns, api_calls, prompt_tokens, "
                "completion_tokens, total_tokens, cache_read_tokens, cache_write_tokens, "
                "reasoning_tokens, host_prompt_tokens, host_completion_tokens, "
                "host_total_tokens, finalized_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET turns = excluded.turns, "
                "api_calls = excluded.api_calls, prompt_tokens = excluded.prompt_tokens, "
                "completion_tokens = excluded.completion_tokens, "
                "total_tokens = excluded.total_tokens, "
                "cache_read_tokens = excluded.cache_read_tokens, "
                "cache_write_tokens = excluded.cache_write_tokens, "
                "reasoning_tokens = excluded.reasoning_tokens, "
                "host_prompt_tokens = excluded.host_prompt_tokens, "
                "host_completion_tokens = excluded.host_completion_tokens, "
                "host_total_tokens = excluded.host_total_tokens, "
                "finalized_at = excluded.finalized_at",
                (session, number(values, "turns"), number(values, "api_calls"),
                 number(values, "prompt_tokens"), number(values, "completion_tokens"),
                 number(values, "total_tokens"), number(values, "cache_read_tokens"),
                 number(values, "cache_write_tokens"), number(values, "reasoning_tokens"),
                 number(host_row, "input_tokens", number(host_row, "prompt_tokens")),
                 number(host_row, "output_tokens", number(host_row, "completion_tokens")),
                 number(host_row, "total_tokens",
                        number(host_row, "input_tokens") + number(host_row, "output_tokens")),
                 stamp))
            self._conn.commit()

    def session_usage(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_usage WHERE session_id = ?",
                (str(session_id or ""),)).fetchone()
        return dict(row) if row is not None else {}

    def turn_usage(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """This session's per-turn usage, newest first (the final value is what on_session_end wrote)."""
        limit = max(1, min(100, int(limit or 20)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM turn_usage WHERE session_id = ? "
                "ORDER BY updated_at DESC, turn_id DESC LIMIT ?",
                (str(session_id or ""), limit)).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """Row counts and on-disk size, for the ``minir_stats`` tool."""
        with self._lock:
            sessions = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM jobs").fetchone()["n"]
            jobs = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'").fetchone()["n"]
            failed = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = 'failed'").fetchone()["n"]
            states = self._conn.execute("SELECT COUNT(*) AS n FROM layer_state").fetchone()["n"]
            refreshes = self._conn.execute("SELECT COUNT(*) AS n FROM base_refreshes").fetchone()["n"]
            revisions = self._conn.execute(
                "SELECT COUNT(*) AS n FROM dialectic_refreshes").fetchone()["n"]
            advices = self._conn.execute("SELECT COUNT(*) AS n FROM advisories").fetchone()["n"]
            reviews = self._conn.execute("SELECT COUNT(*) AS n FROM adviser_reviews").fetchone()["n"]
            conclusions = self._conn.execute("SELECT COUNT(*) AS n FROM conclusions").fetchone()["n"]
            metrics = self._conn.execute("SELECT COUNT(*) AS n FROM metrics").fetchone()["n"]
            turn_rows = self._conn.execute(
                "SELECT COUNT(*) AS n FROM turn_usage").fetchone()["n"]
            extractions = self._conn.execute(
                "SELECT COUNT(*) AS n FROM session_extractions").fetchone()["n"]
            pending_extractions = self._conn.execute(
                "SELECT COUNT(*) AS n FROM session_extractions "
                "WHERE status IN ('pending', 'running')").fetchone()["n"]
            usage_sessions = self._conn.execute(
                "SELECT COUNT(*) AS n FROM session_usage").fetchone()["n"]
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {"sessions": int(sessions),
                "jobs": int(jobs), "pending_jobs": int(pending), "failed_jobs": int(failed),
                "layer_states": int(states), "base_refreshes": int(refreshes),
                "base_revisions": int(revisions), "advisories": int(advices),
                "adviser_reviews": int(reviews),
                "conclusions": int(conclusions),
                "metrics": int(metrics), "fts": bool(self._fts),
                "turn_usage": int(turn_rows), "session_extractions": int(extractions),
                "pending_extractions": int(pending_extractions),
                "usage_sessions": int(usage_sessions),
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
        """Close the connection; safe to call twice.

        The worker drain can still be unwinding when the host tears the provider down, so the
        job methods that path uses (``claim_jobs``, ``complete_job``, ``fail_job``, the two job
        counts) treat a closed connection as "store is gone" instead of raising into that thread.
        """
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]


def format_when(created_at: float) -> str:
    """Epoch seconds -> compact UTC timestamp for recall blocks (``2026-10-16 11:04``)."""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(created_at))
