"""Compression engine — trigger, audited summary message, notice, and recall boundaries."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

SESSION = "session-ctx"

HANDOFF = """# Task Handoff
## Goal
- ship the context engine
## Current State
- compression is synchronous
## Next Steps
- verify the summary
## Critical Context
- no secrets"""


def _msg(role, text, uid=""):
    message = {"role": role, "content": text}
    if uid:
        message["message_uid"] = uid
    return message


def _conversation(turns: int) -> list[dict]:
    messages = [_msg("system", "You are a test agent.")]
    for index in range(1, turns + 1):
        messages.append(_msg("user", f"deploy step {index} about kubernetes", uid=f"u{index:03d}"))
        messages.append(_msg("assistant", f"ran deploy step {index}", uid=f"a{index:03d}"))
    return messages


@pytest.fixture()
def state_db(tmp_path):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, message_uid TEXT,"
        " role TEXT, content TEXT, timestamp INTEGER);")
    connection.commit()
    connection.close()
    return path


def _engine(plugin_package, *, hermes_home="", state_db=""):
    engine = plugin_package.MinirContextEngine(hermes_home=str(hermes_home),
                                               state_db=str(state_db))
    engine.on_session_start(SESSION)
    return engine


# -- contract -----------------------------------------------------------------

def test_the_class_is_a_context_engine_subclass(plugin_package):
    engine_type = plugin_package.context_engine.ContextEngine
    assert issubclass(plugin_package.MinirContextEngine, engine_type)
    engine = plugin_package.MinirContextEngine()
    assert engine.name == "minir"
    for attribute in ("last_prompt_tokens", "last_completion_tokens", "last_total_tokens",
                      "threshold_tokens", "context_length", "compression_count",
                      "protect_first_n", "protect_last_n"):
        assert hasattr(engine, attribute), attribute
    assert engine.emit_automatic_compaction_status is False


def test_register_never_claims_the_engine_slot(plugin_package):
    class ProviderCtx:
        def __init__(self):
            self.provider, self.hooks, self.engines = None, {}, []

        def register_memory_provider(self, provider):
            self.provider = provider

        def register_hook(self, name, callback):
            self.hooks[name] = callback

        def register_context_engine(self, engine):
            self.engines.append(engine)

    ctx = ProviderCtx()
    plugin_package.register(ctx)
    assert ctx.provider is not None and ctx.hooks
    assert ctx.engines == [], "a memory-provider load must not register a context engine"


def test_update_from_response_tracks_usage(plugin_package):
    engine = plugin_package.MinirContextEngine()
    engine.update_model("m", 200_000)
    engine.update_from_response({"prompt_tokens": 1_500, "completion_tokens": 40,
                                 "total_tokens": 1_540})
    assert (engine.last_prompt_tokens, engine.last_completion_tokens, engine.last_total_tokens) \
        == (1500, 40, 1540)
    assert engine.threshold_tokens == int(200_000 * 0.75)
    assert engine.get_status()["engine"] == "minir"


def test_clone_for_agent_shares_config_and_no_state(plugin_package):
    engine = _engine(plugin_package)
    engine.compression_count = 3
    clone = engine.clone_for_agent()
    assert clone.name == "minir"
    assert clone.compression_count == 0
    assert clone._session_id == ""


# -- trigger ------------------------------------------------------------------

def test_the_only_trigger_is_95_percent_of_the_threshold(plugin_package):
    engine = plugin_package.MinirContextEngine()
    engine.threshold_tokens = 100
    engine.last_prompt_tokens = 94
    assert engine.should_compress() is False
    engine.last_prompt_tokens = 95
    assert engine.should_compress() is True
    engine.threshold_tokens = 0
    assert engine.should_compress(10_000) is False, "unknown context length never triggers"


def test_preflight_defers_while_the_last_request_fit(plugin_package):
    engine = plugin_package.MinirContextEngine()
    engine.threshold_tokens = 100
    engine.last_prompt_tokens = 50
    assert engine.should_defer_preflight_to_real_usage(90) is True
    engine.last_prompt_tokens = 100
    assert engine.should_defer_preflight_to_real_usage(100) is False


# -- slice --------------------------------------------------------------------

def test_foldable_slice_protects_head_tail_and_the_last_user_turn(plugin_package):
    slice_fn = plugin_package.context_engine.foldable_slice
    messages = _conversation(12)
    start, end = slice_fn(messages, 3, 6)
    assert start >= 4 and end <= len(messages) - 6
    assert messages[end]["role"] == "user", "the retained tail opens on a user turn"
    assert messages[-1]["role"] == "assistant"


def test_foldable_slice_honours_a_focus_topic(plugin_package):
    slice_fn = plugin_package.context_engine.foldable_slice
    messages = [
        _msg("system", "s"),
        _msg("user", "keep kubernetes details here"),
        _msg("assistant", "a"),
        _msg("user", "drop this"),
        _msg("assistant", "b"),
        _msg("user", "keep going"),
        _msg("assistant", "c"),
        _msg("user", "latest"),
    ]
    bounds = slice_fn(messages, 1, 2, focus_topic="kubernetes")
    assert bounds is not None and bounds[1] < len(messages) - 2


def test_has_content_to_compress_reports_honestly(plugin_package):
    engine = plugin_package.MinirContextEngine()
    assert engine.has_content_to_compress(_conversation(10)) is True
    assert engine.has_content_to_compress(_conversation(1)) is False


# -- compression --------------------------------------------------------------

def _compression_env(monkeypatch, plugin_package):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.handoff, "summarize_agent",
                        lambda *a, **k: HANDOFF)


def test_compress_returns_head_notice_assistant_summary_and_tail(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    original = _conversation(12)
    result = engine.compress(original)

    assert result[0]["role"] == "system"
    notices = [m for m in result
                if plugin_package.context_engine.NOTICE_MARKER in m["content"]]
    assert len(notices) == 1 and notices[0]["role"] == "system"
    summaries = [m for m in result
                 if plugin_package.context_engine.SUMMARY_MARKER in m["content"]]
    assert len(summaries) == 1 and summaries[0]["role"] == "assistant"
    assert HANDOFF in summaries[0]["content"]
    assert result[-1] == original[-1], "the tail is unmodified"
    assert "recover:" not in summaries[0]["content"], "no folded uid list"


def test_compress_writes_a_pending_summary_and_promotes_it_on_observation(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    result = engine.compress(_conversation(12))
    store = engine._memory_store()
    pending = store.newest_conclusion("session_summary", session_id=SESSION)
    assert pending is None, "a pending summary is not recallable before its marker is observed"

    engine.on_turn_complete(result)
    active = store.newest_conclusion("session_summary", session_id=SESSION)
    assert active is not None and active["status"] == "active"
    assert HANDOFF in active["content"]


def test_the_notice_is_added_only_once(plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    once = engine.compress(_conversation(20))
    twice = engine.compress(once)
    assert sum(1 for m in twice
               if plugin_package.context_engine.NOTICE_MARKER in m["content"]) == 1


def test_a_second_compression_keeps_exactly_one_summary(
        plugin_package, tmp_path, state_db, monkeypatch):
    seen: list[str] = []
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.handoff, "summarize_agent",
                        lambda chunk, review, **kwargs: (
                            seen.append(str(kwargs.get("previous") or "")) or HANDOFF))
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    once = engine.compress(_conversation(40))
    twice = engine.compress(once)
    assert "ship the context engine" in seen[-1], \
        "the previous assistant summary must feed the next summary agent"
    assert twice != once, "the second compression must actually fold"
    assert sum(1 for m in twice
               if plugin_package.context_engine.SUMMARY_MARKER in m["content"]) == 1, \
        "the previous summary must be replaced, not accumulated in the head"
    assert sum(1 for m in twice
               if plugin_package.context_engine.NOTICE_MARKER in m["content"]) == 1


def test_summary_agent_failure_falls_back_to_the_prepared_chunk(
        plugin_package, tmp_path, state_db, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.handoff, "summarize_agent", lambda *a, **k: "")
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    store = engine._memory_store()
    store.add_chunk_summary(SESSION, 1.0, 2.0, "prepared chunk fact")
    result = engine.compress(_conversation(12))
    summary = next(m for m in result
                   if plugin_package.context_engine.SUMMARY_MARKER in m["content"])
    assert "prepared chunk fact" in summary["content"]


def test_reasoning_disabled_skips_the_summary_agent(
        plugin_package, tmp_path, state_db, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "false")
    monkeypatch.setattr(plugin_package.handoff, "summarize_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent called")))
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    result = engine.compress(_conversation(12))
    assert any(plugin_package.context_engine.SUMMARY_MARKER in m["content"] for m in result)


def test_compress_clears_the_compression_gate(plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    store = engine._memory_store()
    store.set_compression_gate(SESSION, expires_at=time.time() + 60)
    engine.compress(_conversation(12))
    assert store.compression_gate_active(SESSION) is False


def test_summary_agent_prompt_bounds_history_and_includes_flags(plugin_package, monkeypatch):
    captured: dict[str, str] = {}

    class Result:
        answer = HANDOFF

    monkeypatch.setattr(plugin_package.adviser, "load_config",
                        lambda: {"model": "", "provider": "", "toolsets": []})
    monkeypatch.setattr(plugin_package.agent_loop, "run",
                        lambda prompt, **kwargs: captured.update(prompt=prompt, **kwargs) or Result())

    result = plugin_package.handoff.summarize_agent(
        "prepared chunk", "prepared review", session_id=SESSION, before=123.0, timeout=5)
    assert result == HANDOFF
    prompt = captured["prompt"]
    assert "before=123.000000" in prompt
    assert "include_inactive=True" in prompt and "include_compacted=True" in prompt
    assert "prepared chunk" in prompt and "prepared review" in prompt
    assert plugin_package.adviser.session_search.TOOLSET in captured["toolsets"]
    assert "TEMPLATE:" in captured["system_message"] and "Task Handoff" in captured["system_message"], \
        "MINIR_SUMMARY_TEMPLATE / prompts/session_handoff.md must reach the agent"


def test_compress_fails_open(plugin_package):
    engine = _engine(plugin_package)
    assert engine.compress("not a message list") == "not a message list"
    assert engine.compress([]) == []
    assert engine.compress([_msg("user", "only one")]) == [_msg("user", "only one")]


def test_prune_tool_results_only_elides_the_old_payloads(plugin_package):
    engine = _engine(plugin_package)
    engine.protect_last_n = 4
    messages = [
        _msg("system", "s"),
        _msg("user", "u1"),
        {"role": "tool", "content": "x" * 5_000},
        _msg("assistant", "a1"),
        _msg("user", "u2"),
        {"role": "tool", "content": "keep me"},
        _msg("assistant", "a2"),
        _msg("user", "latest"),
    ]
    pruned, count = engine.prune_tool_results_only(messages)
    assert count == 1
    assert pruned[2]["content"] == plugin_package.context_engine._ELIDED_TOOL_MARKER
    assert pruned[5]["content"] == "keep me"


def test_session_reset_clears_state_and_gate(plugin_package, tmp_path, state_db):
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    store = engine._memory_store()
    store.set_compression_gate(SESSION, expires_at=time.time() + 60)
    engine.compression_count = 2
    engine.on_session_reset()
    assert engine._session_id == "" and engine.compression_count == 0
    assert store.compression_gate_active(SESSION) is False
