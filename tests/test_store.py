"""Store behavior — SQLite persistence, ranking, and bounds."""

from __future__ import annotations

import pytest


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


def test_creates_parent_directories(plugin_package, tmp_path):
    db_path = tmp_path / "nested" / "dir" / "memory.db"
    instance = plugin_package.store.MinirStore(db_path)
    instance.close()
    assert db_path.exists()


def test_add_turn_and_search_roundtrip(store):
    store.add_turn("s1", "user", "I deploy Hermes on Windows with Docker Desktop")
    store.add_turn("s1", "assistant", "Noted.")
    results = store.search("Docker Hermes")
    assert results, "content with both terms must be found"
    top = results[0]
    assert top["kind"] == "turn" and top["role"] == "user"
    assert top["session_id"] == "s1"
    assert top["score"] == 2  # both query tokens matched


def test_search_ranks_more_matches_first(store):
    store.add_turn("s1", "user", "kubernetes cluster upgrade")
    store.add_turn("s1", "user", "kubernetes notes")
    results = store.search("kubernetes cluster")
    assert results[0]["score"] >= results[-1]["score"]
    assert "cluster" in results[0]["text"]


def test_search_covers_notes_too(store):
    note_id = store.remember("User prefers terse Chinese answers", source="memory_tool/user")
    results = store.search("terse Chinese")
    assert any(row["kind"] == "note" and row["id"] == note_id for row in results)


def test_search_rejects_noise_queries(store):
    store.add_turn("s1", "user", "a b c")
    assert store.search("") == []
    assert store.search("a b") == []  # tokens shorter than 2 chars are ignored


def test_search_limit_is_bounded(store):
    for index in range(25):
        store.add_turn("s1", "user", f"alpha beta gamma item {index}")
    assert len(store.search("alpha beta gamma", limit=100)) <= 20


def test_remember_and_forget(store):
    note_id = store.remember("rotate credentials quarterly")
    assert store.forget(note_id) is True
    assert store.forget(note_id) is False
    assert store.search("rotate credentials") == []


def test_forget_by_content(store):
    store.remember("mirrored entry")
    store.remember("unrelated")
    assert store.forget_by_content("mirrored entry") == 1
    assert store.stats()["notes"] == 1


def test_stats_counts(store):
    store.add_turn("s1", "user", "hello world")
    store.add_turn("s2", "user", "other session")
    store.remember("a note")
    stats = store.stats()
    assert (stats["turns"], stats["notes"], stats["sessions"]) == (2, 1, 2)
    assert stats["db_bytes"] > 0 and stats["db_path"].endswith("memory.db")


def test_stored_content_is_truncated(store):
    store.add_turn("s1", "user", "x" * 10_000)
    assert len(store.search("x" * 10)[0]["text"]) <= 4000


def test_close_is_idempotent(store):
    store.close()
    store.close()
