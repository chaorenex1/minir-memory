"""Optional vector recall over the conclusions table (sqlite-vec + an OpenAI-compatible API).

The module is deliberately fail-open: a missing package, key, endpoint or a bad dimension makes
the caller fall back to bm25 for that turn. It never raises into a search and never writes a
half vector row.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

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
        self._cache: dict[str, list[float]] = {}

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

    def ensure_table(self) -> bool:
        if self._ready:
            return True
        if not self.available():
            return False
        conn = self._conn()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'conclusions_vec'").fetchone()
                if existing is not None and f"FLOAT[{self.dim}]" not in str(existing[0] or ""):
                    return self._recreate_locked(conn)
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS conclusions_vec USING vec0("
                    "conclusion_id INTEGER PRIMARY KEY, embedding FLOAT[%d])" % self.dim)
                conn.execute("CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, "
                             "value TEXT NOT NULL)")
                row = conn.execute(
                    "SELECT value FROM vector_meta WHERE key = 'dim'").fetchone()
                if row is not None and int(row[0]) != self.dim:
                    return self._recreate_locked(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO vector_meta (key, value) VALUES ('dim', ?)",
                    (str(self.dim),))
            self._ready = True
            return True
        except Exception as exc:
            logger.warning("minir vector: table unavailable: %s", exc)
            return False

    def _recreate_locked(self, conn: Any) -> bool:
        conn.execute("DROP TABLE IF EXISTS conclusions_vec")
        conn.execute(
            "CREATE VIRTUAL TABLE conclusions_vec USING vec0("
            "conclusion_id INTEGER PRIMARY KEY, embedding FLOAT[%d])" % self.dim)
        conn.execute("INSERT OR REPLACE INTO vector_meta (key, value) VALUES ('dim', ?)",
                     (str(self.dim),))
        conn.execute("DELETE FROM vector_meta WHERE key = 'backfill_done'")
        self._ready = True
        return True

    # -- embedding ------------------------------------------------------------

    def embed(self, text: str) -> Optional[list[float]]:
        flat = " ".join(str(text or "").split())
        if not flat:
            return None
        cached = self._cache.get(flat)
        if cached is not None:
            return cached
        key = self._key()
        if not key:
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
        if not isinstance(vector, list) or len(vector) != self.dim:
            logger.warning("minir vector: embedding dimension %s != %s; refused",
                           len(vector) if isinstance(vector, list) else "?", self.dim)
            return None
        values = [float(item) for item in vector]
        if len(self._cache) >= 128:
            self._cache.pop(next(iter(self._cache)))
        self._cache[flat] = values
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
                "SELECT conclusion_id FROM conclusions_vec WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?",
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
                    "INSERT OR REPLACE INTO conclusions_vec (conclusion_id, embedding) "
                    "VALUES (?, ?)",
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
                "SELECT conclusion_id FROM conclusions_vec").fetchall()}
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

    def stats(self) -> dict[str, Any]:
        if not self.available():
            return {"enabled": bool(self.settings.get("enabled")), "ready": False}
        try:
            count = int(self._conn().execute(
                "SELECT COUNT(*) FROM conclusions_vec").fetchone()[0])
        except Exception:
            count = 0
        return {"enabled": True, "ready": bool(self._key()), "dim": self.dim,
                "model": self.settings.get("model"), "indexed": count}
