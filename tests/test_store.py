"""Store behavior — SQLite persistence, FTS recall, scoping, bounds, and migration.

No transcripts: the store holds notes, job bookkeeping, derived layer artifacts, the indexed
conclusions built from them, and aggregated tool metrics.
"""

from __future__ import annotations

import sqlite3
import time

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


def test_schema_holds_no_transcript(store):
    tables = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "turns" not in tables
    assert {"notes", "jobs", "layer_state", "conclusions", "metrics"} <= tables
    assert "conclusions_fts" in tables


def test_remember_and_search_roundtrip(store):
    note_id = store.remember("User deploys Hermes on Windows with Docker Desktop", source="tool")
    results = store.search("Docker Hermes")
    assert results, "content with both terms must be found"
    top = results[0]
    assert top["kind"] == "note" and top["id"] == note_id and top["score"] >= 2


def test_search_ranks_more_matches_first(store):
    store.remember("kubernetes cluster upgrade")
    store.remember("kubernetes notes")
    results = store.search("kubernetes cluster")
    assert results[0]["score"] >= results[-1]["score"]
    assert "cluster" in results[0]["text"]


def test_search_rejects_noise_queries(store):
    store.remember("a b c")
    assert store.search("") == []
    assert store.search("a b") == []  # tokens shorter than 2 chars are ignored


def test_search_limit_is_bounded(store):
    for index in range(25):
        store.remember(f"alpha beta gamma item {index}")
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
    store.remember("a note")
    store.record_job("base", "s1", 0.0, 1.0)
    store.record_job("dialectic", "s2", 0.0, 1.0)
    stats = store.stats()
    assert (stats["notes"], stats["sessions"], stats["jobs"], stats["pending_jobs"]) == (1, 2, 2, 2)
    assert stats["db_bytes"] > 0 and stats["db_path"].endswith("memory.db")


def test_stored_content_is_truncated(store):
    store.remember("x" * 10_000)
    assert len(store.search("x" * 10)[0]["text"]) <= 4000


def test_layer_state_versions_increment_and_keep_the_latest_content(store):
    assert store.put_layer_state("base", "user:ada", "v1") == 1
    assert store.put_layer_state("base", "user:ada", "v2") == 2
    state = store.layer_state("base", "user:ada")
    assert (state["version"], state["content"]) == (2, "v2")
    assert store.layer_state("base", "user:nobody") is None
    assert [row["subject"] for row in store.all_layer_states()] == ["user:ada"]


def test_newest_layer_state_picks_the_freshest_subject(store):
    store.put_layer_state("base", "user:ada", "a")
    time.sleep(0.002)
    store.put_layer_state("base", "user:bo", "b")
    assert store.newest_layer_state("base")["subject"] == "user:bo"
    assert store.newest_layer_state("base", subject="user:ada")["content"] == "a"
    assert store.newest_layer_state("dialectic") is None


# -- job bookkeeping ----------------------------------------------------------

def test_claim_marks_running_and_counts_attempts(store):
    job_id = store.record_job("base", "s1", 0.0, 10.0, turns=3)
    claimed = store.claim_jobs(limit=1)
    assert [(job["id"], job["status"], job["attempts"]) for job in claimed] == [(job_id, "running", 1)]
    assert store.pending_jobs() == []


def test_complete_advances_the_content_watermark(store):
    job_id = store.record_job("base", "s1", 0.0, 10.0)
    store.claim_jobs(limit=1)
    store.complete_job(job_id, processed_ts=9.5)
    assert store.last_processed_ts("base", "s1") == 9.5
    assert store.last_processed_ts("base", "other") == 0.0
    assert store.last_processed_ts("dialectic", "s1") == 0.0


def test_failure_requeues_then_gives_up_without_moving_the_watermark(store):
    job_id = store.record_job("base", "s1", 0.0, 10.0)
    store.claim_jobs(limit=1)
    assert store.fail_job(job_id, "boom", max_attempts=2) == "pending"
    assert [job["id"] for job in store.pending_jobs()] == [job_id]
    store.claim_jobs(limit=1)
    assert store.fail_job(job_id, "boom again", max_attempts=2) == "failed"
    assert store.pending_jobs() == []
    assert store.stats()["failed_jobs"] == 1
    assert store.last_processed_ts("base", "s1") == 0.0, "a failed window stays owed"


def test_newer_and_open_job_queries(store):
    first = store.record_job("base", "s1", 0.0, 10.0)
    assert store.has_open_job("base", "s1") is True
    assert store.has_newer_job("base", "s1", first) is False
    second = store.record_job("base", "s1", 10.0, 20.0)
    assert store.has_newer_job("base", "s1", first) is True
    assert store.has_newer_job("base", "other", first) is False
    store.claim_jobs(limit=2)
    store.complete_job(first, 10.0)
    store.complete_job(second, 20.0)
    assert store.has_open_job("base", "s1") is False


def test_expire_closes_only_jobs_from_before_the_process(store):
    old = store.record_job("base", "s1", 0.0, 10.0)
    time.sleep(0.002)
    cutoff = time.time()
    store.record_job("base", "s1", 10.0, 20.0)
    claimed = store.record_job("dialectic", "s1", 0.0, 10.0)
    store.claim_jobs(limit=5)  # everything running
    # Only the job queued before the cutoff: the two later ones belong to this process.
    assert store.expire_open_jobs("gone", before=cutoff) == 1
    rows = {row["id"]: row["status"] for row in store._conn.execute("SELECT id, status FROM jobs")}
    assert rows[old] == "expired"
    assert rows[claimed] == "running" and len(rows) == 3


def test_migration_drops_a_legacy_transcript_table(plugin_package, tmp_path):
    """A database written by the version that captured every turn is migrated on open."""
    path = tmp_path / "legacy" / "memory.db"
    path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(str(path))
    legacy.executescript(
        "CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
        " author TEXT, created_at REAL);"
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, layer TEXT, session_id TEXT, from_ts REAL,"
        " to_ts REAL, turns INTEGER, status TEXT, created_at REAL);"
        "INSERT INTO turns (session_id, role, content, created_at) VALUES ('s', 'user', 'secret', 1.0);"
        "INSERT INTO jobs (layer, session_id, from_ts, to_ts, turns, status, created_at)"
        " VALUES ('base', 's', 0.0, 1.0, 1, 'pending', 1.0);")
    legacy.commit()
    legacy.close()

    store = plugin_package.store.MinirStore(path)
    try:
        tables = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "turns" not in tables
        # The jobs table kept its rows and gained the worker's columns.
        assert store.pending_jobs()[0]["layer"] == "base"
        assert store.last_processed_ts("base", "s") == 0.0
    finally:
        store.close()


def test_close_is_idempotent(store):
    store.close()
    store.close()


def test_conclusions_are_indexed_and_session_scoped(store):
    """Durable conclusions recall across sessions; a review/summary never leaks."""
    base = ("## Session Summary\n- deploy the Windows build\n\n"
            "## User Representation\n- [goal] finish the plugin\n\n"
            "## User Peer Card\n- prefers terse answers\n\n"
            "## AI Self Representation\n- direct\n\n"
            "## AI Identity Card\n- never edits outside the repo")
    store.put_layer_state("base", "user:ada", base, session_id="s1", ai_subject="agent:minir")
    dialectic = ("## Adviser Review\n- skipped the smoke test\n\n"
                 "## Constraints\n- never delete user files\n\n"
                 "## Preferences\n- act first\n\n"
                 "## Second Brain\n- [tool] pytest -q")
    store.put_layer_state("dialectic", "agent:minir", dialectic, session_id="s1")

    goal = store.search("finish the plugin", session_id="s1")
    assert goal and goal[0]["kind"] == "base.user_representation"
    assert store.search("finish the plugin", session_id="s2") == [], "session focus must not leak"

    review = store.search("skipped smoke", session_id="s1")
    assert review and review[0]["kind"] == "dialectic.review"
    assert not any(row["kind"] == "dialectic.review"
                   for row in store.search("skipped smoke", session_id="s2"))

    peer = store.search("terse answers", session_id="s2")
    assert peer and peer[0]["kind"] == "base.user_peer_card", "durable conclusions are cross-session"

    assert store.search("plugin", session_id="") == [], "no session means no session-bound kinds"


def test_remember_and_forget_update_the_index(store):
    note_id = store.remember("fts indexed conclusion")
    assert store.search("indexed conclusion")
    assert store.forget(note_id) is True
    assert store.search("indexed conclusion") == []
