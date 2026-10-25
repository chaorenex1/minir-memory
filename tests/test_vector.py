"""Optional vector recall: index, rank and degrade without a key."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


def _vector(plugin_package, store):
    pytest.importorskip("sqlite_vec")
    return importlib.import_module(f"{plugin_package.__name__}.vector").VectorIndex(
        store, {"dim": 4, "api_key": "test"})


def test_vector_index_and_recall_use_the_same_row(plugin_package, store):
    index = _vector(plugin_package, store)
    if not index.available():
        pytest.skip("sqlite-vec is not loadable here")
    cid = store.save_conclusion("note", "docker restart policy", key="v1")
    index.embed = lambda text: [1.0, 0.0, 0.0, 0.0]
    assert index.index(cid, "docker restart policy") is True
    assert index.recall("restart") == {cid: 0}
    assert index.stats()["indexed"] == 1


def test_vector_degrades_without_a_key(plugin_package, store):
    index = _vector(plugin_package, store)
    index.settings["api_key"] = ""
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
