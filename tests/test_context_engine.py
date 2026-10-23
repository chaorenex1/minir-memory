"""Compression engine — trigger, audited summary message, notice, and recall boundaries."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

SESSION = "session-ctx"

WINDOW_SUMMARY = """## Window Summary
### Objective
- ship the context engine
### Current State
#### Done
- compression is synchronous
#### Doing
- verify the summary
#### Todo
### Decisions
### Constraints
### Verified Facts
### Open Issues
### Next Actions
### References"""


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

def _compression_env(monkeypatch, plugin_package, body=WINDOW_SUMMARY):
    """Stub the compression-time B3 call; returns the kwargs it was given."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    seen: list[dict] = []

    def fake_summarize(**kwargs):
        seen.append(dict(kwargs))
        return body

    monkeypatch.setattr(plugin_package.compress_pass, "summarize", fake_summarize)
    return seen


def _seed_base(store, summary=WINDOW_SUMMARY, to_ts=1.0):
    """One published base window: the calibration baseline compression reads."""
    store.add_base_refresh(
        SESSION, "user:default", window_from_ts=0.0, window_to_ts=to_ts,
        window_from_turn=0, window_to_turn=1,
        user_representation=json.dumps("## Self\n\n- Occupation: engineer"),
        user_peer_card=json.dumps("prefers terse answers"),
        ai_self_representation=json.dumps("## AI\n\n- local memory plugin"),
        ai_identity_card=json.dumps("steady"),
        window_summary=json.dumps(summary))


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
    assert WINDOW_SUMMARY in summaries[0]["content"]
    assert result[-1] == original[-1], "the tail is unmodified"
    assert "recover:" not in summaries[0]["content"], "no folded uid list"


def test_compress_writes_an_active_summary_indexed_per_session(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    result = engine.compress(_conversation(12))
    store = engine._memory_store()
    first = store.get_conclusion("session_summary", "session_summary:1")
    assert first is not None, "the summary is active from the start, with no pending phase"
    assert first["status"] == "active" and first["session_id"] == SESSION
    assert WINDOW_SUMMARY in first["content"]

    # The marker still releases the compression gate; nothing promotes the row any more.
    engine.on_turn_complete(result)
    assert store.compression_gate_active(SESSION) is False

    engine.compress(result)
    second = store.get_conclusion("session_summary", "session_summary:2")
    assert second is not None and second["status"] == "active"


def test_the_notice_is_added_only_once(plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    once = engine.compress(_conversation(20))
    twice = engine.compress(once)
    assert sum(1 for m in twice
               if plugin_package.context_engine.NOTICE_MARKER in m["content"]) == 1


def test_a_second_compression_keeps_exactly_one_summary(
        plugin_package, tmp_path, state_db, monkeypatch):
    seen = _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    _seed_base(engine._memory_store(), to_ts=100.0)
    once = engine.compress(_conversation(40))
    twice = engine.compress(once)
    assert seen[-1]["previous"] == WINDOW_SUMMARY, \
        "the base window summary is the calibration baseline"
    assert seen[-1]["from_ts"] == 100.0, "the window starts where the base window ended"
    assert twice != once, "the second compression must actually fold"
    assert sum(1 for m in twice
               if plugin_package.context_engine.SUMMARY_MARKER in m["content"]) == 1, \
        "the previous summary must be replaced, not accumulated in the head"
    assert sum(1 for m in twice
               if plugin_package.context_engine.NOTICE_MARKER in m["content"]) == 1


def test_a_failed_calibration_folds_the_previous_window_summary(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package, body="")
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    _seed_base(engine._memory_store(), summary="PREVIOUS BASE FACT", to_ts=1.0)
    result = engine.compress(_conversation(12))
    summary = next(m for m in result
                   if plugin_package.context_engine.SUMMARY_MARKER in m["content"])
    assert "PREVIOUS BASE FACT" in summary["content"]


def test_a_failed_calibration_without_a_base_window_folds_the_empty_template(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package, body="")
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    result = engine.compress(_conversation(12))
    summary = next(m for m in result
                   if plugin_package.context_engine.SUMMARY_MARKER in m["content"])
    assert "## Window Summary" in summary["content"]
    assert any(plugin_package.context_engine.NOTICE_MARKER in m["content"] for m in result)


def test_reasoning_disabled_keeps_the_fold_model_free(
        plugin_package, tmp_path, state_db, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "false")
    monkeypatch.setattr(plugin_package.compress_pass, "summarize",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("model called")))
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    _seed_base(engine._memory_store(), summary="BASE ONLY", to_ts=1.0)

    result = engine.compress(_conversation(12))

    summary = next(m for m in result
                   if plugin_package.context_engine.SUMMARY_MARKER in m["content"])
    assert "BASE ONLY" in summary["content"]


def test_compression_does_not_read_the_chunk_chain(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package, body="CALIBRATED BODY")
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    store = engine._memory_store()
    assert store.chunk_summaries(SESSION) == []
    result = engine.compress(_conversation(12))
    summary = next(m for m in result
                   if plugin_package.context_engine.SUMMARY_MARKER in m["content"])
    assert "CALIBRATED BODY" in summary["content"]


def test_a_reply_missing_a_section_is_still_folded(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package, body="### Objective\n- only one section")
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    result = engine.compress(_conversation(12))
    summary = next(m for m in result
                   if plugin_package.context_engine.SUMMARY_MARKER in m["content"])
    assert "only one section" in summary["content"]


def test_the_fold_is_stored_as_this_sessions_compression_record(
        plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package, body="STORED BODY")
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    result = engine.compress(_conversation(12))
    summary = next(m for m in result
                   if plugin_package.context_engine.SUMMARY_MARKER in m["content"])
    row = engine._memory_store().latest_compression_summary(SESSION)
    assert row is not None and row["status"] == "active"
    assert row["key"] == "session_summary:1"
    assert summary["content"].endswith("STORED BODY")


def test_compress_clears_the_compression_gate(plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    store = engine._memory_store()
    store.set_compression_gate(SESSION, expires_at=time.time() + 60)
    engine.compress(_conversation(12))
    assert store.compression_gate_active(SESSION) is False


def test_the_compression_pass_runs_b3_with_the_window_template(plugin_package, monkeypatch):
    captured: dict = {}

    class Result:
        answer = "### Objective\n- ship"

    monkeypatch.setattr(plugin_package.agent_loop, "run",
                        lambda prompt, **kwargs: captured.update(prompt=prompt, **kwargs)
                        or Result())

    text = plugin_package.compress_pass.summarize(
        session_id=SESSION, previous="OLD SUMMARY", from_ts=10.0, to_ts=20.0)

    assert text == "### Objective\n- ship"
    assert captured["layer"] == "window_summary"
    assert "toolsets" not in captured, "the calibration pass gets no tools"
    system = captured["system_message"]
    assert "You close one base refresh" in system, "B3's own core must lead the prompt"
    assert "TEMPLATE:" in system and "## Window Summary" in system
    assert "10.000000 to 20.000000" in captured["prompt"]
    assert "OLD SUMMARY" in captured["prompt"]


def test_the_engine_finds_the_host_store_from_hermes_home(
        plugin_package, tmp_path, monkeypatch):
    """The loader may pass only hermes_home; the window transcript still has a source."""
    connection = sqlite3.connect(str(tmp_path / "state.db"))
    connection.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT, timestamp INTEGER);")
    connection.commit()
    connection.close()
    seen: dict = {}
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.compress_pass, "summarize",
                        lambda **kwargs: seen.update(kwargs) or "BODY")
    engine = _engine(plugin_package, hermes_home=tmp_path)
    _seed_base(engine._memory_store(), to_ts=1.0)

    engine.compress(_conversation(12))

    assert seen["state_db"] == str(tmp_path / "state.db")


def test_the_window_digest_starts_at_the_base_window_edge(plugin_package, state_db):
    connection = sqlite3.connect(str(state_db))
    connection.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [(SESSION, "user", "before the window", 5),
         (SESSION, "user", "inside the window", 15),
         (SESSION, "assistant", "after the window", 25)])
    connection.commit()
    connection.close()

    text = plugin_package.compress_pass.transcript_text(state_db, SESSION, 10.0, 20.0)

    assert "inside the window" in text
    assert "before the window" not in text and "after the window" not in text
    assert "[user]" in text


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


def test_compress_never_rewrites_the_base_window(plugin_package, tmp_path, state_db, monkeypatch):
    _compression_env(monkeypatch, plugin_package)
    engine = _engine(plugin_package, hermes_home=tmp_path, state_db=state_db)
    store = engine._memory_store()
    store.add_base_refresh(
        SESSION, "user:default", window_from_ts=0.0, window_to_ts=1.0,
        window_from_turn=1, window_to_turn=1, user_representation="", user_peer_card="",
        ai_self_representation=json.dumps("ai"), ai_identity_card="",
        window_summary=json.dumps("### Objective\n- keep me"))
    before = store.latest_base_refresh(SESSION)
    engine.compress(_conversation(12))
    after = store.latest_base_refresh(SESSION)
    assert (after["id"], after["window_summary"], after["is_latest"]) == \
        (before["id"], before["window_summary"], before["is_latest"])
    assert store.newest_conclusion("session_summary", session_id=SESSION) is not None
