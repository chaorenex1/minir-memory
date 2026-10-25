"""Store behavior — SQLite persistence, FTS recall, scoping, bounds, and migration.

No transcripts: ``conclusions`` is the only memory table. The store holds its rows, job
bookkeeping, the base refresh windows, and aggregated tool metrics.
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
    assert "notes" not in tables, "conclusions is the only memory table"
    assert {"jobs", "layer_state", "base_refreshes", "conclusions", "metrics"} <= tables
    assert "conclusions_fts" in tables
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(conclusions)")}
    assert columns == {"id", "kind", "key", "session_id", "subject", "content", "evidence",
                       "version", "status", "created_at", "updated_at"}


def test_conclusions_fts_still_queries_after_the_shrink(store):
    store.save_conclusion("entities", "User deploys Hermes on Windows with Docker Desktop")
    assert store.search("Docker Hermes"), "the FTS index still reaches a stored row"


def test_save_conclusion_returns_the_row_and_search_hides_note_fields(store):
    row_id = store.save_conclusion(
        "entities", "User deploys Hermes on Windows with Docker Desktop", subject="tool")
    assert row_id > 0
    results = store.search("Docker Hermes")
    assert results, "content with both terms must be found"
    top = results[0]
    assert top["kind"] == "entities" and top["conclusions_id"] == row_id and top["score"] > 0
    assert set(top) == {"conclusions_id", "kind", "key", "text", "when", "author", "subject",
                        "session_id", "score", "rank"}


def test_save_conclusion_keeps_content_verbatim(store):
    payload = '{"value_markers": ["工具", "pytest"]}'
    plain = "plain text with spaces\nand a second line"
    store.save_conclusion("tools", payload, key="marker:1")
    store.save_conclusion("note", plain, key="next:1")
    assert store.get_conclusion("tools", "marker:1")["content"] == payload
    assert store.search("pytest")[0]["text"] == payload
    assert store.get_conclusion("note", "next:1")["content"] == plain


def test_search_ranks_more_matches_first(store):
    store.save_conclusion("note", "kubernetes cluster upgrade")
    store.save_conclusion("note", "kubernetes notes")
    results = store.search("kubernetes cluster")
    assert results[0]["score"] >= results[-1]["score"]
    assert "cluster" in results[0]["text"]


def test_search_rejects_noise_queries(store):
    store.save_conclusion("note", "a b c")
    assert store.search("") == []
    assert store.search("a b") == []  # tokens shorter than 2 chars are ignored


def test_search_limit_is_bounded(store):
    for index in range(25):
        store.save_conclusion("note", f"alpha beta gamma item {index}")
    assert len(store.search("alpha beta gamma", limit=100)) <= 20


def test_forget_conclusion_requires_the_stored_kind(store):
    row_id = store.save_conclusion("note", "rotate credentials quarterly", session_id="s1")
    assert store.forget_conclusion("tools", row_id) is False, "a wrong kind deletes nothing"
    assert store.search("rotate credentials", session_id="s1"), "the row is still there"
    assert store.forget_conclusion("note", row_id) is True
    assert store.search("rotate credentials", session_id="s1") == []


def test_session_scope_hides_other_sessions_until_scope_all(store):
    mine = store.save_conclusion("tools", "alpha marker", session_id="s1")
    other = store.save_conclusion("tools", "alpha marker", session_id="s2")
    durable = store.save_conclusion("note", "alpha marker durable")

    rows = store.search("alpha marker", session_id="s1")
    ids = {row["conclusions_id"] for row in rows}
    assert mine in ids and durable in ids and other not in ids
    assert next(row for row in rows if row["conclusions_id"] == mine)["kind"] == "tools"

    rows = store.search("alpha marker", session_id="s1", scope="all")
    ids = {row["conclusions_id"] for row in rows}
    assert {mine, other, durable} <= ids
    assert next(row for row in rows if row["conclusions_id"] == other)["session_id"] == "s2"


def test_session_scope_keeps_other_reviews_out_until_scope_all(store):
    store.put_layer_state("dialectic", "agent:default:minir",
                          "## Base Revision\n- zebraword revision", session_id="s2")
    assert store.search("zebraword", session_id="s3") == []
    assert any(row["kind"] == "dialectic.review"
               for row in store.search("zebraword", session_id="s3", scope="all"))


def test_search_kind_filter_applies_before_the_limit(store):
    marker = store.save_conclusion("tools", "zebraword note", session_id="s1")
    for index in range(25):
        store.save_conclusion("base.user_representation", f"zebraword derived {index}",
                              key=f"goal:{index}", session_id="s1")
    rows = store.search("zebraword", limit=1, session_id="s1", kinds=frozenset({"tools"}))
    assert [row["conclusions_id"] for row in rows] == [marker]
    assert all(row["kind"] == "tools"
               for row in store.search("zebraword", session_id="s1",
                                       kinds=frozenset({"tools"})))
    # Without the filter the 25 derived rows crowd the marker out of a short page: that is why
    # the filter runs in SQL before the limit instead of after it.
    assert all(row["kind"] != "tools"
               for row in store.search("zebraword", limit=1, session_id="s1"))


def test_newest_conclusion_by_content_is_the_mirror_undo(store):
    first = store.save_conclusion("note", "mirrored entry", session_id="s1")
    second = store.save_conclusion("note", "mirrored entry", session_id="s1")
    store.save_conclusion("note", "mirrored entry", session_id="s2")
    assert store.newest_conclusion_by_content("note", "mirrored entry",
                                              session_id="s1")["id"] == second != first


def test_stats_counts(store):
    store.save_conclusion("note", "a note", session_id="s1")
    store.record_job("base", "s1", 0.0, 1.0)
    store.record_job("dialectic", "s2", 0.0, 1.0)
    stats = store.stats()
    assert "notes" not in stats and "markers" not in stats
    assert (stats["sessions"], stats["jobs"], stats["pending_jobs"]) == (2, 2, 2)
    assert stats["db_bytes"] > 0 and stats["db_path"].endswith("memory.db")


def test_conclusions_by_kind_only_counts_one_session(store):
    store.save_conclusion("note", "a note", session_id="s1")
    store.save_conclusion("tools", "a tool marker", session_id="s1")
    store.save_conclusion("tools", "another tool marker", session_id="s1")
    store.save_conclusion("tools", "durable tool marker")
    assert store.conclusions_by_kind("s1") == {"note": 1, "tools": 2}
    assert store.conclusions_by_kind("s2") == {}


def test_next_session_summary_index_counts_one_session(store):
    assert store.next_session_summary_index("s1") == 1
    store.save_conclusion("session_summary", "one", key="session_summary:1", session_id="s1")
    assert store.next_session_summary_index("s1") == 2
    assert store.next_session_summary_index("s2") == 1, "another session starts at one"


def test_stored_content_is_truncated(store):
    store.save_conclusion("note", "x" * 15_000)
    assert len(store.search("x" * 10)[0]["text"]) <= 12_000


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
    store.record_job("base", "s2", 10.0, 20.0)
    claimed = store.record_job("dialectic", "s3", 0.0, 10.0)
    store.claim_jobs(limit=5)  # each job is in its own session, so all can run
    # Only the job queued before the cutoff: the two later ones belong to this process.
    assert store.expire_open_jobs("gone", before=cutoff) == 1
    rows = {row["id"]: row["status"] for row in store._conn.execute("SELECT id, status FROM jobs")}
    assert rows[old] == "expired"
    assert rows[claimed] == "running" and len(rows) == 3


def test_reclaim_requeues_running_jobs_left_by_a_previous_process(store):
    job = store.record_job("base", "s1", 0.0, 10.0)
    store.claim_jobs(limit=1)
    assert store.reclaim_open_jobs(before=time.time() - 1.0) == 0, \
        "a job claimed by this process must be left running"
    cutoff = time.time() + 0.001  # a later process starts
    assert store.reclaim_open_jobs(before=cutoff) == 1
    row = store._conn.execute(
        "SELECT status, attempts, reclaims, claimed_at, error FROM jobs WHERE id = ?",
        (job,)).fetchone()
    assert (row["status"], row["attempts"], row["reclaims"], row["claimed_at"],
            row["error"]) == ("pending", 1, 1, None, ""), \
        "a process death is charged to reclaims, not to the job's attempts"
    # Two more deaths still requeue the job; the fourth closes it.
    for expected in (2, 3):
        store.claim_jobs(limit=1)
        assert store.reclaim_open_jobs(before=time.time() + 0.001) == 1
        row = store._conn.execute(
            "SELECT status, attempts, reclaims FROM jobs WHERE id = ?", (job,)).fetchone()
        assert (row["status"], row["attempts"], row["reclaims"]) == ("pending", expected, expected)
    store.claim_jobs(limit=1)
    assert store.reclaim_open_jobs(before=time.time() + 0.001) == 1
    row = store._conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job,)).fetchone()
    assert row["status"] == "failed" and "process" in row["error"]


def test_a_reclaim_does_not_spend_the_failure_budget(store):
    """One death buys an attempt: the job still gets its two real failures afterwards."""
    job = store.record_job("base", "s1", 0.0, 10.0)
    store.claim_jobs(limit=1)
    assert store.reclaim_open_jobs(before=time.time() + 0.001) == 1
    for expected in ("pending", "failed"):
        store.claim_jobs(limit=1)
        assert store.fail_job(job, "LayerMergeError: boom") == expected


def test_open_sets_a_busy_timeout_and_skips_a_current_schema(plugin_package, tmp_path, monkeypatch):
    path = tmp_path / "v" / "memory.db"
    first = plugin_package.store.MinirStore(path)
    version = first._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == plugin_package.store._SCHEMA_VERSION
    assert first._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    first.close()

    migrations: list[int] = []
    monkeypatch.setattr(plugin_package.store.MinirStore, "_migrate",
                        lambda self: migrations.append(1))
    again = plugin_package.store.MinirStore(path)
    try:
        assert migrations == [], "a file at the current version is not migrated again"
        assert again._conn.execute("PRAGMA user_version").fetchone()[0] == version
    finally:
        again.close()


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
    """Durable cards recall across sessions; a window row or review never leaks."""
    durable = store.save_conclusion("base.user_peer_card", "prefers terse answers",
                                    key="user:ada", subject="user:ada")
    summary = store.save_conclusion("base.window_summary", "deploy the Windows build",
                                    key="session:s1:user:ada", session_id="s1",
                                    subject="user:ada")
    store.put_layer_state("dialectic", "agent:default:minir",
                          "## Base Revision\n- user_preferences: skipped the smoke test",
                          session_id="s1")

    assert store.search("terse", session_id="s1")[0]["conclusions_id"] == durable
    assert store.search("terse", session_id="s2")[0]["conclusions_id"] == durable, \
        "the user peer card is durable across sessions"
    assert store.search("Windows build", session_id="s1")[0]["conclusions_id"] == summary
    assert store.search("Windows build", session_id="s2") == [], \
        "a window row must not leak into another session"
    assert store.search("Windows build", session_id="") == [], \
        "a session-bound row is not visible without a session"
    assert store.search("skipped smoke", session_id="s1")[0]["kind"] == "dialectic.review"
    assert not any(row["kind"] == "dialectic.review"
                   for row in store.search("skipped smoke", session_id="s2"))


def test_compression_gate_pauses_until_cleared_or_expired(store):
    now = time.time()
    store.set_compression_gate("s1", expires_at=now + 60, run_id="r1")
    assert store.compression_gate_active("s1", now=now) is True
    assert store.compression_gate_active("s1", now=now + 61) is False
    store.clear_compression_gate("s1")
    assert store.compression_gate_active("s1") is False


def test_save_conclusion_is_indexed_and_forget_removes_the_hit(store):
    row_id = store.save_conclusion("note", "fts indexed conclusion")
    assert store.search("indexed conclusion")
    assert store.forget_conclusion("note", row_id) is True
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
        ai_identity_card="direct", window_summary=summary,
        value_markers=markers)


def test_base_refreshes_keep_history_and_exactly_one_latest(store):
    first = _refresh(store, from_ts=0.0, to_ts=10.0, from_turn=1, to_turn=2)
    second = _refresh(store, from_ts=10.0, to_ts=20.0, from_turn=3, to_turn=4,
                      summary="second window", peer="terse, no emoji")

    latest = store.latest_base_refresh("s1", "user:ada")
    assert latest["id"] == second and latest["is_latest"] == 1
    assert (latest["window_from_ts"], latest["window_to_ts"]) == (10.0, 20.0)
    assert (latest["window_from_turn"], latest["window_to_turn"]) == (3, 4)
    assert latest["window_summary"] == "second window"
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
    assert store.latest_base_refresh("s1")["window_summary"] == "retried attempt"


def test_claims_are_fifo_within_a_session(store):
    """A same-turn review waits for the base job before it: no layer priority, only order."""
    base_one = store.record_job("base", "s1", 0.0, 10.0)
    base_two = store.record_job("base", "s1", 10.0, 20.0)
    review = store.record_job("dialectic", "s1", 0.0, 10.0)
    assert store.claim_jobs(limit=3)[0]["id"] == base_one
    assert store.claim_jobs(limit=3) == []  # the running base blocks the rest of the session
    store.complete_job(base_one, 10.0)
    assert store.claim_jobs(limit=3)[0]["id"] == base_two
    store.complete_job(base_two, 20.0)
    assert store.claim_jobs(limit=3)[0]["id"] == review


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
    assert "advices" not in revision, "the legacy column is gone"
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
    store.put_layer_state("base", "user:ada", live, session_id="s1", ai_subject="agent:default:minir")

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


def test_legacy_base_refreshes_rename_the_window_summary_and_gain_value_markers_and_fts(
        plugin_package, tmp_path):
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
        assert "session_chunk_summary" not in row
        assert row["window_summary"] == "- old summary"
        assert row["value_markers"] == ""
        hits = reopened.search_base_refreshes("legacy")
        assert hits and hits[0]["user_peer_card"] == "- legacy tool"
    finally:
        reopened.close()




def test_legacy_two_table_database_migrates_to_conclusions_only(plugin_package, tmp_path):
    """An old database loses ``notes`` and renames ``base_revisions`` in one open."""
    path = tmp_path / "legacy" / "memory.db"
    path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(str(path))
    try:
        legacy.executescript(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " source TEXT NOT NULL DEFAULT 'tool', content TEXT NOT NULL,"
            " created_at REAL NOT NULL, session_id TEXT NOT NULL DEFAULT '',"
            " marker_type TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '');"
            "INSERT INTO notes (source, content, created_at, session_id, marker_type, category)"
            " VALUES ('tool', 'old marker', 1.0, 's1', 'tools', 'KNOWLEDGE');"
            "CREATE TABLE base_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',"
            " base_refresh_id INTEGER NOT NULL DEFAULT 0, window_from_ts REAL NOT NULL,"
            " window_to_ts REAL NOT NULL, window_from_turn INTEGER, window_to_turn INTEGER,"
            " user_representation TEXT NOT NULL DEFAULT '',"
            " user_peer_card TEXT NOT NULL DEFAULT '',"
            " ai_self_representation TEXT NOT NULL DEFAULT '',"
            " ai_identity_card TEXT NOT NULL DEFAULT '',"
            " window_summary TEXT NOT NULL DEFAULT '',"
            " value_markers TEXT NOT NULL DEFAULT '',"
            " advices TEXT NOT NULL DEFAULT '[]', changed_fields TEXT NOT NULL DEFAULT '[]',"
            " source_job_id INTEGER NOT NULL DEFAULT 0,"
            " is_latest INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);"
            "INSERT INTO base_revisions (session_id, subject, window_from_ts, window_to_ts,"
            " window_from_turn, window_to_turn, user_peer_card, advices, changed_fields,"
            " created_at) VALUES ('s1', 'user:ada', 0, 10, 1, 2, 'old peer', '[]',"
            " '[\"user_peer_card\"]', 1.0);")
        legacy.commit()
    finally:
        legacy.close()

    reopened = plugin_package.store.MinirStore(path)
    try:
        tables = {row[0] for row in reopened._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "notes" not in tables and "base_revisions" not in tables
        columns = {row[1] for row in reopened._conn.execute(
            "PRAGMA table_info(dialectic_refreshes)")}
        assert "advices" not in columns and "window_from_ts" in columns
        rows = reopened.base_revisions("s1", "user:ada")
        assert len(rows) == 1 and rows[0]["user_peer_card"] == "old peer"
        assert (rows[0]["window_from_ts"], rows[0]["window_to_turn"]) == (0.0, 2)
        assert reopened.search_base_revisions("old peer", session_id="s1")
    finally:
        reopened.close()

def test_legacy_session_chunk_summary_kind_is_renamed(plugin_package, tmp_path):
    path = tmp_path / "mem" / "memory.db"
    first = plugin_package.store.MinirStore(path)
    try:
        first.save_conclusion("session_chunk_summary", "window body", key="chunk:s1:0:10",
                              session_id="s1", subject="session")
    finally:
        first.close()
    reopened = plugin_package.store.MinirStore(path)
    try:
        row = reopened.get_conclusion("window_summary", "chunk:s1:0:10")
        assert row is not None and row["content"] == "window body"
        assert reopened.get_conclusion("session_chunk_summary", "chunk:s1:0:10") is None
    finally:
        reopened.close()


def test_a_pending_legacy_handoff_row_expires_on_reopen(plugin_package, tmp_path):
    path = tmp_path / "mem" / "memory.db"
    first = plugin_package.store.MinirStore(path)
    try:
        first.save_conclusion("handoff", "old calibration", key="handoff:s1:1",
                              session_id="s1", subject="session", status="pending")
    finally:
        first.close()

    reopened = plugin_package.store.MinirStore(path)
    try:
        row = reopened.get_conclusion("handoff", "handoff:s1:1")
        assert row is not None and row["status"] == "expired"
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


def test_release_advisories_only_returns_injected_rows(store):
    """An accepted host delivery that never landed must not strand its advice."""
    ids = store.add_advisories(
        "s1", [{"content": "re-read the diff", "severity": "concern"}], source="adviser_mode")
    assert store.mark_advisories_injected(ids) == 1
    assert store.release_advisories(ids) == 1
    row = store.pending_advisories("s1", source="adviser_mode")[0]
    assert row["status"] == "pending" and row["injected_at"] is None
    # A second release is a no-op; a staged row belongs to reset_staged instead.
    assert store.release_advisories(ids) == 0
    store.stage_advisories(ids, "req-1")
    assert store.release_advisories(ids) == 0
    assert store.reset_staged(ids) == 1


def test_publish_base_conclusions_writes_the_six_fields_once(store):
    """One conclusion per calibrated column; user/agent rows durable, window rows session-bound."""
    store.add_base_refresh(
        "s1", "user:ada", window_from_ts=0.0, window_to_ts=10.0,
        window_from_turn=1, window_to_turn=2,
        user_representation="- [goal] ship", user_peer_card="- terse",
        ai_self_representation="- [done] x", ai_identity_card="- direct",
        window_summary="- window", value_markers='{"value_markers": []}')
    assert store.publish_base_conclusions(
        "s1", user_subject="user:ada", ai_subject="agent:default:minir") == 6

    rows = {row["kind"]: dict(row) for row in store._conn.execute(
        "SELECT kind, session_id, key, content FROM conclusions WHERE kind LIKE 'base.%'")}
    assert set(rows) == {"base.user_representation", "base.user_peer_card",
                         "base.ai_self_representation", "base.ai_identity_card",
                         "base.window_summary", "base.value_markers"}
    for kind in ("base.user_representation", "base.user_peer_card",
                 "base.ai_identity_card"):
        assert rows[kind]["session_id"] == "", f"{kind} must stay durable"
    # The AI self representation is a ruler for the session that produced it.
    assert rows["base.ai_self_representation"]["session_id"] == "s1"
    assert rows["base.ai_self_representation"]["key"].startswith("session:s1:")
    assert rows["base.window_summary"]["session_id"] == "s1"
    assert rows["base.value_markers"]["session_id"] == "s1"
    assert rows["base.value_markers"]["content"] == '{"value_markers": []}', \
        "the column value is stored verbatim"

    # A second caller (another hook, another provider instance) publishes nothing, and a later
    # refresh window of the same session does not reopen it either.
    assert store.publish_base_conclusions(
        "s1", user_subject="user:ada", ai_subject="agent:default:minir") == 0
    store.add_base_refresh(
        "s1", "user:ada", window_from_ts=10.0, window_to_ts=20.0,
        window_from_turn=3, window_to_turn=4,
        user_representation="- [goal] ship more", user_peer_card="- terse",
        ai_self_representation="- [done] y", ai_identity_card="- direct",
        window_summary="- window 2", value_markers='{"value_markers": []}')
    assert store.publish_base_conclusions(
        "s1", user_subject="user:ada", ai_subject="agent:default:minir") == 0
    assert store.get_conclusion(
        "base.user_representation", "user:ada")["content"] == "- [goal] ship"


def test_session_bound_kind_needs_a_session_and_never_leaks_into_all(plugin_package, store):
    store.save_conclusion("base.ai_self_representation", "ruler text",
                          key="session:s1:agent", session_id="s1")
    kinds = frozenset({"base.ai_self_representation"})

    with pytest.raises(plugin_package.store.SessionScopeError) as exc:
        store.search("ruler", kinds=kinds)
    assert "session_scope_required" in str(exc.value)
    assert "main session id" in str(exc.value)

    with pytest.raises(plugin_package.store.SessionScopeError):
        store.search("ruler", scope="all", kinds=kinds)

    assert store.search("ruler", scope="all") == []
    assert store.search("ruler", scope="session", session_id="s2") == []
    assert store.search("ruler", scope="session", session_id="s1")[0]["kind"] == \
        "base.ai_self_representation"


def test_layer_failures_upsert_open_and_resolve(store):
    store.record_layer_failure("base", "user:ada", window_key="s1:10", session_id="s1",
                               window_from_ts=10, window_to_ts=20, error="first", attempts=1)
    store.record_layer_failure("base", "user:ada", window_key="s1:10", session_id="s1",
                               window_from_ts=10, window_to_ts=20, error="second", attempts=2)
    store.record_layer_failure("base", "user:ada", window_key="s1:30", session_id="s1",
                               window_from_ts=30, window_to_ts=40, error="other")
    assert len(store.open_layer_failures("base", "user:ada", limit=5)) == 2
    newest = store.open_layer_failures("base", "user:ada", limit=1)[0]
    assert newest["window_key"] in {"s1:10", "s1:30"}
    store.resolve_layer_failure("base", "user:ada", "s1:10")
    remaining = store.open_layer_failures("base", "user:ada", limit=5)
    assert [row["window_key"] for row in remaining] == ["s1:30"]


def test_layer_failures_close_by_window_range(store):
    store.record_layer_failure("base", "user:ada", window_key="s1:10", session_id="s1",
                               window_from_ts=10, window_to_ts=20, error="old")
    store.record_layer_failure("base", "user:ada", window_key="s1:30", session_id="s1",
                               window_from_ts=30, window_to_ts=60, error="newer")
    store.record_layer_failure("knowledge", "user:ada", window_key="s1:10", session_id="s1",
                               window_from_ts=10, window_to_ts=20, error="other layer")

    assert store.resolve_layer_failures_through("base", "user:ada", 20) == 1
    assert [row["window_key"] for row in store.open_layer_failures("base", "user:ada")] == ["s1:30"]
    assert store.resolve_layer_failures_through("base", "user:ada", 60) == 1
    assert store.open_layer_failures("base", "user:ada") == []
    assert len(store.open_layer_failures("knowledge", "user:ada")) == 1


_ARCHIVE_DDL = (
    "CREATE TABLE conclusion_versions (original_id INTEGER PRIMARY KEY, "
    "conclusion_id INTEGER NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
    "session_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', "
    "content TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', "
    "status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 0, "
    "valid_from REAL NOT NULL, valid_to REAL, superseded_by INTEGER, "
    "recorded_at REAL NOT NULL, archived_at REAL NOT NULL)")


def test_as_of_attaches_only_the_newest_archive(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "memory.db")
    try:
        instance.save_conclusion("note", "kubernetes restart policy", key="k", session_id="s1")
        folder = tmp_path / "conclusion" / "archive"
        folder.mkdir(parents=True)
        for name, content in (("conclusion_archive_20250101.db", "docker prune old"),
                              ("conclusion_archive_20260101.db", None)):
            raw = sqlite3.connect(str(folder / name))
            try:
                raw.execute(_ARCHIVE_DDL)
                if content:
                    raw.execute(
                        "INSERT INTO conclusion_versions (original_id, conclusion_id, kind, key,"
                        " content, version, valid_from, valid_to, recorded_at, archived_at)"
                        " VALUES (1, 1, 'note', 'd', ?, 0, 1000.0, 2000.0, 1000.0, 3000.0)",
                        (content,))
                raw.commit()
            finally:
                raw.close()

        assert instance.search("docker", session_id="s1", as_of=1500) == []
    finally:
        instance.close()


def test_an_archive_conflict_keeps_the_row_in_the_hot_table(plugin_package, tmp_path):
    path = tmp_path / "memory.db"
    instance = plugin_package.store.MinirStore(path)
    try:
        instance.save_conclusion("note", "docker v0", key="d", session_id="s1")
        instance.save_conclusion("note", "docker v1", key="d", session_id="s1")
        row = instance._conn.execute(
            "SELECT id, valid_from FROM conclusion_versions WHERE content = 'docker v0'"
        ).fetchone()
        archive = tmp_path / "conclusion" / "archive"
        archive.mkdir(parents=True)
        later = time.time() + 400 * 86_400
        target = archive / ("conclusion_archive_%s.db" % time.strftime(
            "%Y%m%d", time.localtime(later)))
        conn = sqlite3.connect(str(target))
        try:
            conn.execute(_ARCHIVE_DDL)
            conn.execute(
                "INSERT INTO conclusion_versions (original_id, conclusion_id, kind, key, "
                "content, version, valid_from, recorded_at, archived_at) "
                "VALUES (?, ?, 'note', 'd', 'a conflicting body', 0, ?, ?, ?)",
                (row["id"], row["id"], row["valid_from"], row["valid_from"], later))
            conn.commit()
        finally:
            conn.close()

        counts = instance.maintain_conclusion_history(archive, now=later)

        assert counts["archived"] == 0
        remaining = {item["content"] for item in instance._conn.execute(
            "SELECT content FROM conclusion_versions").fetchall()}
        assert "docker v0" in remaining
    finally:
        instance.close()


def test_folding_needs_the_age_threshold_and_runs_once(store):
    for index in range(34):
        store.save_conclusion("note", f"docker v{index}", key="d", session_id="s1")
    base = time.time()

    assert store.maintain_conclusion_history(now=base + 179 * 86_400)["folded"] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM conclusion_versions WHERE status = 'collapsed'"
    ).fetchone()[0] == 0

    assert store.maintain_conclusion_history(now=base + 181 * 86_400)["folded"] == 2
    assert store._conn.execute(
        "SELECT COUNT(*) FROM conclusion_versions WHERE status = 'collapsed'"
    ).fetchone()[0] == 1
    assert store.maintain_conclusion_history(now=base + 181 * 86_400)["folded"] == 0


def test_v4_backfill_gives_each_eligible_row_one_open_version(plugin_package, tmp_path):
    path = tmp_path / "memory.db"
    instance = plugin_package.store.MinirStore(path)
    instance.save_conclusion("note", "docker prune", key="n", session_id="s1")
    instance.save_conclusion("todo", "ship it", key="t", session_id="s1")
    instance.close()
    raw = sqlite3.connect(str(path))
    raw.execute("DROP TABLE conclusion_versions")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()

    migrated = plugin_package.store.MinirStore(path)
    try:
        rows = migrated._conn.execute(
            "SELECT kind, COUNT(*) AS n, SUM(valid_to IS NULL) AS open FROM "
            "conclusion_versions GROUP BY kind").fetchall()
    finally:
        migrated.close()

    assert [(row["kind"], row["n"], row["open"]) for row in rows] == [("note", 1, 1)]


def test_expansion_stops_at_the_fanout_cap(store):
    store.save_conclusion("note", "docker hub", key="hub", session_id="s1")
    for index in range(12):
        # the children must not match the query, or they arrive as lexical hits and prove nothing
        store.save_conclusion("note", f"container child {index}", key=f"c{index}", session_id="s1")
        store.link(("note", "hub"), "part_of", ("note", f"c{index}"))

    rows = store.search("docker hub", session_id="s1", expand=1)

    assert 1 <= sum(1 for row in rows if row.get("via")) <= 10


def test_as_of_boundaries_pick_the_version_in_force(store):
    store.save_conclusion("note", "docker v1", key="d", session_id="s1")
    first = time.time()
    time.sleep(0.03)
    store.save_conclusion("note", "docker v2", key="d", session_id="s1")
    second = time.time()
    time.sleep(0.03)
    store.save_conclusion("note", "docker v3", key="d", session_id="s1")
    third = time.time()

    assert [row["text"] for row in store.search("docker", session_id="s1", as_of=first)] == [
        "docker v1"]
    assert [row["text"] for row in store.search("docker", session_id="s1", as_of=second)] == [
        "docker v2"]
    assert [row["text"] for row in store.search("docker", session_id="s1", as_of=third)] == [
        "docker v3"]


def test_a_todo_write_creates_no_version_row(store):
    store.save_conclusion("todo", "ship the release", key="t", session_id="s1")
    row = store.get_conclusion("todo", "t")

    assert store._conn.execute(
        "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_id = ?",
        (row["id"],)).fetchone()[0] == 0


def test_link_refuses_missing_endpoints_and_upserts(store):
    store.save_conclusion("note", "a", key="a", session_id="s1")
    store.save_conclusion("note", "b", key="b", session_id="s1")

    assert store.link(("note", "a"), "part_of", ("note", "missing")) is False
    assert store.link(("note", "a"), "part_of", ("note", "b")) is True
    assert store.link(("note", "a"), "part_of", ("note", "b")) is True

    assert store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE src_key = 'a' AND rel = 'part_of'"
    ).fetchone()[0] == 1


def test_merge_meta_and_unmerge_restore_the_row(store):
    source = store.save_conclusion("note", "docker prune", key="src", session_id="s1")
    store.save_conclusion("note", "docker prune images", key="dst", session_id="s1")
    assert store.merge_into(("note", "src"), ("note", "dst"), score=0.95, now=100.0)

    meta = json.loads(store._conn.execute(
        "SELECT meta FROM edges WHERE src_key = 'src' AND rel = 'same_as'").fetchone()["meta"])
    assert (meta["prev_status"], meta["score"], meta["merged_at"]) == ("active", 0.95, 100.0)
    assert [row["text"] for row in store.search("docker", session_id="s1")] == [
        "docker prune images"]

    result = store.unmerge("note", "src", "wrong merge", now=200.0)
    assert result["unmerged"] is True and result["restored_status"] == "active"
    assert store.get_conclusion("note", "src")["status"] == "active"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE src_key = 'src' AND rel = 'same_as'"
    ).fetchone()[0] == 0
    latest = store._conn.execute(
        "SELECT version, valid_to FROM conclusion_versions WHERE conclusion_id = ? "
        "ORDER BY version DESC LIMIT 1", (source,)).fetchone()
    assert latest["valid_to"] is None and latest["version"] == 1

    assert store.unmerge("note", "src", "again")["unmerged"] is False
    with pytest.raises(ValueError):
        store.unmerge("note", "src", "")


def test_alias_chains_resolve_and_a_cycle_is_refused(store):
    for key in ("a", "b", "c"):
        store.save_conclusion("note", f"docker {key}", key=key, session_id="s1")
    assert store.merge_into(("note", "a"), ("note", "b"))
    assert store.merge_into(("note", "b"), ("note", "c"))

    assert store.resolve_alias("note", "a") == ("note", "c")
    assert store.resolve_alias("note", "c") == ("note", "c")
    with pytest.raises(ValueError):
        store.merge_into(("note", "c"), ("note", "a"))


def test_scheduler_runs_keep_the_newest_two_hundred_per_task(store):
    now = time.time()
    for _ in range(205):
        store.record_scheduler_run("card_review", "tick", "ok", "", now - 10, now - 10, 1.0)
    store.record_scheduler_run("card_review_other", "tick", "ok", "", now - 10, now - 10, 1.0)

    store.maintain_conclusion_history(now=now)

    counts = dict(store._conn.execute(
        "SELECT task, COUNT(*) FROM scheduler_runs GROUP BY task").fetchall())
    assert counts == {"card_review": 200, "card_review_other": 1}


def test_scheduler_runs_age_out_at_ninety_days(store):
    now = time.time()
    store.record_scheduler_run("card_review", "tick", "ok", "", now - 100 * 86_400,
                               now - 100 * 86_400, 1.0)
    store.record_scheduler_run("card_review", "tick", "ok", "", now - 80 * 86_400,
                               now - 80 * 86_400, 1.0)

    store.maintain_conclusion_history(now=now)

    assert len(store.scheduler_runs(limit=10)) == 1


def test_page_topics_are_merge_candidates_but_never_auto_merged(store):
    counts = store.generate_merge_candidates(page_topics=[
        {"path": "tools/METHOD-docker-prune.md", "topic": "Docker prune image"},
        {"path": "tools/METHOD-docker-pruning.md", "topic": "Docker prune images"}])

    assert counts == {"merged": 0, "advised": 1}
    rows = store.pending_advisories("", limit=5)
    assert any("page tools/METHOD-docker-prune.md" in row["content"] for row in rows)


def test_default_search_matches_the_first_golden(store):
    """A regression lock on the default ``search`` output, created after the strict alignment.

    No pre-change snapshot exists in the repository, so this cannot prove the output matched the
    old build; it pins what ships from here on: same rows, same order, same RRF scores.
    """
    store.save_conclusion("note", "docker system prune removes dangling images", session_id="s1")
    store.save_conclusion("note", "pytest -q keeps the feedback loop short", session_id="s1")
    store.save_conclusion("entities", "Docker is the container runtime", key="Docker",
                          session_id="s1")

    rows = store.search("docker", session_id="s1")

    note = store.search("docker", session_id="s1", kinds=frozenset({"note"}))[0]
    assert [(row["kind"], row["text"], round(row["score"], 6)) for row in rows] == [
        ("entities", "Docker is the container runtime", 0.016667),
        ("note", "docker system prune removes dangling images", 0.016393)]
    assert note["key"].startswith("note:")


def test_rrf_fuses_the_lexical_and_vector_channels(store):
    store.save_conclusion("note", "docker prune keeps images", key="a", session_id="s1")
    second = store.save_conclusion("note", "docker prune removes images", key="b", session_id="s1")

    rows = store.search("docker", session_id="s1", vector_ranks={second: 0})

    assert len(rows) == 2
    # both rows are lexical hits; the vector hit also carries the vector term at rank 0
    assert sorted(round(row["score"], 6) for row in rows) == sorted(
        [round(1 / 60, 6), round(1 / 61 + 0.7 / 60, 6)])
    assert set(rows[0]) == {"conclusions_id", "kind", "key", "text", "when", "author",
                            "subject", "session_id", "score", "rank"}


def test_a_vector_only_hit_is_fused_in_without_a_lexical_rank(store):
    hit = store.save_conclusion("note", "kubernetes pod restart policy", key="k",
                                session_id="s1")

    rows = store.search("docker", session_id="s1", vector_ranks={hit: 0})

    assert [row["key"] for row in rows] == ["k"]
    assert rows[0]["score"] == pytest.approx(0.7 / 60)


def test_a_saved_conclusion_reaches_the_index_hook(store):
    seen: list[tuple[int, str]] = []
    store.on_conclusion_saved = lambda cid, text: seen.append((cid, text))

    store.save_conclusion("note", "docker prune removes images", key="d", session_id="s1")

    assert seen and seen[0][1] == "docker prune removes images"


def test_a_dangling_edge_is_not_expanded(store):
    parent = store.save_conclusion("note", "the parent", key="parent", session_id="s1")
    store.save_conclusion("note", "the child", key="child", session_id="s1")
    assert store.link(("note", "parent"), "part_of", ("note", "child"))
    assert [row["key"] for row in store.edges_from(("note", "parent"))] == ["child"]

    assert store.forget_conclusion("note", store.get_conclusion("note", "child")["id"])

    assert store.edges_from(("note", "parent")) == []
    assert store.get_conclusion("note", "parent")["id"] == parent


def test_as_of_returns_a_row_after_forget(store):
    store.save_conclusion("note", "docker system prune clears dangling images", session_id="s1")
    hit = store.search("docker", session_id="s1")[0]
    before = time.time()
    time.sleep(0.03)  # the deletion must land strictly after the as-of instant
    assert store.forget_conclusion("note", hit["conclusions_id"])
    assert store.search("docker", session_id="s1") == []

    rows = store.search("docker", session_id="s1", as_of=before)

    assert [row["text"] for row in rows] == ["docker system prune clears dangling images"]
    assert (rows[0]["version"], rows[0]["version_status"]) == (0, "deleted")


def test_include_superseded_reaches_a_superseded_row(store):
    store.save_conclusion("note", "docker prune current", key="docker", session_id="s1")
    store.save_conclusion("note", "docker prune retired", key="docker-old", session_id="s1",
                          status="superseded")

    assert [row["text"] for row in store.search("docker", session_id="s1")] == [
        "docker prune current"]
    both = store.search("docker", session_id="s1", include_superseded=True)
    assert {row["text"] for row in both} == {"docker prune current", "docker prune retired"}


def test_as_of_reads_a_version_archived_to_a_file(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "memory.db")
    try:
        instance.save_conclusion("note", "docker prune v1", key="docker", session_id="s1")
        time.sleep(0.03)
        stamp = time.time()
        time.sleep(0.03)  # v2 must be strictly newer than the as-of instant
        instance.save_conclusion("note", "docker prune v2", key="docker", session_id="s1")
        archive = tmp_path / "conclusion" / "archive"
        later = stamp + 200 * 86_400
        assert instance.maintain_conclusion_history(archive, now=later)["archived"] == 1

        rows = instance.search("docker", session_id="s1", as_of=stamp)

        assert [row["text"] for row in rows] == ["docker prune v1"]
    finally:
        instance.close()


def test_conclusion_versions_append_and_answer_as_of(store):
    cid = store.save_conclusion("note", "first text", key="k1")
    store.save_conclusion("note", "second text", key="k1")
    versions = store.conclusion_versions(cid)
    assert [row["content"] for row in versions] == ["second text", "first text"]
    assert sum(1 for row in versions if row["valid_to"] is None) == 1
    assert store.version_at(cid, time.time() + 1)["content"] == "second text"
    assert store.search("second text", scope="all",
                        as_of=time.time() + 1)[0]["text"] == "second text"
    todo = store.save_conclusion("todo", "a task", key="t1")
    assert store.conclusion_versions(todo) == []


def test_edges_expand_only_to_existing_rows_and_merge_is_reversible(store):
    store.save_conclusion("projects", "project row", key="p1")
    store.save_conclusion("experiences", "lesson row", key="e1")
    store.save_conclusion("tools", "tool row", key="t1")
    assert store.link(("projects", "p1"), "part_of", ("experiences", "e1"))
    assert store.link(("experiences", "e1"), "instance_of", ("tools", "t1"))
    assert not store.link(("projects", "p1"), "part_of", ("tools", "missing"))
    hops = store.edges_from(("projects", "p1"), max_depth=2)
    assert {(row["kind"], row["key"]) for row in hops} == {("experiences", "e1"),
                                                           ("tools", "t1")}
    assert store.merge_into(("experiences", "e1"), ("projects", "p1"))
    assert store.resolve_alias("experiences", "e1") == ("projects", "p1")
    assert store.unmerge("experiences", "e1", "wrong merge")["unmerged"] is True
    assert store.get_conclusion("experiences", "e1")["status"] == "active"


def test_scheduler_runs_are_recorded_through_the_store(plugin_package, store):
    scheduler = plugin_package.scheduler.Scheduler(
        record=store.record_scheduler_run)
    calls: list[str] = []
    scheduler.add_time("probe", 1, lambda: calls.append("run") or True, first_delay=0.0)
    assert scheduler.tick(now=time.monotonic() + 0.01) == ["probe"]
    rows = store.scheduler_runs("probe")
    assert len(rows) == 1 and rows[0]["outcome"] == "ok"
    assert calls == ["run"]


def test_history_maintenance_folds_old_versions_and_archives_them(store, tmp_path):
    cid = store.save_conclusion("note", "current", key="k1")
    old = time.time() - 200 * 86400
    with store._lock:
        store._conn.executemany(
            "INSERT INTO conclusion_versions (conclusion_id, kind, key, content, status, "
            "version, valid_from, valid_to, recorded_at) VALUES (?, 'note', 'k1', ?, "
            "'active', ?, ?, ?, ?)",
            [(cid, f"old {index}", index, old + index, old + index + 1, old + index)
             for index in range(40)])
        store._conn.commit()
    result = store.maintain_conclusion_history(tmp_path / "archive", now=time.time())
    assert result["folded"] > 0 and result["archived"] > 0
    archive = next((tmp_path / "archive").glob("conclusion_archive_*.db"))
    connection = sqlite3.connect(str(archive))
    try:
        collapsed = connection.execute(
            "SELECT COUNT(*) FROM conclusion_versions WHERE status = 'collapsed'").fetchone()[0]
    finally:
        connection.close()
    assert collapsed == 1
    remaining = store.conclusion_versions(cid, limit=100)
    assert any(row["valid_to"] is None for row in remaining)
    archived = store.version_at(cid, old + 10, tmp_path / "archive")
    assert archived and archived.get("archived")

def test_claim_stats_count_cited_blocked_and_a_separate_repeat_row(store):
    first = store.add_advisories("s1", [{
        "content": "prefer pytest", "severity": "concern", "target": "general",
        "claim": "card:preference.md#3"}], source="adviser_mode")
    store.mark_advisory_outcome(first[0], "blocked")

    repeat_id = store.repeat_claim("s1", "card:preference.md#3", text="prefer pytest again",
                                   severity="concern", target="general")

    assert repeat_id is not None
    row = store._conn.execute("SELECT * FROM advisories WHERE id = ?", (repeat_id,)).fetchone()
    assert (row["outcome"], row["status"], row["claim_id"]) == (
        "repeated", "dropped", "card:preference.md#3")
    stats = store.claim_stats("s1")
    assert stats[0]["claim_id"] == "card:preference.md#3"
    assert (stats[0]["cited"], stats[0]["blocked"], stats[0]["repeated"]) == (2, 1, 1)
    assert repeat_id not in [row["id"] for row in store.pending_advisories("s1", limit=10)]
    assert store.claim_stats("other") == []

def test_merge_candidates_auto_merge_and_advise(store):
    store.save_conclusion("entities", "acme one", key="acme-1", subject="Acme Corp")
    store.save_conclusion("entities", "acme two", key="acme-2", subject="Acme Corp.")
    store.save_conclusion("entities", "docker", key="d1", subject="docker")
    store.save_conclusion("entities", "dockers", key="d2", subject="dockers")
    counts = store.generate_merge_candidates()
    assert counts["merged"] >= 1 and counts["advised"] >= 1
    statuses = {key: store.get_conclusion("entities", key)["status"]
                for key in ("acme-1", "acme-2")}
    assert "merged" in statuses.values()
    merged_key = next(key for key, status in statuses.items() if status == "merged")
    other = "acme-1" if merged_key == "acme-2" else "acme-2"
    assert store.resolve_alias("entities", merged_key) == ("entities", other)
    merge_notes = [row for row in store.pending_advisories("", limit=10)
                   if row["target"] == "merge_candidate"]
    assert merge_notes and "entities:d1" in merge_notes[0]["content"]


def test_a_late_blocker_counts_as_blocked_for_its_claim(store):
    row = store.add_advisories("s1", [{
        "content": "late_blocker: operator claim soul:SOUL.md#2 refused terminal",
        "severity": "blocker", "target": "late_blocker", "claim": "soul:SOUL.md#2"}],
        source="adviser_mode")
    store.mark_advisory_outcome(row[0], "blocked")

    stats = store.claim_stats("s1")

    assert [(item["claim_id"], item["blocked"]) for item in stats] == [("soul:SOUL.md#2", 1)]


def test_claim_coverage_and_target_coverage_are_separate_columns(store):
    review = store.start_adviser_review("s", "t", "window", 0, 1)
    store.set_adviser_review_claims(
        review, [{"id": "card:user/preference.md#2"}],
        {"card:user/preference.md#2": "clean"})
    store.finish_adviser_review(review, status="done", coverage='{"general": "clean"}')

    row = store.adviser_reviews("s")[0]
    assert json.loads(row["claim_coverage"]) == {"card:user/preference.md#2": "clean"}
    assert json.loads(row["coverage"]) == {"general": "clean"}


def _shape(instance):
    """Every non-FTS table and its columns: what a reader of the database can depend on."""
    tables = [row[0] for row in instance._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '%_fts%' ORDER BY name").fetchall()]
    shape = {name: [column[1] for column in instance._conn.execute(
        f"PRAGMA table_info({name})").fetchall()] for name in tables}
    version = instance._conn.execute("PRAGMA user_version").fetchone()[0]
    return shape, int(version)


def test_a_migrated_database_has_the_same_shape_as_a_fresh_one(plugin_package, tmp_path):
    legacy_path = tmp_path / "legacy" / "memory.db"
    legacy_path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(str(legacy_path))
    try:
        legacy.executescript(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " source TEXT NOT NULL DEFAULT 'tool', content TEXT NOT NULL,"
            " created_at REAL NOT NULL, session_id TEXT NOT NULL DEFAULT '');"
            "CREATE TABLE base_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',"
            " base_refresh_id INTEGER NOT NULL DEFAULT 0, window_from_ts REAL NOT NULL,"
            " window_to_ts REAL NOT NULL, window_from_turn INTEGER, window_to_turn INTEGER,"
            " user_representation TEXT NOT NULL DEFAULT '',"
            " user_peer_card TEXT NOT NULL DEFAULT '',"
            " ai_self_representation TEXT NOT NULL DEFAULT '',"
            " ai_identity_card TEXT NOT NULL DEFAULT '',"
            " window_summary TEXT NOT NULL DEFAULT '',"
            " value_markers TEXT NOT NULL DEFAULT '',"
            " advices TEXT NOT NULL DEFAULT '[]', changed_fields TEXT NOT NULL DEFAULT '[]',"
            " source_job_id INTEGER NOT NULL DEFAULT 0,"
            " is_latest INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);")
        legacy.commit()
    finally:
        legacy.close()

    migrated = plugin_package.store.MinirStore(legacy_path)
    fresh = plugin_package.store.MinirStore(tmp_path / "fresh" / "memory.db")
    try:
        assert _shape(migrated) == _shape(fresh)
    finally:
        migrated.close()
        fresh.close()
