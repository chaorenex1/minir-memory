"""Provider behavior — lifecycle, recall, persistence, counting, and tools."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import types


def _no_model_call(*args, **kwargs):
    """Poisons the layer model call: any test that reaches it fails loudly."""
    raise AssertionError("the layer worker must not call the model in this test")


def _base_json(*, summary=None, user=None, peer=None, ai_self=None, identity=None,
               markers=None):
    """A full six-key base reply for the one-shot fallback."""
    return json.dumps({
        "session_chunk_summary": summary or ["deploy on Windows"],
        "user_representation": user or ["[goal] ship"],
        "user_peer_card": peer or ["wants evidence"],
        "ai_self_representation": ai_self or ["direct"],
        "ai_identity_card": identity or ["careful"],
        "value_markers": markers or {
            "entities": ["Windows"],
            "trajectories": [],
            "experiences": [],
            "tools": ["pytest"],
            "projects": ["minir-memory"],
        },
    })


def _dialectic_json(*, text="run a smoke test", severity="concern", target="user_peer_card",
                    advices=None, changed=False, fields=None):
    return json.dumps({
        "user_representation": ["[goal] ship"],
        "user_peer_card": ["wants evidence"],
        "ai_self_representation": ["[done] ran the smoke test"],
        "ai_identity_card": ["careful"],
        "session_chunk_summary": ["[done] deployed on Windows"],
        "advices": advices if advices is not None else [{
            "severity": severity, "target": target, "text": text,
            "decision": "adopt it", "evidence": ["msg-1"],
        }],
        "value_markers": {"entities": ["Windows"], "trajectories": [], "experiences": [],
                          "tools": ["pytest"], "projects": ["minir-memory"]},
        "base_changed": {"changed": bool(changed), "fields": list(fields or [])},
    })


def test_initialize_creates_profile_scoped_store(provider, tmp_path):
    assert (tmp_path / "minir-memory" / "memory.db").exists()


def test_notes_survive_a_new_instance(plugin_package, make_provider):
    first = make_provider({})
    first.handle_tool_call("minir_remember", {"content": "We standardized on SQLite for the store"})
    first.shutdown()

    second = make_provider({})  # same tmp home -> same db file
    block = second.prefetch("SQLite store")
    assert "SQLite" in block
    assert block.startswith("## Minir recall")


def test_no_conversation_content_is_persisted_anywhere(plugin_package, make_provider, monkeypatch):
    """The provider's whole point of honour: turns are reasoned over in memory and dropped."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, *args, **kwargs: (
        _dialectic_json()
        if task == plugin_package.worker.DIALECTIC_LAYER_TASK else
        _base_json(summary=["s"], user=["[goal] x"], peer=["y"],
                   ai_self=["direct"], identity=["careful"])))
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    provider.sync_turn("the secret sauce is a staging cluster in Frankfurt", "noted")
    provider._worker.stop()
    db_path = provider._store.path
    provider.shutdown()

    connection = sqlite3.connect(str(db_path))
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        assert "turns" not in tables
        for table in tables:
            if table.startswith("sqlite_"):
                continue
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            assert "Frankfurt" not in str(rows), f"{table} must hold no conversation content"
    finally:
        connection.close()


def test_a_restart_expires_windows_it_can_no_longer_read(plugin_package, make_provider):
    """The content lived in the old process's memory; the job it queued must not be retried."""
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    pending = provider._store.pending_jobs()
    assert pending, "the tick queued jobs"
    db_path = provider._store.path
    provider.shutdown()

    # A later process opens the same store: the jobs are closed, not re-queued.
    reopened = plugin_package.store.MinirStore(db_path)
    try:
        later = time.time() + 0.001  # that process started just after these jobs were queued
        assert reopened.expire_open_jobs("gone", before=later) == len(pending)
        assert reopened.pending_jobs() == []
        statuses = {row["status"] for row in reopened._conn.execute("SELECT status FROM jobs")}
        assert statuses == {"expired"}
    finally:
        reopened.close()


def test_sync_turn_keeps_the_author_for_the_base_layer(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.sync_turn("kv cache tuning preferences", "Noted.",
                       turn_author={"id": "u1", "name": "alice", "is_bot": False})
    assert [(row["author"], row["author_id"]) for row in provider._transcript.messages("session-test")] \
        == [("alice", "u1"), ("", "")]


def test_sync_turn_does_nothing_when_the_layers_are_off(make_provider):
    provider = make_provider({})
    provider.sync_turn("something important about staging deploys", "ok")
    assert provider._transcript.messages("session-test") == []


def test_sync_turn_skipped_for_non_primary_context(plugin_package, make_provider, monkeypatch):
    """Host contract: subagent/cron/flush contexts must not write memory."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({}, agent_context="subagent")
    provider.sync_turn("subagent scratch work that should not persist", "ok")
    assert provider._transcript.messages("session-test") == []


def test_sync_turn_skips_trivial_prompts(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.sync_turn("thanks!", "You're welcome.")
    assert provider._transcript.messages("session-test") == []


def test_a_message_never_lands_on_a_window_edge(plugin_package):
    """Windows are ``(from, to]``, so a message stamped exactly on an edge would be invisible to
    both neighbouring windows. Clocks are coarser than the tick→arrival gap, so the buffer keeps
    every message strictly past the newest edge instead of relying on the clock."""
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.mark_edge("s", 100.0)
    buffer.add("s", "user", "on the edge", created_at=100.0)   # a tie with the edge
    buffer.add("s", "user", "before the edge", created_at=50.0)
    stamps = [row["created_at"] for row in buffer.messages("s")]
    assert all(stamp > 100.0 for stamp in stamps)
    assert [row["content"] for row in buffer.window("s", 100.0, 100.01)] == ["on the edge",
                                                                           "before the edge"]
    buffer.mark_edge("s", 50.0)  # an older edge must never move the mark back
    buffer.add("s", "user", "after", created_at=150.0)
    assert buffer.messages("s")[-1]["created_at"] == 150.0


def test_the_provider_never_buffers_a_message_on_an_edge(plugin_package, make_provider, monkeypatch):
    """The regression that motivated ``mark_edge``: an interval of 1 puts a window edge on the
    very turn that follows it, and a coarse clock can stamp the message on that edge."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    edge = provider._store.pending_jobs()[0]["to_ts"]
    provider.sync_turn("deploy the plugin on Windows", "noted")
    message = provider._transcript.messages("session-test")[0]
    assert message["created_at"] > edge
    assert provider._transcript.window("session-test", edge, time.time() + 1), \
        "the turn that triggered the window must still be inside it"


def test_reasoning_can_be_enabled_by_env(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({"reasoning_enabled": False})  # env wins over config
    assert provider._reasoning is True
    assert provider._worker.enabled is True


def test_prefetch_limit_can_be_set_by_env(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_PREFETCH_LIMIT", "2")
    provider = make_provider({"prefetch_limit": 9})
    for index in range(5):
        provider._store.remember(f"deployment checklist entry {index}")
    payload = json.loads(provider.handle_tool_call("minir_search", {"query": "deployment checklist"}))
    assert len(payload["results"]) == 2


def test_queue_prefetch_feeds_next_prefetch(provider):
    provider.handle_tool_call("minir_remember", {"content": "User deploys via Docker Desktop"})
    provider.queue_prefetch("Docker Desktop")
    thread = provider._prefetch_thread
    if thread is not None:
        thread.join(timeout=5.0)
    block = provider.prefetch("Docker Desktop")
    assert "Docker Desktop" in block
    status = provider.recall_status()
    assert status is not None and status.count >= 1


def test_prefetch_falls_back_to_inline_search(provider):
    """No queued result (first turn): prefetch still recalls, bounded and inline."""
    provider.handle_tool_call("minir_remember", {"content": "staging cluster lives in eu-west"})
    block = provider.prefetch("staging cluster")
    assert "eu-west" in block


def test_prefetch_on_empty_store_is_blank(provider):
    assert provider.prefetch("nothing was ever stored") == ""
    assert provider.recall_status() is None


def test_prefetch_never_returns_stale_recall(provider):
    provider.handle_tool_call("minir_remember", {"content": "remembered once"})
    assert provider.prefetch("remembered") != ""
    assert provider.prefetch("totally unrelated empty query") == ""
    assert provider.recall_status() is None


def test_tools_roundtrip(provider):
    stored = json.loads(provider.handle_tool_call("minir_remember", {"content": "prefers terse output"}))
    assert stored["stored"] is True
    found = json.loads(provider.handle_tool_call("minir_search", {"query": "terse output"}))
    assert found["count"] >= 1
    note_id = next(row["id"] for row in found["results"] if row["kind"] == "note")
    assert json.loads(provider.handle_tool_call("minir_forget", {"note_id": note_id}))["deleted"] is True
    assert json.loads(provider.handle_tool_call("minir_forget", {"note_id": str(note_id)}))["deleted"] is False
    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert stats["notes"] == 0 and "db_path" in stats


def test_tool_argument_validation(provider):
    assert "error" in json.loads(provider.handle_tool_call("minir_search", {"query": "  "}))
    assert "error" in json.loads(provider.handle_tool_call("minir_remember", {}))
    assert "error" in json.loads(provider.handle_tool_call("minir_forget", {"note_id": "abc"}))


def test_on_memory_write_mirrors_and_removes(provider):
    provider.on_memory_write("add", "user", "user profile entry")
    assert provider._store.stats()["notes"] == 1
    provider.on_memory_write("replace", "memory", "user profile entry v2")
    assert provider._store.stats()["notes"] == 2
    provider.on_memory_write("remove", "user", "user profile entry")
    assert provider._store.stats()["notes"] == 1


def test_on_session_switch_rebinds_and_reset_clears_cache(provider):
    provider.handle_tool_call("minir_remember", {"content": "switch test fact"})
    provider.queue_prefetch("switch test")
    provider.on_session_switch("session-2")
    assert provider._session_id == "session-2"
    provider.on_session_switch("session-3", reset=True)
    assert provider.prefetch("switch test") != ""  # store survives; only caches are dropped


def test_shutdown_is_idempotent(plugin_package, make_provider):
    provider = make_provider({})
    provider.shutdown()
    provider.shutdown()  # second call must not raise


def test_search_results_are_excerpted(provider):
    provider.handle_tool_call("minir_remember", {"content": "long " * 400})
    payload = json.loads(provider.handle_tool_call("minir_search", {"query": "long"}))
    assert payload["results"]
    assert all(len(row["text"]) <= 601 for row in payload["results"])


def test_recall_block_is_bounded(make_provider):
    provider = make_provider({"prefetch_limit": 20})
    for index in range(10):
        provider._store.remember(f"bounded note {index} " + "y" * 400)
    block = provider.prefetch("bounded")
    assert "more results truncated" in block
    assert len(block) < 2600


# -- turn counting (base / dialectic layers) ----------------------------------

def _jobs(provider):
    """The queued layer jobs as ``(layer, turns)`` pairs, oldest first."""
    return [(job["layer"], job["turns"]) for job in provider._store.pending_jobs()]


def test_turn_start_queues_base_jobs_only(make_provider):
    """Dialectic is dependent: the worker queues it after the base job publishes, not on a tick."""
    provider = make_provider({"base_interval": 3, "dialectic_interval": 2})
    for turn in range(1, 7):
        provider.on_turn_start(turn, f"message {turn}")
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"], job["session_id"]) for job in jobs] == [
        ("base", 3, "session-test"), ("base", 6, "session-test")]
    assert all(job["status"] == "pending" and job["from_ts"] <= job["to_ts"] for job in jobs)


def test_new_session_ordering_keeps_the_tail(plugin_package, make_provider):
    """The host's ``/new`` order: ``on_session_finalize`` (old id) stops the session
    synchronously, then the queued ``on_session_end`` (old id) flushes the tail. The stop
    must not swallow it — and the later ``on_session_reset`` must not duplicate it."""
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail before /new", "noted")
    plugin_package._on_session_finalize(session_id="session-test")   # frozen first
    provider.on_session_end([])                              # ... tail arrives after
    plugin_package._on_session_reset(session_id="session-next", old_session_id="session-test")
    assert _jobs(provider) == [("base", 4)]


def test_session_end_flushes_the_tail_once(make_provider):
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    provider.on_session_end([])
    provider.on_session_end([])  # a second boundary hook must not re-queue the window
    assert _jobs(provider) == [("base", 4)]


def test_session_end_on_a_multiple_queues_no_extra_job(make_provider):
    """The spec's "no boundary trigger when the count sits on a multiple" case."""
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    provider.on_session_end([])
    assert _jobs(provider) == [("base", 3)]


def test_pre_compress_without_reasoning_does_not_queue_a_job(make_provider):
    provider = make_provider({"base_interval": 50, "dialectic_interval": 2})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    assert provider.on_pre_compress([]) == ""
    assert _jobs(provider) == [], "no worker means no layer refresh job"
    for turn in (4, 5):
        provider.on_turn_start(turn, "m")
    assert _jobs(provider) == []


def test_pre_compress_pauses_scheduling_and_resets_both_layers(plugin_package, make_provider,
                                                               monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, *args, **kwargs: (
        _dialectic_json() if task == plugin_package.worker.DIALECTIC_LAYER_TASK
        else _base_json()))
    provider = make_provider({"base_interval": 2, "dialectic_interval": 2})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider.on_pre_compress([])

    snapshot = plugin_package.turn_counter.TRACKER.snapshot("session-test")
    assert snapshot["layers"]["base"]["last_trigger_turn"] == 3
    assert snapshot["layers"]["dialectic"]["last_trigger_turn"] == 3
    assert provider._store.compression_gate_active("session-test") is True

    for turn in (4, 5):
        provider.on_turn_start(turn, "m")
    assert _jobs(provider) == [], "periodic scheduling is paused"

    provider._store.clear_compression_gate("session-test")
    provider.on_turn_start(6, "m")
    assert len(provider._store.pending_jobs()) >= 1, "scheduling resumes after the gate"


def test_pre_compress_drains_base_and_dependent_dialectic(plugin_package, make_provider,
                                                          monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, *args, **kwargs: (
        _dialectic_json() if task == plugin_package.worker.DIALECTIC_LAYER_TASK
        else _base_json()))

    provider = make_provider({"base_interval": 50, "dialectic_interval": 10})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail evidence", "noted")
    assert provider.on_pre_compress([]) == ""

    assert provider._store.pending_jobs() == [], "the sync drain finished both layers"
    assert provider._store.layer_state("base", "user:default") is not None
    assert provider._store.layer_state("dialectic", "agent:minir") is not None


def test_session_reset_flushes_old_tail_and_opens_new_session(plugin_package, make_provider):
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3, "session_mode": "coding"})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    provider.on_session_switch("session-next", parent_session_id="session-test", reset=True)
    # The plugin hook fires after the provider switch for the same boundary: still one job.
    plugin_package._on_session_reset(session_id="session-next", old_session_id="session-test")
    assert _jobs(provider) == [("base", 4)]
    state = plugin_package.turn_counter.TRACKER.snapshot("session-next")
    assert (state["mode"], state["layers"]["base"]["turns"]) == ("coding", 0)
    assert plugin_package.turn_counter.TRACKER.tick("session-test", 5, now=1.0) == []


def test_branch_inherits_parent_counters(make_provider):
    provider = make_provider({"base_interval": 3, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")           # base job at turn 3
    provider.on_session_switch("branch-1", parent_session_id="session-test", reset=False)
    assert provider._session_id == "branch-1"
    for turn in (5, 6):
        provider.on_turn_start(turn, "m")
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"]) for job in jobs] == [("base", 3), ("base", 6)]
    assert jobs[0]["to_ts"] == jobs[1]["from_ts"]   # the branch continues the parent's window


def test_non_primary_context_does_not_count(make_provider):
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1}, agent_context="subagent")
    provider.on_turn_start(1, "subagent scratch work")
    assert provider._store.pending_jobs() == []


def test_layer_intervals_can_be_set_by_env(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_BASE_INTERVAL", "2")
    monkeypatch.setenv("MINIR_DIALECTIC_INTERVAL", "1")
    provider = make_provider({"base_interval": 99, "dialectic_interval": 99})
    for turn in (1, 2):
        provider.on_turn_start(turn, "m")
    assert _jobs(provider) == [("base", 2)]


def test_layer_settings_come_from_config_yaml(plugin_package, monkeypatch, tmp_path):
    """``memory.minir`` in config.yaml drives the cadence — the block ``hermes memory setup``
    and the dashboard both persist (see the README's Configuration section).

    Stubs only ``hermes_cli.config`` (the module ``_load_config`` imports), so the provider's
    own read path — not just the ``__init__`` wiring — is under test.
    """
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"memory": {"minir": {
        "base_interval": 4, "dialectic_interval": 2, "session_mode": "from-config"}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    sys.modules["hermes_cli"].__path__ = []

    provider = plugin_package.MinirMemoryProvider()  # no explicit config: read config.yaml
    provider.initialize("session-test", hermes_home=str(tmp_path), platform="cli")
    try:
        tracker = plugin_package.turn_counter.TRACKER
        assert (tracker.base_interval, tracker.dialectic_interval) == (4, 2)
        assert tracker.snapshot("session-test")["mode"] == "from-config"
        for turn in range(1, 5):
            provider.on_turn_start(turn, "m")  # base interval 4
        assert _jobs(provider) == [("base", 4)]
    finally:
        provider.shutdown()


def test_subagent_session_start_does_not_disturb_the_bound_session(plugin_package, make_provider):
    """Delegate children are not persist-disabled and carry their own session id, so
    ``on_session_start`` fires for them too — it must only record that id, never move the
    counters of the session this provider is bound to (``on_turn_start`` has no session_id)."""
    provider = make_provider({"base_interval": 2, "dialectic_interval": 2})
    plugin_package._on_session_start(session_id="child-abc", model="m", platform="cli")
    for turn in (1, 2):
        provider.on_turn_start(turn, "m")
    jobs = provider._store.pending_jobs()
    assert jobs and all(job["session_id"] == "session-test" for job in jobs)
    child = plugin_package.turn_counter.TRACKER.snapshot("child-abc")
    assert (child["layers"]["base"]["turns"], child["layers"]["dialectic"]["turns"]) == (0, 0)


def test_stats_report_layer_counters_and_jobs(provider):
    for turn in range(1, 21):
        provider.on_turn_start(turn, "m")
    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert (stats["jobs"], stats["pending_jobs"]) == (1, 1)
    assert stats["layers"]["layers"]["dialectic"]["turns"] == 20
    assert stats["layers"]["layers"]["base"]["interval"] == 20
    assert stats["reasoning_enabled"] is False  # the test bootstrap disables the model calls
    assert stats["artifacts"] == []


def test_a_boundary_drains_content_the_cadence_already_flushed(plugin_package, make_provider):
    """The live defect: a layer fires on a turn's tick, so its watermark moves *before* the
    turn's messages exist — and ``to_ts`` is then stamped behind them. Nothing would ever
    reason over that turn. The boundary drain asks the content watermark instead."""
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    # The tick jobs ran to completion before this turn's messages existed (an initialize-time
    # drain, say), which is what leaves the watermark behind the content.
    for job in provider._store.pending_jobs():
        provider._store.complete_job(job["id"], job["to_ts"])
    provider._transcript.add_turn("session-test", "deploy on Windows", "noted")
    assert plugin_package.turn_counter.TRACKER.tail("session-test") == []

    provider.on_session_end([])
    jobs = provider._store.pending_jobs()
    assert [job["layer"] for job in jobs] == ["base"], "the base layer owes that turn"
    # The queued window starts at the content watermark and reaches forward, so the next drain
    # actually sees the turn the cadence had already counted.
    for job in jobs:
        covered = provider._transcript.window("session-test", job["from_ts"], time.time() + 1)
        assert [row["content"] for row in covered] == ["deploy on Windows", "noted"]


def test_a_boundary_is_quiet_while_a_queued_job_still_owns_the_content(plugin_package, make_provider):
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    provider._transcript.add_turn("session-test", "deploy on Windows", "noted")
    before = len(provider._store.pending_jobs())
    provider.on_session_end([])
    # The two tick jobs are still queued; they read to ``now`` when they run, so the boundary
    # must not add a third one for the same turns.
    assert len(provider._store.pending_jobs()) == before


def test_prefetch_injects_only_the_dialectic_artifact(provider):
    provider._store.put_layer_state("base", "user:default",
                                    "## Session Summary\n- s\n\n## User Representation\n- [fact] f\n\n"
                                    "## User Peer Card\n- works on Windows\n\n"
                                    "## AI Self Representation\n- direct\n\n"
                                    "## AI Identity Card\n- terse\n\n"
                                    "## Value Markers\n- [tool] pytest")
    provider._store.put_layer_state("dialectic", "agent:minir",
                                    "## Adviser Review\n- run a smoke test\n\n"
                                    "## Constraints\n- never edit outside the repo\n\n"
                                    "## Preferences\n- act first\n\n## Second Brain\n- [tool] pytest")
    block = provider.prefetch("nothing matches this query")
    assert block.startswith("## Minir layer state")
    assert "### Agent model (v1)" in block
    assert "### User model" not in block
    assert "run a smoke test" in block
    assert "never edit outside the repo" not in block
    assert "works on Windows" not in block and "## User Peer Card" not in block
    status = provider.recall_status()
    assert status is not None and status.count == 1, "only the current review is injected"


def test_an_oversized_message_cannot_empty_or_own_the_budget(plugin_package):
    """One long paste must neither consume the whole budget nor leave an empty bucket (which the
    oldest-session eviction would then index)."""
    buffer = plugin_package.transcript.TranscriptBuffer(max_chars=25)
    buffer.add("s", "user", "x" * 5000)
    assert buffer.messages("s") == []  # it could not fit the budget at all
    buffer.add("s", "user", "short")
    assert [row["content"] for row in buffer.messages("s")] == ["short"]

    async_buffer = plugin_package.transcript.TranscriptBuffer(max_chars=9000)
    async_buffer.add("s", "user", "y" * 5000)
    assert len(async_buffer.messages("s")[0]["content"]) == 4001  # capped, ellipsis included
    async_buffer.add("s", "user", "later")  # must not raise
    assert len(async_buffer.messages("s")) == 2


def test_eviction_skips_a_bucket_a_huge_message_emptied(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer(max_chars=10, max_sessions=1)
    buffer.add("emptied", "user", "z" * 100)   # bigger than the whole budget -> empty bucket
    buffer.add("fresh", "user", "ok")          # evicting the empty one must not raise
    assert buffer.messages("emptied") == []
    assert [row["content"] for row in buffer.messages("fresh")] == ["ok"]


def test_sync_turn_survives_a_transcript_failure(plugin_package, make_provider, monkeypatch):
    """The one hook the host calls after every turn must not be able to raise into its worker."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})

    def explode(*args, **kwargs):
        raise RuntimeError("transcript exploded")

    monkeypatch.setattr(provider._transcript, "add_turn", explode)
    provider.sync_turn("deploy the plugin on Windows", "noted")  # must not raise
    provider.on_turn_start(1, "m")  # and the provider stays usable


def test_sync_turn_records_the_author_from_the_tick(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.on_turn_start(1, "m", author_id="u-42", author_name="Ada")
    provider.sync_turn("deploy the plugin on Windows", "noted")
    rows = provider._transcript.messages("session-test")
    assert [(row["author"], row["author_id"]) for row in rows] == [("Ada", "u-42"), ("", "")]


def test_sync_turn_prefers_the_host_supplied_author(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.on_turn_start(1, "m", author_id="stale", author_name="Stale")
    provider.sync_turn("deploy the plugin on Windows", "noted",
                       turn_author={"id": "u-7", "name": "Bo", "is_bot": False})
    assert provider._transcript.messages("session-test")[0]["author_id"] == "u-7"


def test_layer_reasoning_runs_end_to_end(plugin_package, make_provider, monkeypatch):
    """Reasoning enabled: a turn ends, both layers publish, and prefetch carries only the agent one."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, system, prompt, **kw: (
        _dialectic_json(text="run a smoke test")
        if task == plugin_package.worker.DIALECTIC_LAYER_TASK else
        _base_json()))

    provider = make_provider({"base_interval": 1, "dialectic_interval": 1}, agent_identity="Masía")
    provider.on_turn_start(1, "m")
    provider.sync_turn("deploy on Windows", "noted")
    provider._worker.stop()  # join the background drain before asserting

    assert provider._store.pending_jobs() == []
    assert [job["status"] for job in provider._store.claim_jobs(limit=9)] == []  # nothing left to do
    subjects = sorted(state["subject"] for state in provider._store.all_layer_states())
    assert subjects == ["agent:Masía", "user:default"]
    block = provider.prefetch("q")
    assert "run a smoke test" in block
    assert "wants evidence" not in block, "base cards are search-only"


def test_reasoning_disabled_never_calls_the_model(plugin_package, make_provider, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("reasoning is off: no model call may happen")

    monkeypatch.setattr(plugin_package.worker, "call_model", explode)
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    provider.sync_turn("deploy on Windows", "noted")
    provider.on_session_end([])
    assert len(provider._store.pending_jobs()) == 1  # the base job is queued, never drained
    assert provider._store.all_layer_states() == []


def test_register_wires_the_provider_and_its_hooks(plugin_package):
    class Ctx:
        def __init__(self):
            self.provider = None
            self.hooks = {}

        def register_memory_provider(self, provider):
            self.provider = provider

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = Ctx()
    plugin_package.register(ctx)
    assert isinstance(ctx.provider, plugin_package.MinirMemoryProvider)
    assert set(ctx.hooks) == {"on_session_start", "on_session_reset", "on_session_finalize",
                              "pre_llm_call", "post_llm_call", "post_tool_call",
                              "pre_tool_call", "on_interim_message", "post_api_request",
                              "api_request_error"}
    # A non-None result would be injected into the user message (pre_llm_call) or sent to
    # the chat (on_session_finalize), so every handler must return None.
    for callback in ctx.hooks.values():
        assert callback(session_id="s", old_session_id="o", reason="new_session", turn_id="t",
                        model="m", platform="cli", tool_name="terminal", duration_ms=3) is None


# -- cold start, review injection, metrics ------------------------------------

def test_cold_start_is_gated_to_the_primary_bound_session(
        plugin_package, make_provider, monkeypatch, tmp_path):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({"dialectic_depth": 1})
    provider._worker._enabled = False  # inspect the queue, do not race a background drain
    provider._identity = "I am Masía."  # the context that makes a cold start meaningful
    connection = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL);")
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("session-test", "user", "existing history", time.time()))
        connection.commit()
    finally:
        connection.close()

    plugin_package._on_session_start(session_id="child", provider=provider)
    assert provider._store.pending_jobs() == [], "a child session must not queue a model job"

    plugin_package._on_session_start(session_id="session-test", provider=provider)
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"], job["session_id"]) for job in jobs] == [
        ("base", 0, "session-test")]


def test_cold_start_rejects_a_non_primary_agent_context(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({}, agent_context="subagent")
    provider._worker._enabled = False
    plugin_package._on_session_start(session_id="session-test", provider=provider)
    assert provider._store.pending_jobs() == []


def test_review_is_injected_once_per_turn(plugin_package, make_provider):
    provider = make_provider({})
    provider._store.put_layer_state(
        "dialectic", "agent:minir",
        "## Adviser Review\n- ran the smoke test\n\n"
        "## Constraints\n- never edit outside the repo\n\n"
        "## Preferences\n- act first\n\n"
        "## Second Brain\n- [tool] pytest",
        session_id="session-test")
    first = plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-1", provider=provider)
    assert first and "[minir adviser review" in first and "ran the smoke test" in first
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-1", provider=provider) is None
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-2", provider=provider)
    assert plugin_package._on_pre_llm_call(
        session_id="other", turn_id="turn-3", provider=provider) is None
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-4", provider=provider,
        parent_session_id="parent") is None


def test_pending_advices_are_injected_once(plugin_package, provider):
    provider._store.add_advisories("session-test", [
        {"severity": "nit", "target": "general", "content": "polish the wording",
         "decision": "keep or trim", "evidence": ["m1"]},
        {"severity": "blocker", "target": "user_peer_card",
         "content": "verify the deploy target before deleting", "decision": "confirm the path",
         "evidence": ["m2"]},
    ])
    block = plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-1", provider=provider)
    assert block and "[minir adviser — advisory only]" in block
    assert "verify the deploy target" in block and "polish the wording" in block
    assert block.index("verify the deploy target") < block.index("polish the wording"), \
        "blockers are delivered before nits"
    assert provider._store.pending_advisories("session-test") == []
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-2", provider=provider) is None, \
        "an existing advice ledger suppresses the legacy review fallback"


def test_advice_injection_leaves_overflow_pending(plugin_package, provider):
    provider._store.add_advisories("session-test", [
        {"severity": "concern", "target": "general",
         "content": f"advice {index} " + "x" * 380, "decision": "d" * 180}
        for index in range(6)
    ])
    first = provider.advisory_block("session-test", "turn-1")
    assert first and len(first) <= 900
    pending = provider._store.pending_advisories("session-test")
    assert 0 < len(pending) < 6, "the budget must defer the overflow, not drop it"
    delivered = first.count("- [")
    assert delivered == 6 - len(pending), "only the lines actually injected may be marked"

    second = provider.advisory_block("session-test", "turn-2")
    assert second and len(second) <= 900
    assert len(provider._store.pending_advisories("session-test")) < len(pending)

    for turn in range(3, 10):
        if not provider.advisory_block("session-test", f"turn-{turn}"):
            break
    assert provider._store.pending_advisories("session-test") == [], \
        "draining repeatedly must eventually deliver every advice"


def test_post_tool_call_aggregates_metrics(plugin_package, make_provider):
    provider = make_provider({})
    for duration in (10, 30):
        plugin_package._on_post_tool_call(
            tool_name="terminal", session_id="session-test", duration_ms=duration, provider=provider)
    plugin_package._on_post_tool_call(
        tool_name="terminal", session_id="session-test", duration_ms=5, provider=provider,
        error=True)
    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert stats["tool_metrics"]["terminal"] == {
        "count": 3, "total_ms": 45.0, "max_ms": 30.0, "errors": 1}
    provider.flush_metrics()
    assert provider._store.stats()["metrics"] == 1


def test_layers_reason_from_host_history_and_publish_searchable_sections(
        plugin_package, make_provider, tmp_path, monkeypatch):
    """The restart-durable path: a job reads state.db, not the in-process buffer."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, system, prompt, **kw: (
        _dialectic_json(text="ran the deploy smoke test")
        if task == plugin_package.worker.DIALECTIC_LAYER_TASK else
        _base_json(summary=["kubernetes deploy"], user=["[goal] deploy the cluster"],
                   peer=["wants evidence"], ai_self=["direct"],
                   identity=["careful with user files"])))
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    state_path = tmp_path / "state.db"
    connection = sqlite3.connect(str(state_path))
    try:
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, message_uid TEXT,"
            " role TEXT, content TEXT, timestamp REAL, active INTEGER DEFAULT 1,"
            " _compressed_summary INTEGER DEFAULT 0);")
        stamp = time.time()
        connection.executemany(
            "INSERT INTO messages (session_id, message_uid, role, content, timestamp)"
            " VALUES (?, ?, ?, ?, ?)",
            [("session-test", "u1", "user", "deploy the kubernetes cluster", stamp),
             ("session-test", "a1", "assistant", "running the deploy smoke test", stamp + 0.001),
             ("session-test", "u2", "user", "[Assistant Rules] not user intent", stamp + 0.002)])
        connection.commit()
    finally:
        connection.close()

    provider.on_turn_start(1, "m")
    provider.on_session_end([])  # drain the boundary without adding buffer content
    provider._worker.stop()

    assert provider._store.layer_state("base", "user:default") is not None
    assert provider._store.layer_state("dialectic", "agent:minir") is not None
    hits = provider._store.search("kubernetes deploy", session_id="session-test")
    assert hits and any(row["kind"] == "base.user_representation" for row in hits)
    assert not any("not user intent" in row["text"] for row in hits)
    review = provider.review_block("session-test")
    assert "deploy smoke test" in review


def test_search_never_exposes_a_conclusion_as_a_forgettable_note(plugin_package, provider):
    provider._store.put_layer_state(
        "base", "user:default",
        "## Session Summary\n- deploy\n\n## User Representation\n- [goal] deploy\n\n"
        "## User Peer Card\n- terse\n\n## AI Self Representation\n- direct\n\n"
        "## AI Identity Card\n- careful\n\n## Value Markers\n- [tool] pytest",
        session_id="session-test")
    payload = json.loads(provider.handle_tool_call("minir_search", {"query": "deploy"}))
    conclusion = next(row for row in payload["results"] if row["kind"].startswith("base."))
    assert "id" not in conclusion and "note_id" not in conclusion
    assert "conclusion_id" not in conclusion

    note_id = json.loads(provider.handle_tool_call(
        "minir_remember", {"content": "a forgettable note"}))["note_id"]
    found = json.loads(provider.handle_tool_call("minir_search", {"query": "forgettable"}))
    note = next(row for row in found["results"] if row["kind"] == "note")
    assert note["note_id"] == note_id and note["id"] == note_id


def test_boundary_queues_a_state_db_tail_after_the_buffer_was_lost(
        plugin_package, make_provider, tmp_path):
    provider = make_provider({})
    provider._transcript.clear()
    connection = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL);")
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("session-test", "user", "history-only tail", time.time()))
        connection.commit()
    finally:
        connection.close()

    provider.on_session_end([])
    jobs = provider._store.pending_jobs()
    assert {(job["layer"], job["session_id"]) for job in jobs} == {
        ("base", "session-test")}


def test_prefetch_does_not_inject_another_sessions_review_or_summary(provider):
    provider._store.put_layer_state(
        "base", "user:default",
        "## Session Summary\n- old session secret\n\n## User Representation\n- [goal] old goal\n\n"
        "## User Peer Card\n- durable trait\n\n## AI Self Representation\n- old stance\n\n"
        "## AI Identity Card\n- durable identity\n\n## Value Markers\n- [tool] durable tool",
        session_id="other-session")
    provider._store.put_layer_state(
        "dialectic", "agent:minir",
        "## Adviser Review\n- old advice\n\n## Constraints\n- durable constraint\n\n"
        "## Preferences\n- durable preference\n\n## Second Brain\n- [project] durable project",
        session_id="other-session")
    block = provider.prefetch("q")
    assert block == "", "another session's review and base cards are not injectable"
    assert "durable constraint" not in block and "durable project" not in block


def test_prefetch_never_injects_durable_base_cards_from_fts(provider):
    provider._store.put_conclusion("base.user_peer_card", "user:default",
                                   "durable peer card secret", subject="user:default")
    provider._store.put_conclusion("base.ai_identity_card", "agent:minir",
                                   "durable identity card secret", subject="agent:minir")
    provider._store.remember("durable peer card note")

    block = provider.prefetch("durable peer card secret")
    assert "durable peer card note" in block
    assert "durable peer card secret" not in block
    assert "durable identity card secret" not in block

    provider.queue_prefetch("durable peer card secret")
    thread = provider._prefetch_thread
    if thread is not None:
        thread.join(timeout=5.0)
    cached = provider.prefetch("durable peer card secret")
    assert "durable peer card note" in cached
    assert "durable peer card secret" not in cached
    assert "durable identity card secret" not in cached

    payload = json.loads(provider.handle_tool_call(
        "minir_search", {"query": "durable peer card secret"}))
    kinds = {row["kind"] for row in payload["results"]}
    assert "base.user_peer_card" in kinds and "base.ai_identity_card" in kinds, \
        "explicit minir_search still exposes the durable cards"


def test_prefetch_does_not_inject_any_base_artifact(provider):
    provider._store.put_layer_state(
        "base", "user:default",
        "## Session Summary\n- OLD PROJECTION\n\n## User Representation\n- [goal] current\n\n"
        "## User Peer Card\n- durable preference\n\n## AI Self Representation\n- direct\n\n"
        "## AI Identity Card\n- careful\n\n## Value Markers\n- [tool] pytest")
    block = provider.prefetch("q")
    assert block == ""
    assert "durable preference" not in block
    assert "OLD PROJECTION" not in block
    assert "## Session Summary" not in block
