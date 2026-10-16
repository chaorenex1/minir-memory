"""Provider behavior — lifecycle, recall, persistence, counting, and tools."""

from __future__ import annotations

import json
import sys
import types


def test_initialize_creates_profile_scoped_store(provider, tmp_path):
    assert (tmp_path / "minir-memory" / "memory.db").exists()


def test_sync_turn_persists_and_new_instance_recalls(plugin_package, make_provider):
    first = make_provider({"capture_turns": True})
    first.sync_turn("We standardized on SQLite for the memory store", "Good choice.")
    first.shutdown()

    second = make_provider({"capture_turns": True})  # same tmp home -> same db file
    block = second.prefetch("SQLite memory store")
    assert "SQLite" in block
    assert block.startswith("## Minir recall")


def test_sync_turn_records_turn_author(provider):
    provider.sync_turn("kv cache tuning preferences", "Noted.",
                       turn_author={"id": "u1", "name": "alice", "is_bot": False})
    rows = provider._store.search("kv cache tuning")
    assert rows and rows[0]["author"] == "alice"


def test_sync_turn_skipped_when_capture_disabled(make_provider):
    provider = make_provider({"capture_turns": False})
    provider.sync_turn("something important about staging deploys", "ok")
    assert provider._store.stats()["turns"] == 0


def test_sync_turn_skipped_for_non_primary_context(make_provider):
    """Host contract: subagent/cron/flush contexts must not write memory."""
    provider = make_provider({"capture_turns": True}, agent_context="subagent")
    provider.sync_turn("subagent scratch work that should not persist", "ok")
    assert provider._store.stats()["turns"] == 0


def test_sync_turn_skips_trivial_prompts(provider):
    provider.sync_turn("thanks!", "You're welcome.")
    assert provider._store.stats()["turns"] == 0


def test_capture_can_be_enabled_by_env(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_CAPTURE_TURNS", "false")
    provider = make_provider({"capture_turns": True})  # env wins over config
    provider.sync_turn("env precedence check for capture", "ok")
    assert provider._store.stats()["turns"] == 0


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
    provider = make_provider({"capture_turns": True, "prefetch_limit": 20})
    for index in range(10):
        provider._store.remember(f"bounded note {index} " + "y" * 400)
    block = provider.prefetch("bounded")
    assert "more results truncated" in block
    assert len(block) < 2600


# -- turn counting (base / dialectic layers) ----------------------------------

def _jobs(provider):
    """The queued layer jobs as ``(layer, turns)`` pairs, oldest first."""
    return [(job["layer"], job["turns"]) for job in provider._store.pending_jobs()]


def test_turn_start_queues_layer_jobs(make_provider):
    provider = make_provider({"base_interval": 3, "dialectic_interval": 2})
    for turn in range(1, 7):
        provider.on_turn_start(turn, f"message {turn}")
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"], job["session_id"]) for job in jobs] == [
        ("dialectic", 2, "session-test"), ("base", 3, "session-test"),
        ("dialectic", 4, "session-test"), ("base", 6, "session-test"),
        ("dialectic", 6, "session-test")]
    assert all(job["status"] == "pending" and job["from_ts"] <= job["to_ts"] for job in jobs)


def test_new_session_ordering_keeps_the_tail(plugin_package, make_provider):
    """The host's ``/new`` order: ``on_session_finalize`` (old id) stops the session
    synchronously, then the queued ``on_session_end`` (old id) flushes the tail. The stop
    must not swallow it — and the later ``on_session_reset`` must not duplicate it."""
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")                    # dialectic job at turn 3
    plugin_package._on_session_finalize(session_id="session-test")   # frozen first
    provider.on_session_end([])                              # ... tail arrives after
    plugin_package._on_session_reset(session_id="session-next", old_session_id="session-test")
    assert _jobs(provider) == [("dialectic", 3), ("dialectic", 4)]


def test_session_end_flushes_the_tail_once(make_provider):
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider.on_session_end([])
    provider.on_session_end([])  # a second boundary hook must not re-queue the window
    assert _jobs(provider) == [("dialectic", 3), ("dialectic", 4)]


def test_session_end_on_a_multiple_queues_no_extra_job(make_provider):
    """The spec's "no boundary trigger when the count sits on a multiple" case."""
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider.on_session_end([])
    assert _jobs(provider) == [("dialectic", 3)]


def test_pre_compress_flushes_tail_without_ending_the_session(make_provider):
    provider = make_provider({"base_interval": 50, "dialectic_interval": 2})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")               # dialectic job at turn 2
    assert provider.on_pre_compress([]) == ""
    assert _jobs(provider) == [("dialectic", 2), ("dialectic", 3)]  # turn 3 flushed by the boundary
    for turn in (4, 5):
        provider.on_turn_start(turn, "m")               # counting continues after compression
    assert _jobs(provider) == [("dialectic", 2), ("dialectic", 3), ("dialectic", 5)]


def test_session_reset_flushes_old_tail_and_opens_new_session(plugin_package, make_provider):
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3, "session_mode": "coding"})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider.on_session_switch("session-next", parent_session_id="session-test", reset=True)
    # The plugin hook fires after the provider switch for the same boundary: still one job.
    plugin_package._on_session_reset(session_id="session-next", old_session_id="session-test")
    assert _jobs(provider) == [("dialectic", 3), ("dialectic", 4)]
    state = plugin_package.turn_counter.TRACKER.snapshot("session-next")
    assert (state["mode"], state["layers"]["base"]["turns"]) == ("coding", 0)
    assert plugin_package.turn_counter.TRACKER.tick("session-test", 5, now=1.0) == []


def test_branch_inherits_parent_counters(make_provider):
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")           # dialectic job at turn 3
    provider.on_session_switch("branch-1", parent_session_id="session-test", reset=False)
    assert provider._session_id == "branch-1"
    for turn in (5, 6):
        provider.on_turn_start(turn, "m")
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"]) for job in jobs] == [("dialectic", 3), ("dialectic", 6)]
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
    assert _jobs(provider) == [("dialectic", 1), ("base", 2), ("dialectic", 2)]


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
        for turn in (1, 2):
            provider.on_turn_start(turn, "m")  # dialectic interval 2; base needs 4
        assert _jobs(provider) == [("dialectic", 2)]
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
    for turn in range(1, 11):
        provider.on_turn_start(turn, "m")
    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert (stats["jobs"], stats["pending_jobs"]) == (1, 1)
    assert stats["layers"]["layers"]["dialectic"]["turns"] == 10
    assert stats["layers"]["layers"]["base"]["interval"] == 20


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
                              "pre_llm_call", "post_llm_call", "post_tool_call"}
    # A non-None result would be injected into the user message (pre_llm_call) or sent to
    # the chat (on_session_finalize), so every handler must return None.
    for callback in ctx.hooks.values():
        assert callback(session_id="s", old_session_id="o", reason="new_session", turn_id="t",
                        model="m", platform="cli", tool_name="terminal", duration_ms=3) is None
