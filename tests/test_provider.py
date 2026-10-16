"""Provider behavior — lifecycle, recall, persistence, and tools."""

from __future__ import annotations

import json


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
