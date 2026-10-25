"""Optional vector recall: index, rank and degrade without a key."""

from __future__ import annotations

import importlib
import json
import time

import pytest


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


def _vector(plugin_package, store):
    pytest.importorskip("sqlite_vec")
    return importlib.import_module(f"{plugin_package.__name__}.vector").VectorIndex(
        store, {"dim": 4, "key_env": "MINIR_TEST_VECTOR_KEY"})


def test_vector_index_and_recall_use_the_same_row(plugin_package, store):
    index = _vector(plugin_package, store)
    if not index.available():
        pytest.skip("sqlite-vec is not loadable here")
    cid = store.save_conclusion("note", "docker restart policy", key="v1")
    index.embed = lambda text: [1.0, 0.0, 0.0, 0.0]
    assert index.index(cid, "docker restart policy") is True
    assert index.recall("restart") == {cid: 0}
    assert index.stats()["indexed"] == 1


def test_a_dimension_change_queues_a_resumable_migration(plugin_package, store, monkeypatch):
    vector_mod = importlib.import_module(f"{plugin_package.__name__}.vector")
    sqlite_vec = pytest.importorskip("sqlite_vec")
    index = vector_mod.VectorIndex(store, {"dim": 4, "model": "new-model"})
    assert index.available() is True  # vec0 is loaded on this connection
    conn = store._conn
    conn.execute("CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, "
                 "value TEXT NOT NULL)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS conclusions_vec USING vec0("
                 "conclusion_id INTEGER PRIMARY KEY, embedding FLOAT[2])")
    conn.execute("INSERT INTO vector_meta VALUES ('dim', '2')")
    conn.execute("INSERT INTO vector_meta VALUES ('table', 'conclusions_vec')")
    conn.execute("INSERT OR REPLACE INTO conclusions_vec (conclusion_id, embedding) "
                 "VALUES (1, ?)", (sqlite_vec.serialize_float32([0.1, 0.2]),))
    conn.commit()
    store.save_conclusion("note", "docker prune removes images", key="d", session_id="s1")

    assert index.ensure_table() is False  # queued, never dropped
    assert conn.execute("SELECT COUNT(*) FROM conclusions_vec").fetchone()[0] == 1
    assert index._meta("pending_dim") == "4"

    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert index.migrate()["status"] == "running"  # no key: the cursor stays put
    assert index._meta("migration_cursor") == "0"

    index.embed = lambda text, dim=None: [0.5] * int(dim or index.dim)
    assert index.migrate(batch=1)["status"] == "running"
    assert index.migrate(batch=1)["status"] == "done"

    assert index._meta("pending_dim") is None
    assert (index._meta("dim"), index._meta("model")) == ("4", "new-model")
    table = index._meta("table")
    assert table != "conclusions_vec"
    assert conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'conclusions_vec'"
                        ).fetchone()[0] == 0
    assert index.ensure_table() is True


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_http_failures_degrade_to_bm25_without_raising(plugin_package, store, monkeypatch):
    vector_mod = importlib.import_module(f"{plugin_package.__name__}.vector")
    monkeypatch.setenv("MINIR_TEST_VECTOR_KEY", "k")
    index = vector_mod.VectorIndex(store, {"key_env": "MINIR_TEST_VECTOR_KEY",
                                           "dim": 4, "enabled": True})
    assert index.ensure_table() is True
    monkeypatch.setattr(vector_mod.urllib.request, "urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            vector_mod.urllib.error.HTTPError("u", 401, "no", {}, None)))

    assert index.embed("docker") is None
    assert index.recall("docker") == {}


def test_base_url_and_key_env_are_configurable(plugin_package, store, monkeypatch):
    vector_mod = importlib.import_module(f"{plugin_package.__name__}.vector")
    seen = {}

    def capture(request, **kwargs):
        seen["url"] = request.full_url
        seen["auth"] = request.headers.get("Authorization")
        return _Response({"data": [{"embedding": [0.1] * 4}]})

    monkeypatch.setattr(vector_mod.urllib.request, "urlopen", capture)
    monkeypatch.setenv("MINIR_TEST_OTHER_KEY", "k2")
    index = vector_mod.VectorIndex(store, {"key_env": "MINIR_TEST_OTHER_KEY", "dim": 4,
                                           "base_url": "https://example.test/v1"})

    assert len(index.embed("docker") or []) == 4
    assert seen == {"url": "https://example.test/v1/embeddings", "auth": "Bearer k2"}


def test_the_key_comes_only_from_the_env_var_named_by_key_env(plugin_package, store,
                                                              monkeypatch):
    vector_mod = importlib.import_module(f"{plugin_package.__name__}.vector")
    monkeypatch.setenv("MINIR_TEST_NAMED_KEY", "from-env")
    named = vector_mod.VectorIndex(store, {"key_env": "MINIR_TEST_NAMED_KEY"})
    assert named._key() == "from-env"

    monkeypatch.delenv("MINIR_TEST_NAMED_KEY")
    assert named._key() == ""

    monkeypatch.setenv("SILICONFLOW_API_KEY", "fallback")
    assert vector_mod.VectorIndex(store)._key() == "fallback"
    # an empty key_env is dropped by the settings merge, so the default name applies
    assert vector_mod.VectorIndex(store, {"key_env": ""})._key() == "fallback"


def test_the_pinned_defaults_are_siliconflow_bge_m3_1024(plugin_package, store):
    index = importlib.import_module(f"{plugin_package.__name__}.vector").VectorIndex(store)

    assert (index.settings["provider"], index.settings["model"], index.dim) == (
        "siliconflow", "BAAI/bge-m3", 1024)
    assert index.settings["key_env"] == "SILICONFLOW_API_KEY"
    assert index.settings["base_url"] == "https://api.siliconflow.cn/v1"


def test_a_wrong_dimension_is_refused_without_touching_the_table(plugin_package, store,
                                                                 monkeypatch):
    vector_mod = importlib.import_module(f"{plugin_package.__name__}.vector")
    monkeypatch.setenv("MINIR_TEST_VECTOR_KEY", "k")
    index = vector_mod.VectorIndex(store, {"key_env": "MINIR_TEST_VECTOR_KEY", "dim": 4,
                                           "enabled": True})
    assert index.ensure_table() is True
    monkeypatch.setattr(vector_mod.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(
                            {"data": [{"embedding": [0.1] * 4}]}))
    assert len(index.embed("a four dim reply", dim=4)) == 4  # the harness itself works

    monkeypatch.setattr(vector_mod.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(
                            {"data": [{"embedding": [0.1] * 768}]}))
    assert index.index(1, "docker prune") is False

    assert store._conn.execute(
        "SELECT COUNT(*) FROM %s" % index._table()).fetchone()[0] == 0


def test_a_missing_key_warns_once_and_reports_bm25(plugin_package, store, monkeypatch, caplog):
    import logging

    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    index = importlib.import_module(f"{plugin_package.__name__}.vector").VectorIndex(
        store, {"key_env": "", "enabled": True})

    with caplog.at_level(logging.WARNING):
        assert index.embed("docker") is None
        assert index.embed("docker") is None

    assert sum("no API key" in record.message for record in caplog.records) == 1
    assert index.stats()["mode"] == "bm25"


def test_index_async_hands_the_write_to_the_background_thread(plugin_package, store):
    index = importlib.import_module(f"{plugin_package.__name__}.vector").VectorIndex(store)
    seen: list[tuple[int, str]] = []
    index.index = lambda cid, text: seen.append((cid, text)) or True

    index.index_async(7, "docker   prune")
    deadline = time.time() + 2.0
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    index.stop()

    assert seen == [(7, "docker prune")]


def test_vector_degrades_without_a_key(plugin_package, store):
    index = _vector(plugin_package, store)
    index.settings["key_env"] = ""
    index.embed = lambda text: None
    store.save_conclusion("note", "docker restart policy", key="v1")
    assert index.recall("restart") == {}
    assert index.available() is True, "the table still exists; only the query degrades"


def test_backfill_indexes_rows_missing_from_the_table(plugin_package, store):
    index = _vector(plugin_package, store)
    if not index.available():
        pytest.skip("sqlite-vec is not loadable here")
    first = store.save_conclusion("note", "one", key="b1")
    second = store.save_conclusion("note", "two", key="b2")
    index.embed = lambda text: [0.0, 1.0, 0.0, 0.0]
    assert index.backfill() == 2
    assert index.stats()["indexed"] >= 2
    assert {first, second} <= set(index.recall("anything").keys())
