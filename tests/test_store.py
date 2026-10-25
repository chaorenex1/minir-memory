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
    assert top["kind"] == "entities" and top["conclusions_id"] == row_id and top["score"] >= 2
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

def test_claim_stats_and_repeat_guard_count_the_same_claim(store):
    first = store.add_advisories("s1", [{
        "content": "prefer pytest", "severity": "concern", "target": "general",
        "claim": "card:preference.md#3"}], source="adviser_mode")
    store.mark_advisory_outcome(first[0], "blocked")
    store.add_advisories("s1", [{
        "content": "prefer pytest again", "severity": "concern", "target": "general",
        "claim": "card:preference.md#3"}], source="adviser_mode")
    assert store.mark_claim_repeated("s1", "card:preference.md#3") is True
    stats = store.claim_stats("s1")
    assert stats[0]["claim_id"] == "card:preference.md#3"
    assert stats[0]["cited"] == 2 and stats[0]["blocked"] == 1
    assert stats[0]["repeated"] == 1
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
