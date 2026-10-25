"""Optional vector recall over the conclusions table (sqlite-vec + an OpenAI-compatible API).

The module is deliberately fail-open: a missing package, key, endpoint or a bad dimension makes
the caller fall back to bm25 for that turn. It never raises into a search and never writes a
half vector row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

DEFAULTS = {
    "enabled": True,
    "provider": "siliconflow",
    "model": "BAAI/bge-m3",
    "dim": 1024,
    "base_url": "https://api.siliconflow.com/v1",
    "api_key": "",
}


class VectorIndex:
    """One vec0 table beside the conclusions; ranking only, never the source of truth."""

    def __init__(self, store: Any, config: Optional[dict[str, Any]] = None):
        self._store = store
        settings = dict(DEFAULTS)
        settings.update({key: value for key, value in (config or {}).items()
                         if value not in (None, "")})
        self.settings = settings
        self.dim = max(1, int(settings.get("dim") or 1024))
        self._loaded: Optional[bool] = None
        self._ready = False
        self._cache: dict[tuple[str, int], list[float]] = {}
        self._warned_key = False
        self._queue: "queue.Queue[tuple[int, str]]" = queue.Queue(maxsize=256)
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

    # -- availability ---------------------------------------------------------

    def _conn(self) -> Any:
        return getattr(self._store, "_conn", None)

    def available(self) -> bool:
        if self._loaded is not None:
            return self._loaded
        conn = self._conn()
        if conn is None or not bool(self.settings.get("enabled", True)):
            self._loaded = False
            return False
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._loaded = True
        except Exception as exc:
            logger.warning("minir vector: sqlite-vec unavailable, bm25 only: %s", exc)
            self._loaded = False
        return bool(self._loaded)

    def _key(self) -> str:
        return str(self.settings.get("api_key") or os.environ.get(
            "SILICONFLOW_API_KEY", "") or "")

    def _meta(self, key: str, conn: Any = None) -> Optional[str]:
        conn = conn if conn is not None else self._conn()
        try:
            row = conn.execute("SELECT value FROM vector_meta WHERE key = ?", (key,)).fetchone()
        except Exception:
            return None
        return None if row is None else str(row[0])

    def _set_meta(self, conn: Any, key: str, value: Any) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, "
                     "value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO vector_meta (key, value) VALUES (?, ?)",
                     (key, str(value)))

    def _table(self) -> str:
        """The active vec table; a migration moves this pointer, never renames a table."""
        name = str(self._meta("table") or "conclusions_vec")
        return name if _TABLE_RE.fullmatch(name) else "conclusions_vec"

    def _pending_table(self) -> str:
        digest = hashlib.sha1(str(self.settings.get("model")).encode("utf-8")).hexdigest()[:8]
        return "conclusions_vec_%d_%s" % (self.dim, digest)

    def ensure_table(self) -> bool:
        if self._ready:
            return True
        if not self.available():
            return False
        conn = self._conn()
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, "
                             "value TEXT NOT NULL)")
                if self._meta("pending_dim", conn) is not None:
                    return False  # a migration is queued: bm25 until it finishes
                table = self._table()
                existing = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,)).fetchone()
                stored_dim = self._meta("dim", conn)
                stored_model = self._meta("model", conn)
                model = str(self.settings.get("model") or "")
                differs = (
                    (existing is not None and f"FLOAT[{self.dim}]" not in str(existing[0] or ""))
                    or (stored_dim is not None and int(stored_dim) != self.dim)
                    or (stored_model is not None and stored_model != model))
                if differs:
                    return self._plan_migration(conn, table)
                if existing is None:
                    conn.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS %s USING vec0("
                        "conclusion_id INTEGER PRIMARY KEY, embedding FLOAT[%d])"
                        % (table, self.dim))
                self._set_meta(conn, "dim", self.dim)
                self._set_meta(conn, "model", model)
                self._set_meta(conn, "provider", self.settings.get("provider"))
                self._set_meta(conn, "table", table)
            self._ready = True
            return True
        except Exception as exc:
            logger.warning("minir vector: table unavailable: %s", exc)
            return False

    def _plan_migration(self, conn: Any, old_table: str) -> bool:
        """Keep the old vectors and queue a re-embed; nothing is dropped on the startup path."""
        self._set_meta(conn, "pending_dim", self.dim)
        self._set_meta(conn, "pending_model", self.settings.get("model"))
        self._set_meta(conn, "pending_provider", self.settings.get("provider"))
        self._set_meta(conn, "pending_table", self._pending_table())
        self._set_meta(conn, "migration_cursor", 0)
        self._set_meta(conn, "table", old_table)
        self._ready = False
        logger.warning("minir vector: dim/model changed; migration queued to %s",
                       self._pending_table())
        return False

    # -- embedding ------------------------------------------------------------

    def embed(self, text: str, *, dim: Optional[int] = None) -> Optional[list[float]]:
        expected = max(1, int(dim or self.dim))
        flat = " ".join(str(text or "").split())
        if not flat:
            return None
        cached = self._cache.get((flat, expected))
        if cached is not None:
            return cached
        key = self._key()
        if not key:
            if not self._warned_key:
                logger.warning("minir vector: no API key configured, bm25 only")
                self._warned_key = True
            return None
        body = json.dumps({"model": self.settings.get("model"), "input": flat}).encode("utf-8")
        request = urllib.request.Request(
            str(self.settings.get("base_url")).rstrip("/") + "/embeddings", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=8.0) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            vector = payload["data"][0]["embedding"]
        except Exception as exc:
            logger.warning("minir vector: embedding failed, bm25 only: %s", exc)
            return None
        if not isinstance(vector, list) or len(vector) != expected:
            logger.warning("minir vector: embedding dimension %s != %s; refused",
                           len(vector) if isinstance(vector, list) else "?", expected)
            return None
        values = [float(item) for item in vector]
        if len(self._cache) >= 128:
            self._cache.pop(next(iter(self._cache)))
        self._cache[(flat, expected)] = values
        return values

    # -- reads and writes -----------------------------------------------------

    def recall(self, query: str, limit: int = 20) -> dict[int, int]:
        """``{conclusion_id: rank}`` for a query; empty on any degradation."""
        if not self.ensure_table():
            return {}
        vector = self.embed(query)
        if vector is None:
            return {}
        try:
            import sqlite_vec
            rows = self._conn().execute(
                "SELECT conclusion_id FROM %s WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?" % self._table(),
                (sqlite_vec.serialize_float32(vector), max(1, int(limit)))).fetchall()
        except Exception as exc:
            logger.warning("minir vector: recall failed, bm25 only: %s", exc)
            return {}
        return {int(row[0]): rank for rank, row in enumerate(rows)}

    def index(self, conclusion_id: int, content: str) -> bool:
        if not self.ensure_table():
            return False
        vector = self.embed(content)
        if vector is None:
            return False
        try:
            import sqlite_vec
            with self._conn():
                self._conn().execute(
                    "INSERT OR REPLACE INTO %s (conclusion_id, embedding) VALUES (?, ?)"
                    % self._table(),
                    (int(conclusion_id), sqlite_vec.serialize_float32(vector)))
            return True
        except Exception as exc:
            logger.warning("minir vector: index failed: %s", exc)
            return False

    def backfill(self, limit: int = 50) -> int:
        """Index conclusions missing from the vec table, oldest first."""
        if not self.ensure_table():
            return 0
        conn = self._conn()
        try:
            indexed = {int(row[0]) for row in conn.execute(
                "SELECT conclusion_id FROM %s" % self._table()).fetchall()}
            rows = conn.execute(
                "SELECT id, content FROM conclusions ORDER BY id LIMIT ?",
                (max(1, int(limit)) + len(indexed),)).fetchall()
        except Exception as exc:
            logger.warning("minir vector: backfill read failed: %s", exc)
            return 0
        written = 0
        for row in rows:
            if int(row[0]) in indexed:
                continue
            if self.index(int(row[0]), str(row[1] or "")):
                written += 1
            if written >= max(1, int(limit)):
                break
        return written

    # -- dimension migration --------------------------------------------------

    def migrate(self, batch: int = 50) -> dict[str, Any]:
        """Re-embed conclusions into the pending table, resumable from the stored id cursor.

        The active table stays untouched until the last batch lands, so a fresh model never
        empties recall. A failure or a restart resumes from ``migration_cursor``.
        """
        if not self.available():
            return {"status": "unavailable", "done": 0}
        conn = self._conn()
        pending_dim = self._meta("pending_dim")
        if pending_dim is None:
            return {"status": "none", "done": 0}
        target_dim = max(1, int(pending_dim))
        table = str(self._meta("pending_table") or self._pending_table())
        if not _TABLE_RE.fullmatch(table):
            table = self._pending_table()
        cursor = int(self._meta("migration_cursor") or 0)
        size = max(1, int(batch))
        try:
            with conn:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS %s USING vec0("
                    "conclusion_id INTEGER PRIMARY KEY, embedding FLOAT[%d])"
                    % (table, target_dim))
            rows = conn.execute(
                "SELECT id, content FROM conclusions WHERE id > ? ORDER BY id LIMIT ?",
                (cursor, size)).fetchall()
        except Exception as exc:
            logger.warning("minir vector: migration read failed: %s", exc)
            return {"status": "failed", "done": 0}
        written = 0
        for row in rows:
            conclusion_id, content = int(row[0]), str(row[1] or "")
            vector = self.embed(content, dim=target_dim)
            if vector is None:
                break  # retry the same row next tick; the old vectors are still in place
            try:
                import sqlite_vec
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO %s (conclusion_id, embedding) VALUES (?, ?)"
                        % table, (conclusion_id, sqlite_vec.serialize_float32(vector)))
            except Exception as exc:
                logger.warning("minir vector: migration write failed: %s", exc)
                return {"status": "failed", "done": written}
            cursor = conclusion_id
            written += 1
        with conn:
            self._set_meta(conn, "migration_cursor", cursor)
        if written < len(rows):
            return {"status": "running", "done": written}  # a row failed to embed; retry it
        if len(rows) < size:
            self._finish_migration(conn, target_dim, table)
            return {"status": "done", "done": written}
        return {"status": "running", "done": written}

    def _finish_migration(self, conn: Any, dim: int, table: str) -> None:
        old = self._table()
        with conn:
            self._set_meta(conn, "dim", dim)
            self._set_meta(conn, "model", str(self._meta("pending_model", conn)
                                              or self.settings.get("model")))
            self._set_meta(conn, "provider", str(self._meta("pending_provider", conn)
                                                 or self.settings.get("provider")))
            self._set_meta(conn, "table", table)
            for key in ("pending_dim", "pending_model", "pending_provider", "pending_table",
                        "migration_cursor"):
                conn.execute("DELETE FROM vector_meta WHERE key = ?", (key,))
            conn.execute("DELETE FROM vector_meta WHERE key = 'backfill_done'")
            if old and old != table:
                conn.execute("DROP TABLE IF EXISTS %s" % old)
        self._ready = False
        logger.warning("minir vector: migration finished; active table %s", table)

    # -- write-through queue --------------------------------------------------

    def index_async(self, conclusion_id: int, content: str) -> None:
        """Queue an index write; the writer never waits for the embedding provider."""
        text = " ".join(str(content or "").split())
        if not text or not bool(self.settings.get("enabled", True)):
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._drain, name="minir-vector",
                                            daemon=True)
            self._thread.start()
        try:
            self._queue.put_nowait((int(conclusion_id), text))
        except queue.Full:
            logger.warning("minir vector: queue full, dropping %s", conclusion_id)

    def _drain(self) -> None:
        while not self._stopping:
            try:
                conclusion_id, text = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.index(conclusion_id, text)
            except Exception:
                logger.warning("minir vector: background index failed for %s",
                               conclusion_id, exc_info=True)
            finally:
                self._queue.task_done()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping = True
        if self._thread is not None:
            self._thread.join(timeout)

    def stats(self) -> dict[str, Any]:
        enabled = bool(self.settings.get("enabled"))
        if not enabled:
            return {"enabled": False, "ready": False, "mode": "bm25", "status": "off"}
        if not self.available():
            return {"enabled": True, "ready": False, "mode": "bm25",
                    "status": "unavailable"}
        try:
            count = int(self._conn().execute(
                "SELECT COUNT(*) FROM %s" % self._table()).fetchone()[0])
        except Exception:
            count = 0
        has_key = bool(self._key())
        return {"enabled": True, "ready": has_key, "mode": "rrf" if has_key else "bm25",
                "status": "ready" if has_key else "no_key", "dim": self.dim,
                "model": self.settings.get("model"), "indexed": count}
