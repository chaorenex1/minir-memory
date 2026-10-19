"""Store behavior — SQLite persistence, FTS recall, scoping, bounds, and migration.

No transcripts: the store holds notes, job bookkeeping, derived layer artifacts, the indexed
conclusions built from them, and aggregated tool metrics.
"""

from __future__ import annotations

import json
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
    assert {"notes", "jobs", "layer_state", "base_refreshes", "conclusions", "metrics"} <= tables
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


def test_session_scope_hides_other_sessions_until_scope_all(store):
    mine = store.remember("alpha marker", marker_type="tools", session_id="s1")
    other = store.remember("alpha marker", marker_type="tools", session_id="s2")
    durable = store.remember("alpha marker durable")

    rows = store.search("alpha marker", session_id="s1")
    ids = {row["note_id"] for row in rows}
    assert mine in ids and durable in ids and other not in ids
    assert next(row for row in rows if row["note_id"] == mine)["marker_type"] == "tools"

    rows = store.search("alpha marker", session_id="s1", scope="all")
    ids = {row["note_id"] for row in rows}
    assert {mine, other, durable} <= ids
    assert next(row for row in rows if row["note_id"] == other)["session_id"] == "s2"


def test_session_scope_keeps_other_reviews_out_until_scope_all(store):
    store.put_layer_state("dialectic", "agent:minir",
                          "## Base Revision\n- zebraword revision", session_id="s2")
    assert store.search("zebraword", session_id="s3") == []
    assert any(row["kind"] == "dialectic.review"
               for row in store.search("zebraword", session_id="s3", scope="all"))


def test_search_kind_filter_applies_before_the_limit(store):
    note_id = store.remember("zebraword note", marker_type="tools", session_id="s1")
    for index in range(25):
        store.put_conclusion("base.user_representation", f"goal:{index}",
                             f"zebraword derived {index}", session_id="s1")
    rows = store.search("zebraword", limit=1, session_id="s1", kinds=frozenset({"note"}))
    assert [row["note_id"] for row in rows] == [note_id]


def test_forget_marker_requires_the_stored_type(store):
    note_id = store.remember("typed marker", marker_type="tools", session_id="s1")
    assert store.forget(note_id, marker_type="projects") is False
    assert store.search("typed marker", session_id="s1")
    assert store.forget(note_id, marker_type="tools") is True
    assert store.search("typed marker", session_id="s1") == []


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
    assert store.open_job_end("base", "s1") == 20.0
    assert store.open_job_end("base", "other") is None
    store.claim_jobs(limit=2)
    store.complete_job(first, 10.0)
    store.complete_job(second, 20.0)
    assert store.has_open_job("base", "s1") is False
    assert store.open_job_end("base", "s1") is None


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


def test_reclaim_requeues_running_jobs_left_by_a_previous_process(store):
    job = store.record_job("base", "s1", 0.0, 10.0)
    store.claim_jobs(limit=1)
    assert store.reclaim_open_jobs(before=time.time() - 1.0, max_attempts=2) == 0, \
        "a job claimed by this process must be left running"
    cutoff = time.time() + 0.001  # a later process starts
    assert store.reclaim_open_jobs(before=cutoff, max_attempts=2) == 1
    row = store._conn.execute("SELECT status, attempts, claimed_at, error FROM jobs WHERE id = ?",
                              (job,)).fetchone()
    assert (row["status"], row["attempts"], row["claimed_at"], row["error"]) == ("pending", 1, None, "")
    store.claim_jobs(limit=1)  # second claim: attempts now 2
    assert store.reclaim_open_jobs(before=time.time() + 0.001, max_attempts=2) == 1
    row = store._conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job,)).fetchone()
    assert row["status"] == "failed" and "process" in row["error"]


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


def test_the_worker_job_path_does_not_raise_after_close(store):
    """The host can close the store while a drain is still unwinding (the log showed the
    drain thread dying on ``fail_job``). Job bookkeeping must treat a closed store as gone."""
    store.record_job("base", "s1", 1.0, 2.0, turns=1)
    assert [job["id"] for job in store.claim_jobs()] == [1]
    store.close()
    assert store.claim_jobs() == []
    assert store.pending_job_count("s1") == 0
    assert store.open_job_count("s1") == 0
    assert store.fail_job(1, "shutdown") == "closed"
    store.complete_job(1, 2.0)  # no raise either


def test_conclusions_are_indexed_and_session_scoped(store):
    """Durable conclusions recall across sessions; a review/summary never leaks."""
    base = ("## Session Summary\n- deploy the Windows build\n\n"
            "## User Representation\n- [goal] finish the plugin\n\n"
            "## User Peer Card\n- prefers terse answers\n\n"
            "## AI Self Representation\n- direct\n\n"
            "## AI Identity Card\n- never edits outside the repo\n\n"
            "## Value Markers\n- [tool] pytest")
    store.put_layer_state("base", "user:ada", base, session_id="s1", ai_subject="agent:minir")
    dialectic = "## Base Revision\n- user_peer_card: skipped the smoke test"
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


def test_compression_gate_pauses_until_cleared_or_expired(store):
    now = time.time()
    store.set_compression_gate("s1", expires_at=now + 60, run_id="r1")
    assert store.compression_gate_active("s1", now=now) is True
    assert store.compression_gate_active("s1", now=now + 61) is False
    store.clear_compression_gate("s1")
    assert store.compression_gate_active("s1") is False


def test_promote_summary_run_only_touches_the_matching_marker(store):
    store.put_conclusion("session_summary", "session_summary:s1:r1", "one",
                         session_id="s1", subject="session", status="pending")
    store.put_conclusion("session_summary", "session_summary:s1:r2", "two",
                         session_id="s1", subject="session", status="pending")
    assert store.promote_summary_run("s1", "r1") == 1
    assert store.get_conclusion("session_summary", "session_summary:s1:r1")["status"] == "active"
    assert store.get_conclusion("session_summary", "session_summary:s1:r2")["status"] == "pending"


def test_remember_and_forget_update_the_index(store):
    note_id = store.remember("fts indexed conclusion")
    assert store.search("indexed conclusion")
    assert store.forget(note_id) is True
    assert store.search("indexed conclusion") == []


# -- base refresh history -----------------------------------------------------

def _refresh(store, session="s1", subject="user:ada", *, from_ts, to_ts,
             from_turn=None, to_turn=None, summary="window", peer="terse",
             markers="- [tool] pytest"):
    return store.add_base_refresh(
        session, subject, window_from_ts=from_ts, window_to_ts=to_ts,
        window_from_turn=from_turn, window_to_turn=to_turn,
        user_representation=f"[goal] need at {from_ts}",
        user_peer_card=peer, ai_self_representation=f"[done] window {from_ts}",
        ai_identity_card="direct", session_chunk_summary=summary,
        value_markers=markers)


def test_base_refreshes_keep_history_and_exactly_one_latest(store):
    first = _refresh(store, from_ts=0.0, to_ts=10.0, from_turn=1, to_turn=2)
    second = _refresh(store, from_ts=10.0, to_ts=20.0, from_turn=3, to_turn=4,
                      summary="second window", peer="terse, no emoji")

    latest = store.latest_base_refresh("s1", "user:ada")
    assert latest["id"] == second and latest["is_latest"] == 1
    assert (latest["window_from_ts"], latest["window_to_ts"]) == (10.0, 20.0)
    assert (latest["window_from_turn"], latest["window_to_turn"]) == (3, 4)
    assert latest["session_chunk_summary"] == "second window"
    assert latest["user_peer_card"] == "terse, no emoji"

    history = store.base_refreshes("s1", "user:ada")
    assert [row["id"] for row in history] == [second, first]
    assert [row["is_latest"] for row in history] == [1, 0]
    assert store.latest_base_refresh("s1", "user:bo") is None
    assert store.stats()["base_refreshes"] == 2


def test_latest_refresh_follows_window_order_not_write_order(store):
    newer = _refresh(store, from_ts=10.0, to_ts=20.0, from_turn=3, to_turn=4)
    older = _refresh(store, from_ts=0.0, to_ts=10.0, from_turn=1, to_turn=2,
                     summary="late older window")
    latest = store.latest_base_refresh("s1", "user:ada")
    assert latest["id"] == newer, "a late completion of an older window must not become latest"
    history = store.base_refreshes("s1", "user:ada")
    flags = {row["id"]: row["is_latest"] for row in history}
    assert flags == {newer: 1, older: 0}


def test_the_same_refresh_window_updates_in_place(store):
    first = _refresh(store, from_ts=0.0, to_ts=10.0, summary="first attempt")
    again = _refresh(store, from_ts=0.0, to_ts=10.0, summary="retried attempt")
    assert again == first
    assert len(store.base_refreshes("s1")) == 1
    assert store.latest_base_refresh("s1")["session_chunk_summary"] == "retried attempt"


def test_a_dependent_dialectic_job_is_claimed_before_a_later_base_job(store):
    base_one = store.record_job("base", "s1", 0.0, 10.0)
    base_two = store.record_job("base", "s1", 10.0, 20.0)
    review = store.record_job("dialectic", "s1", 0.0, 10.0)
    assert store.claim_jobs(limit=1)[0]["id"] == review
    assert store.claim_jobs(limit=1)[0]["id"] == base_one
    assert store.claim_jobs(limit=1)[0]["id"] == base_two


def test_advisories_are_deduped_and_blockers_come_first(store):
    first = store.add_advisories("s1", [
        {"severity": "nit", "target": "general", "content": "polish the wording"},
        {"severity": "blocker", "target": "general", "content": "verify the path"},
    ])
    assert len(first) == 2
    assert store.add_advisories("s1", [
        {"severity": "nit", "target": "general", "content": "polish the wording"}]) == []
    pending = store.pending_advisories("s1")
    assert [row["severity"] for row in pending] == ["blocker", "nit"]
    assert store.mark_advisories_injected([row["id"] for row in pending], "turn-1") == 2
    assert store.pending_advisories("s1") == []
    assert store.has_advisories("s1") is True


def test_apply_base_revision_snapshots_then_updates_atomically(store):
    refresh_id = _refresh(store, from_ts=0.0, to_ts=10.0, peer="old peer")
    content = ("## Session Summary\n- window\n\n## User Representation\n- [goal] need at 0.0\n\n"
               "## User Peer Card\n- new peer\n\n## AI Self Representation\n- [done] window 0.0\n\n"
               "## AI Identity Card\n- direct\n\n## Value Markers\n- [tool] pytest")
    revision_id = store.apply_base_revision(
        "s1", "user:ada", window_from_ts=0.0,
        changes={"user_peer_card": json.dumps(["new peer"])},
        changed_fields=["user_peer_card"], content=content)

    assert revision_id
    latest = store.latest_base_refresh("s1", "user:ada")
    assert latest["id"] == refresh_id
    assert json.loads(latest["user_peer_card"]) == ["new peer"]
    assert latest["is_latest"] == 1
    revisions = store.base_revisions("s1", "user:ada")
    assert len(revisions) == 1
    revision = revisions[0]
    assert revision["base_refresh_id"] == refresh_id
    assert revision["user_peer_card"] == "old peer"
    assert json.loads(revision["changed_fields"]) == ["user_peer_card"]
    assert revision["advices"] == "[]"
    state = store.layer_state("base", "user:ada")
    assert state["version"] == 1 and "new peer" in state["content"]
    hits = store.search_base_revisions("old peer", session_id="s1")
    assert hits and hits[0]["id"] == revision_id


def test_an_older_window_revision_does_not_replace_the_live_artifact(store):
    older = _refresh(store, from_ts=0.0, to_ts=10.0, peer="old peer")
    newer = _refresh(store, from_ts=10.0, to_ts=20.0, peer="new peer")
    live = ("## Session Summary\n- new window\n\n## User Representation\n- [goal] new\n\n"
            "## User Peer Card\n- new peer\n\n## AI Self Representation\n- [done] new\n\n"
            "## AI Identity Card\n- direct\n\n## Value Markers\n- [tool] pytest")
    store.put_layer_state("base", "user:ada", live, session_id="s1", ai_subject="agent:minir")

    revision = store.apply_base_revision(
        "s1", "user:ada", window_from_ts=0.0,
        changes={"user_peer_card": json.dumps(["corrected old peer"])},
        changed_fields=["user_peer_card"],
        content="## User Peer Card\n- corrected old peer")

    assert revision
    assert store.layer_state("base", "user:ada")["content"] == live, \
        "a late review of an older window must not replace the live artifact"
    history = store.base_refreshes("s1", "user:ada")
    flags = {row["id"]: row["is_latest"] for row in history}
    assert flags == {newer: 1, older: 0}
    assert json.loads(history[-1]["user_peer_card"]) == ["corrected old peer"]


def test_legacy_base_refreshes_gain_value_markers_and_fts(plugin_package, tmp_path):
    path = tmp_path / "mem" / "memory.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            "CREATE TABLE base_refreshes ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',"
            " window_from_ts REAL NOT NULL, window_to_ts REAL NOT NULL,"
            " window_from_turn INTEGER, window_to_turn INTEGER,"
            " user_representation TEXT NOT NULL DEFAULT '',"
            " user_peer_card TEXT NOT NULL DEFAULT '',"
            " ai_self_representation TEXT NOT NULL DEFAULT '',"
            " ai_identity_card TEXT NOT NULL DEFAULT '',"
            " session_chunk_summary TEXT NOT NULL DEFAULT '',"
            " is_latest INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);")
        connection.execute(
            "INSERT INTO base_refreshes (session_id, subject, window_from_ts, window_to_ts,"
            " user_representation, user_peer_card, ai_self_representation, ai_identity_card,"
            " session_chunk_summary, is_latest, created_at)"
            " VALUES ('s1', 'user:ada', 0, 10, '- old goal', '- legacy tool',"
            " '- old self', '- old identity', '- old summary', 1, 1.0)")
        connection.commit()
    finally:
        connection.close()

    reopened = plugin_package.store.MinirStore(path)
    try:
        row = reopened.latest_base_refresh("s1", "user:ada")
        assert row["value_markers"] == ""
        hits = reopened.search_base_refreshes("legacy")
        assert hits and hits[0]["user_peer_card"] == "- legacy tool"
    finally:
        reopened.close()


def test_base_refresh_fts_indexes_six_fields_and_short_cjk_queries(store):
    _refresh(store, from_ts=0.0, to_ts=10.0, summary="deploy 插件",
             peer="喜欢工具", markers="- [tool] pytest")
    _refresh(store, from_ts=10.0, to_ts=20.0, summary="second 插件",
             peer="后续偏好", markers="- [project] minir")

    # Two characters: trigram cannot match, so this must take the LIKE fallback.
    hits = store.search_base_refreshes("工具")
    assert hits and hits[0]["user_peer_card"] == "喜欢工具"
    # Three+ characters can use the FTS index.
    hits = store.search_base_refreshes("pytest")
    assert hits and "- [tool] pytest" in hits[0]["value_markers"]
    # Latest-only and session filters apply to both paths.
    latest = store.search_base_refreshes("插件", latest_only=True)
    assert [row["window_to_ts"] for row in latest] == [20.0]
    assert store.search_base_refreshes("插件", session_id="other") == []


def test_reopening_normalizes_latest_to_the_newest_window(plugin_package, tmp_path):
    path = tmp_path / "mem" / "memory.db"
    first = plugin_package.store.MinirStore(path)
    try:
        _refresh(first, from_ts=10.0, to_ts=20.0, summary="newer")
        _refresh(first, from_ts=0.0, to_ts=10.0, summary="older")
        # Simulate the previous release: the last write was flagged latest.
        first._conn.execute("UPDATE base_refreshes SET is_latest = 0")
        first._conn.execute("UPDATE base_refreshes SET is_latest = 1 WHERE window_to_ts = 10.0")
        first._conn.commit()
    finally:
        first.close()

    reopened = plugin_package.store.MinirStore(path)
    try:
        latest = reopened.latest_base_refresh("s1", "user:ada")
        assert latest["window_to_ts"] == 20.0
        flags = {row["window_to_ts"]: row["is_latest"]
                 for row in reopened.base_refreshes("s1", "user:ada")}
        assert flags == {20.0: 1, 10.0: 0}
    finally:
        reopened.close()


def test_latest_refresh_is_scoped_to_the_session(store):
    _refresh(store, session="s1", from_ts=0.0, to_ts=10.0)
    _refresh(store, session="s2", from_ts=0.0, to_ts=10.0, peer="other session")
    assert store.latest_base_refresh("s1")["user_peer_card"] == "terse"
    assert store.latest_base_refresh("s2")["user_peer_card"] == "other session"
    assert store.base_refreshes("s2")[0]["user_peer_card"] == "other session"
