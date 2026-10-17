"""Progressive lossless context management — folding, carriers, cadence, and recall."""

from __future__ import annotations

import sqlite3
import time

import pytest

SESSION = "session-ctx"


def _msg(role, text, uid=""):
    message = {"role": role, "content": text}
    if uid:
        message["message_uid"] = uid
    return message


def _conversation(turns: int) -> list[dict]:
    """A system prompt plus *turns* two-message exchanges, each with a stable uid."""
    messages = [_msg("system", "You are a test agent.")]
    for index in range(1, turns + 1):
        messages.append(_msg("user", f"deploy step {index} about kubernetes", uid=f"u{index:03d}"))
        messages.append(_msg("assistant", f"ran deploy step {index}", uid=f"a{index:03d}"))
    return messages


@pytest.fixture()
def state_db(plugin_package, tmp_path):
    """A stand-in for the host's ``state.db``: the lossless backing store the engine reads."""
    path = tmp_path / "state.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, message_uid TEXT,"
        " role TEXT, content TEXT, timestamp INTEGER);")
    connection.commit()
    connection.close()
    return path


def _store_messages(state_db, messages, session_id=SESSION):
    connection = sqlite3.connect(str(state_db))
    for index, message in enumerate(messages):
        connection.execute(
            "INSERT INTO messages (session_id, message_uid, role, content, timestamp) VALUES (?,?,?,?,?)",
            (session_id, message.get("message_uid") or "", message["role"], message["content"],
             int(time.time()) + index))
    connection.commit()
    connection.close()


def _engine(plugin_package, state_db, *, interval=10, hermes_home=""):
    engine = plugin_package.MinirContextEngine(state_db=str(state_db), interval=interval,
                                               hermes_home=hermes_home)
    engine.on_session_start(SESSION)
    return engine


# -- contract -----------------------------------------------------------------

def test_the_class_is_a_context_engine_subclass(plugin_package):
    """The host resolves an engine by ``issubclass(ContextEngine)`` and proxies it by ``base_ref``,
    so inheritance is part of the contract — not a detail."""
    engine_type = plugin_package.context_engine.ContextEngine
    assert issubclass(plugin_package.MinirContextEngine, engine_type)
    assert isinstance(plugin_package.MinirContextEngine(), engine_type)

    engine = plugin_package.MinirContextEngine()
    assert engine.name == "minir", "the name must equal the installed directory name"
    for attribute in ("last_prompt_tokens", "last_completion_tokens", "last_total_tokens",
                      "threshold_tokens", "context_length", "compression_count",
                      "protect_first_n", "protect_last_n"):
        assert hasattr(engine, attribute), attribute
    assert engine.emit_automatic_compaction_status is False, "a fold is routine maintenance"


def test_register_never_claims_the_engine_slot(plugin_package):
    """One directory serves two surfaces, but each loader calls ``register()``.

    A real ``PluginContext`` would register an engine if ``register()`` asked it to, so a provider
    install would silently take the single, user-chosen context-engine slot. The engine is picked
    up by the engine loader's subclass scan instead, which is why this must stay empty.
    """
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


def test_update_from_response_tracks_usage(plugin_package, state_db):
    engine = _engine(plugin_package, state_db)
    engine.update_model("m", 200_000)
    engine.update_from_response({"prompt_tokens": 1_500, "completion_tokens": 40,
                                 "total_tokens": 1_540})
    assert (engine.last_prompt_tokens, engine.last_completion_tokens, engine.last_total_tokens) \
        == (1500, 40, 1540)
    assert engine.threshold_tokens == int(200_000 * 0.75)
    status = engine.get_status()
    assert status["folds"] == 0 and status["interval_turns"] == 10 and status["engine"] == "minir"
    assert status["usage_percent"] < 1


def test_clone_for_agent_shares_config_and_no_state(plugin_package, state_db):
    engine = _engine(plugin_package, state_db, interval=7)
    engine.compress(_conversation(6))
    clone = engine.clone_for_agent()
    assert (clone.name, clone._interval) == ("minir", 7)
    assert clone.compress(_conversation(6)) != _conversation(6)  # works on its own
    assert clone.fold_count == 1 and engine.fold_count == 1
    assert clone._session_id == "", "a clone starts unbound; not shared with the parent"


# -- carriers -----------------------------------------------------------------

def test_carrier_round_trip(plugin_package):
    module = plugin_package.context_engine
    folded = [_msg("user", "deploy step 1", uid="abc123"), _msg("assistant", "ran step 1")]
    carrier = module.build_carrier(1, 4, folded, gist="deploying kubernetes")
    assert module.is_carrier(carrier) and carrier["role"] == "user"
    assert carrier["message_uid"], "the engine mints the uid so the host writes it through"
    parsed = module.parse_carrier(carrier)
    assert (parsed["first"], parsed["last"], parsed["count"]) == (1, 4, 2)
    assert parsed["uids"] == ["abc123"]  # only messages that carry a uid are recoverable
    assert "deploying kubernetes" in parsed["gist"]
    assert module.carrier_turn(carrier) == 4
    assert module.folded_uids([carrier, _msg("user", "later")]) == ["abc123"]
    assert not module.is_carrier(_msg("user", "a real turn"))
    assert module.parse_carrier(_msg("assistant", "nope")) is None


def test_carrier_is_bounded_and_self_identifying(plugin_package):
    module = plugin_package.context_engine
    folded = [_msg("user", "x" * 4000, uid=f"{index:04d}") for index in range(200)]
    carrier = module.build_carrier(1, 200, folded, gist="y" * 4000)
    assert len(carrier["content"]) <= module._MAX_CARRIER_CHARS
    assert module.is_carrier(carrier)
    assert carrier["content"].count("gist:") == 1


# -- the fold -----------------------------------------------------------------

def test_foldable_slice_protects_head_tail_and_the_last_user_turn(plugin_package):
    foldable = plugin_package.context_engine.foldable_slice
    messages = _conversation(10)                      # 1 system + 20 messages
    start, end = foldable(messages, 3, 6)
    assert start == 5                                 # the first turn start past the protected head
    assert messages[start]["role"] == "user", "the fold starts on a turn boundary"
    assert messages[start - 1]["role"] == "assistant", "the protected head is a whole turn"
    assert end <= len(messages) - 6                   # protect_last_n untouched
    assert messages[end]["role"] == "user"            # the window reopens on a user turn
    assert messages[end - 1]["role"] == "assistant"   # no turn was split
    last_user = max(index for index, m in enumerate(messages) if m["role"] == "user")
    assert end <= last_user, "the newest user-authored turn is never folded away"


def test_foldable_slice_declines_when_nothing_can_move(plugin_package):
    foldable = plugin_package.context_engine.foldable_slice
    assert foldable([], 3, 6) is None
    assert foldable(_conversation(1), 3, 6) is None     # all protected
    assert foldable([_msg("system", "s")], 3, 6) is None
    assert foldable(_conversation(3), protect_first_n=10, protect_last_n=6) is None


def test_foldable_slice_never_splits_a_tool_group(plugin_package):
    foldable = plugin_package.context_engine.foldable_slice
    messages = [
        _msg("system", "s"),
        _msg("user", "u1"), _msg("assistant", "calling", uid="c1"),
        _msg("tool", "result one"), _msg("assistant", "done"),
        _msg("user", "u2"), _msg("assistant", "a2"),
        _msg("user", "u3"), _msg("assistant", "a3"),
        _msg("user", "u4"), _msg("assistant", "a4"),
    ]
    start, end = foldable(messages, 2, 4)
    assert [m["role"] for m in messages[start:end]] == ["assistant", "tool", "assistant"] or \
        messages[end]["role"] == "user"
    assert messages[end]["role"] == "user"


def test_foldable_slice_honours_a_focus_topic(plugin_package):
    """``/compress <focus>``: content about the focus stays in the window instead of being folded."""
    foldable = plugin_package.context_engine.foldable_slice
    messages = [
        _msg("system", "s"),
        _msg("user", "u1"), _msg("assistant", "a1"),
        _msg("user", "u2"), _msg("assistant", "a2"),
        _msg("user", "u3"), _msg("assistant", "a3"),
        _msg("user", "u4"), _msg("assistant", "a4"),
        _msg("user", "the sqlite migration plan", uid="keep1"),
        _msg("assistant", "the sqlite migration finished", uid="keep2"),
        _msg("user", "u6"), _msg("assistant", "a6"),
    ]
    plain = foldable(messages, 3, 2)
    focused = foldable(messages, 3, 2, focus_topic="sqlite migration")
    assert plain == (5, 11)
    assert focused is not None and focused[1] < plain[1], "the focus rows stay in the window"
    folded_text = [message["content"] for message in messages[focused[0]:focused[1]]]
    assert not any("sqlite migration" in text for text in folded_text)

    everything = foldable(messages[:3] + messages[3:], 1, 0, focus_topic="a")
    assert everything is None or everything[1] > everything[0], "never fold an empty slice"


def test_compress_replaces_the_slice_with_one_carrier(plugin_package, state_db):
    engine = _engine(plugin_package, state_db, interval=1)
    messages = _conversation(10)
    folded = engine.compress(messages)
    assert len(folded) < len(messages)
    carriers = [message for message in folded if plugin_package.context_engine.is_carrier(message)]
    assert len(carriers) == 1, "one fold produces exactly one carrier"
    assert folded[0]["role"] == "system", "the prompt is never folded"
    assert folded[folded.index(carriers[0]) + 1]["role"] == "user", "valid message sequence"
    assert all("role" in message for message in folded)
    assert engine.fold_count == 1 and engine.compression_count == 1
    assert engine.folded_through_turn >= 1


def test_a_second_fold_absorbs_the_first_carrier(plugin_package, state_db):
    engine = _engine(plugin_package, state_db, interval=0)
    messages = _conversation(20)
    once = engine.compress(messages)
    twice = engine.compress(once)
    carriers = [message for message in twice if plugin_package.context_engine.is_carrier(message)]
    assert len(carriers) == 1, "a carrier is folded into the next carrier, never nested"
    uids = plugin_package.context_engine.folded_uids(twice)
    assert "u003" in uids, "the first fold's recoverable ids survive the second"
    assert len(uids) >= 8, "both folds' ids are named by the surviving carrier"


def test_compress_fails_open(plugin_package, state_db):
    engine = _engine(plugin_package, state_db)
    assert engine.compress("not a message list") == "not a message list"
    assert engine.compress([]) == []
    assert engine.compress([_msg("user", "only one")]) == [_msg("user", "only one")]


def test_has_content_to_compress_reports_honestly(plugin_package, state_db):
    engine = _engine(plugin_package, state_db)
    assert engine.has_content_to_compress(_conversation(10)) is True
    assert engine.has_content_to_compress(_conversation(1)) is False


# -- cadence ------------------------------------------------------------------

def test_handoff_inputs_include_the_value_markers(plugin_package, tmp_path):
    store = plugin_package.store.MinirStore(tmp_path / "minir-memory" / "memory.db")
    try:
        store.put_layer_state(
            "base", "user:default",
            "## Session Summary\n- window\n\n## User Representation\n- [goal] ship\n\n"
            "## User Peer Card\n- terse\n\n## AI Self Representation\n- [done] shipped\n\n"
            "## AI Identity Card\n- careful\n\n## Value Markers\n- [tool] pytest\n"
            "- [project] minir-memory", session_id=SESSION)
        _, tail = plugin_package.MinirContextEngine._dialectic_inputs(store, SESSION)
        assert "Tail-window value markers" in tail
        assert "[tool] pytest" in tail
    finally:
        store.close()


def test_no_fold_before_the_first_beat(plugin_package, state_db):
    engine = _engine(plugin_package, state_db, interval=10)
    assert engine.should_compress(0) is False
    assert engine.due(_conversation(3)) is False


def test_one_fold_per_dialectic_beat(plugin_package, state_db, tmp_path):
    """The cadence comes from the memory provider's own counter: its dialectic dispatches."""
    store = plugin_package.store.MinirStore(tmp_path / "minir-memory" / "memory.db")
    engine = _engine(plugin_package, state_db, interval=5, hermes_home=str(tmp_path))
    try:
        assert engine.due() is False
        store.record_job("dialectic", SESSION, 0.0, time.time(), turns=5)
        assert engine.due() is True
        engine.compress(_conversation(8))
        assert engine.due() is False, "the beat that was folded is not folded twice"
        store.record_job("base", SESSION, 0.0, time.time(), turns=99)
        assert engine.due() is False, "another layer's dispatch is not this engine's beat"
        store.record_job("dialectic", SESSION, 0.0, time.time(), turns=13)
        assert engine.due() is True, "the next dialectic dispatch folds again"
    finally:
        store.close()


def test_the_fold_interval_comes_from_the_configured_dialectic_interval(plugin_package, state_db,
                                                                       monkeypatch, tmp_path):
    """The engine and the memory provider must read the same interval, or they fold on different
    beats the moment it is not the default."""
    import sys
    import types

    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"memory": {"minir": {"dialectic_interval": 5}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    sys.modules["hermes_cli"].__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)

    engine = plugin_package.MinirContextEngine(state_db=str(state_db))
    engine.on_session_start(SESSION, hermes_home=str(tmp_path))
    assert engine._interval == 5, "memory.minir.dialectic_interval must drive the fold cadence"
    assert engine.get_status()["interval_turns"] == 5


def test_the_env_interval_wins_like_it_does_for_the_provider(plugin_package, monkeypatch):
    monkeypatch.setenv("MINIR_DIALECTIC_INTERVAL", "3")
    assert plugin_package.cadence.configured_interval("") == 3


def test_an_explicit_interval_survives_the_profile_resolution(plugin_package, state_db, tmp_path):
    """A fork or a test can pin the cadence; a configured profile must not overwrite it."""
    engine = plugin_package.MinirContextEngine(state_db=str(state_db), interval=7)
    engine.on_session_start(SESSION, hermes_home=str(tmp_path))
    assert engine._interval == 7
    assert engine.clone_for_agent()._interval == 7


def test_the_window_counts_turns_without_a_provider(plugin_package, state_db):
    """With no jobs at all the cadence still advances: carriers carry the turns they folded."""
    engine = _engine(plugin_package, state_db, interval=4)
    messages = _conversation(6)                       # 6 user turns, no provider in sight
    assert engine.current_beat(messages) == 6
    since = engine.compress(messages)
    assert plugin_package.context_engine.folded_uids(since), "a fold happened"
    after = engine.current_beat(since)
    assert after >= 6, "folding moves turns from messages to stamps, it does not lose them"
    assert engine.due(since) is False


def test_threshold_pressure_still_triggers_a_fold(plugin_package, state_db):
    engine = _engine(plugin_package, state_db, interval=1000)
    engine.update_model("m", 1000)
    assert engine.should_compress(700) is False
    assert engine.should_compress(800) is True, "the token safety net survives the cadence"
    engine.update_from_response({"prompt_tokens": 500, "completion_tokens": 1, "total_tokens": 501})
    assert engine.should_defer_preflight_to_real_usage(900) is True, \
        "after a fold that fit, preflight must not re-compact on the rough estimate"


def test_prune_tool_results_only_elides_the_old_payloads(plugin_package, state_db):
    engine = _engine(plugin_package, state_db)
    messages = [_msg("system", "s"), _msg("user", "u1"), _msg("assistant", "call"),
                _msg("tool", "x" * 5000), _msg("assistant", "done"),
                _msg("user", "u2"), _msg("assistant", "a2"), _msg("user", "u3"),
                _msg("assistant", "a3"), _msg("user", "u4"), _msg("assistant", "a4"),
                _msg("user", "u5"), _msg("assistant", "a5"), _msg("user", "u6"),
                _msg("assistant", "a6")]
    pruned_messages, count = engine.prune_tool_results_only(messages)
    assert count == 1
    assert pruned_messages[3]["content"].startswith("[tool result elided")
    assert pruned_messages[3]["role"] == "tool" and pruned_messages[3].get("content")
    assert engine.prune_tool_results_only(pruned_messages)[1] == 0, "idempotent"
    assert (engine.prune_tool_results_only("nope") == ("nope", 0))


# -- lossless recall ----------------------------------------------------------

def test_recall_restores_the_folded_originals_from_the_store(plugin_package, state_db):
    """The whole point: the window forgets, the store does not."""
    messages = _conversation(10)
    _store_messages(state_db, messages)
    engine = _engine(plugin_package, state_db, interval=0)
    folded = engine.compress(messages)
    carrier = next(message for message in folded if plugin_package.context_engine.is_carrier(message))
    # u001 sits inside protect_first_n, so it is never folded; u003 is, and it is named.
    assert "u003" in carrier["content"], "the carrier names the ids it covers"
    assert len(carrier["content"]) <= plugin_package.context_engine._MAX_CARRIER_CHARS
    gist = carrier["content"].split("gist: ", 1)[1]
    assert "\n" not in gist and len(gist) <= plugin_package.context_engine._MAX_GIST_CHARS, \
        "the gist is one bounded line, not a second copy of the slice"

    incoming = _msg("user", "what did the kubernetes deploy step say?")
    selected = engine.select_context(folded, incoming_message=incoming)
    assert selected is not None, "the incoming question matches folded content"
    assert len(selected) == len(folded), "request-only: no messages added or removed"
    target = max(index for index, message in enumerate(selected) if message["role"] == "user")
    assert "deploy step 3 about kubernetes" in selected[target]["content"], "restored verbatim"
    for index, (before, after) in enumerate(zip(folded, selected)):
        if index == target:
            assert after["content"].startswith(before["content"]), "only an append"
            continue
        assert before == after, "every other message stays byte-identical (prompt cache)"


def test_recall_stays_out_of_the_way_when_it_does_not_match(plugin_package, state_db):
    messages = _conversation(10)
    _store_messages(state_db, messages)
    engine = _engine(plugin_package, state_db, interval=0)
    folded = engine.compress(messages)
    assert engine.select_context(folded, incoming_message=_msg("user", "ok")) is None
    assert engine.select_context(folded, incoming_message=_msg("user", "thanks")) is None
    assert engine.select_context(folded, incoming_message=_msg("user", "what about the weather in tokyo")) is None
    assert engine.select_context(_conversation(2), incoming_message=_msg("user", "kubernetes deploy")) is None, \
        "nothing was folded, so there is nothing to recall"
    assert engine.select_context([], incoming_message=_msg("user", "kubernetes deploy")) is None


def test_a_recall_block_is_bounded(plugin_package, state_db):
    """A recall must never flood the request: it stops and says where the rest is."""
    messages = [_msg("system", "s")]
    for index in range(8):
        messages.append(_msg("user", f"kubernetes issue {index} " + "z" * 900, uid=f"u{index:03d}"))
        messages.append(_msg("assistant", "noted", uid=f"a{index:03d}"))
    _store_messages(state_db, messages)
    engine = _engine(plugin_package, state_db, interval=0)
    folded = engine.compress(messages)
    assert len(plugin_package.context_engine.folded_uids(folded)) >= 4, "several messages folded"

    selected = engine.select_context(folded, incoming_message=_msg("user", "kubernetes issue problem"))
    assert selected is not None
    target = max(index for index, message in enumerate(selected) if message["role"] == "user")
    injected = selected[target]["content"].split("\n\n", 1)[1]
    assert len(injected) <= plugin_package.context_engine._MAX_RECALL_CHARS + 100
    assert "more available via minir_context_recall" in injected


def test_recall_fails_open_without_a_store(plugin_package, tmp_path):
    engine = plugin_package.MinirContextEngine(state_db=str(tmp_path / "missing.db"))
    engine.on_session_start(SESSION)
    messages = _conversation(10)
    engine.compress(messages)
    incoming = _msg("user", "what did the kubernetes deploy step say?")
    assert engine.select_context(messages, incoming_message=incoming) is None


def test_the_recall_tool_returns_original_text(plugin_package, state_db):
    import json

    messages = _conversation(10)
    _store_messages(state_db, messages)
    engine = _engine(plugin_package, state_db, interval=0)
    folded = engine.compress(messages)
    carrier = next(message for message in folded if plugin_package.context_engine.is_carrier(message))
    _store_messages(state_db, [carrier])  # what the host persists when it accepts the fold
    payload = json.loads(engine.handle_tool_call("minir_context_recall",
                                                 {"query": "kubernetes deploy step"},
                                                 messages=folded))
    assert payload["count"] >= 1
    assert any("deploy step" in hit["text"] for hit in payload["results"])

    from_store = json.loads(engine.handle_tool_call("minir_context_recall",
                                                    {"query": "kubernetes deploy step"}))
    assert from_store["count"] >= 1, "the carriers in the store are the durable index"
    assert json.loads(engine.handle_tool_call("minir_context_recall", {}))["error"]
    assert json.loads(engine.handle_tool_call("other_tool", {}))["error"]
    assert [schema["name"] for schema in engine.get_tool_schemas()] == ["minir_context_recall"]


def test_session_reset_clears_engine_state(plugin_package, state_db):
    engine = _engine(plugin_package, state_db, interval=0)
    engine.compress(_conversation(10))
    engine.update_from_response({"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11})
    engine.on_session_reset()
    assert (engine.fold_count, engine.folded_through_turn, engine.compression_count) == (0, 0, 0)
    assert engine.last_prompt_tokens == 0
    engine.on_session_start(SESSION)
    engine.on_session_end(SESSION, [])
    assert engine._session_id == ""


# -- compression handoff ------------------------------------------------------

HANDOFF = """# Task Handoff
## Goal
- ship the context engine
## Scope
### Included
- compression
## Acceptance Criteria
- [ ] tests pass
## Constraints & Preferences
- stdlib only
## Current State
- folding works
## Progress
### Done
- implemented folds
## Key Decisions
- progressive folds
## Assumptions & Unknowns
### Confirmed
- state.db is read-only
## Relevant Files
- context_engine.py
## Changes Made
- added handoff summary
## Worktree State
- Branch: main
## Verification Evidence
- pytest
## Known Failures & Risks
- none
## Runtime & Deployment State
- Source modified: yes
## Commands & Artifacts
- pytest -q
## Next Steps
1. verify the carrier
## Stop Condition
- all tests pass
## Critical Context
- OS: Windows
"""


def test_handoff_template_is_the_operator_supplied_structure(plugin_package):
    template = plugin_package.handoff.load_template()
    assert "Task Handoff" in template
    assert "## Goal" in template and "## Stop Condition" in template


def test_handoff_normalization_requires_the_contract_headings(plugin_package):
    assert plugin_package.handoff.normalize(HANDOFF).startswith("# Task Handoff")
    assert plugin_package.handoff.normalize("a summary without headings") == ""
    assert plugin_package.handoff.normalize(
        "```markdown\n" + HANDOFF + "```").startswith("# Task Handoff")


def test_should_compress_stamps_the_decision_time(plugin_package, state_db):
    engine = _engine(plugin_package, state_db, interval=10)
    assert engine.should_compress(0) is False
    engine.threshold_tokens = 10
    engine.last_prompt_tokens = 20
    assert engine.should_compress() is True
    assert engine._compress_requested_at > 0


def test_compress_indexes_the_handoff_and_queues_dialectic(plugin_package, state_db, tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.handoff, "summarize", lambda *a, **k: HANDOFF)
    engine = _engine(plugin_package, state_db, interval=1, hermes_home=str(tmp_path))
    engine.threshold_tokens = 10
    engine.last_prompt_tokens = 20
    assert engine.should_compress() is True

    folded = engine.compress(_conversation(10))
    carrier = next(message for message in folded
                   if plugin_package.context_engine.is_carrier(message))
    parsed = plugin_package.context_engine.parse_carrier(carrier)
    assert "ship the context engine" in carrier["content"]
    assert parsed["summary"] and "## Goal" in parsed["summary"]
    assert len(parsed["summary"]) <= plugin_package.context_engine._MAX_CARRIER_SUMMARY

    store = engine._memory_store()
    assert store.newest_conclusion("session_summary", session_id=SESSION) is None, \
        "a pass is not recallable until its carrier is durable"
    engine.on_turn_complete(folded)
    handoff = store.newest_conclusion("session_summary", session_id=SESSION)
    assert handoff is not None and "## Stop Condition" in handoff["content"]
    jobs = store.pending_jobs()
    assert {(job["layer"], job["session_id"]) for job in jobs} == {
        ("base", SESSION), ("dialectic", SESSION)}
    assert jobs[0]["to_ts"] >= engine._compress_requested_at or jobs[0]["to_ts"] > 0


def test_handoff_serialization_keeps_the_newest_turns(plugin_package):
    messages = [_msg("user", f"old {index} " + "x" * 2_000) for index in range(20)]
    messages.append(_msg("user", "newest turn"))
    serialized = plugin_package.handoff.serialize(messages)
    assert "newest turn" in serialized
    assert serialized.startswith("[…earlier messages omitted]")
    assert "old 0 " not in serialized


def test_reasoning_disabled_skips_the_handoff_model(plugin_package, state_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "false")
    monkeypatch.setattr(plugin_package.handoff, "summarize",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    engine = _engine(plugin_package, state_db, interval=1, hermes_home=str(tmp_path))
    engine.threshold_tokens = 10
    engine.last_prompt_tokens = 20
    assert engine.should_compress() is True
    folded = engine.compress(_conversation(10))
    assert plugin_package.context_engine.is_carrier(
        next(message for message in folded
             if plugin_package.context_engine.is_carrier(message)))


def test_compression_input_contains_historical_and_tail_dialectic_conclusions(
        plugin_package, state_db, tmp_path, monkeypatch):
    engine = _engine(plugin_package, state_db, interval=1, hermes_home=str(tmp_path))
    store = engine._memory_store()
    store.put_conclusion("dialectic.constraints", "agent:minir", "- never delete files",
                         subject="agent:minir")
    store.put_conclusion("base.ai_identity_card", "agent:minir", "- careful operator",
                         subject="agent:minir")
    store.put_conclusion("dialectic.review", "review:session-ctx", "- tail review",
                         session_id=SESSION, subject="agent:minir")
    store.put_conclusion("base.user_representation", "focus:session-ctx", "- [goal] ship",
                         session_id=SESSION, subject="user:default")
    store.add_chunk_summary(SESSION, 1.0, 2.0, "local chunk fact")

    seen: dict[str, str] = {}

    def summarize(*args, **kwargs):
        seen.update(kwargs)
        return HANDOFF

    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.handoff, "summarize", summarize)
    engine._summary_enabled = True
    engine.threshold_tokens = 10
    engine.last_prompt_tokens = 20
    assert engine.should_compress() is True
    engine.compress(_conversation(10))

    assert "local chunk fact" in seen["chunks"]
    assert "never delete files" in seen["historical"]
    assert "careful operator" in seen["historical"]
    assert "tail review" in seen["tail"]
    assert "[goal] ship" in seen["tail"]


def test_update_from_response_accepts_nested_and_canonical_usage(plugin_package):
    engine = plugin_package.MinirContextEngine()
    engine.update_from_response({"usage": {"input_tokens": 11, "output_tokens": 7}})
    assert (engine.last_prompt_tokens, engine.last_completion_tokens,
            engine.last_total_tokens) == (11, 7, 18)
    engine.update_from_response({"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25})
    assert (engine.last_prompt_tokens, engine.last_completion_tokens,
            engine.last_total_tokens) == (20, 5, 25)


def test_handoff_prompt_uses_conclusions_and_falls_back_to_turns(plugin_package, monkeypatch):
    captured: dict[str, str] = {}

    def fake_call(task, system, prompt, **kwargs):
        captured["prompt"] = prompt
        return HANDOFF

    monkeypatch.setattr(plugin_package.worker, "call_model", fake_call)
    messages = [{"role": "user", "content": "raw turn detail"}]

    plugin_package.handoff.summarize(messages, historical="historical conclusion",
                                     tail="tail conclusion")
    assert "HISTORICAL DIALECTIC CONCLUSIONS" in captured["prompt"]
    assert "TAIL-WINDOW DIALECTIC CONCLUSIONS" in captured["prompt"]
    assert "raw turn detail" not in captured["prompt"], "conclusions are the contract input"

    plugin_package.handoff.summarize(messages, historical="historical conclusion", tail="")
    assert "TURNS TO SUMMARIZE" in captured["prompt"]
    assert "raw turn detail" in captured["prompt"], "raw turns are the cold-start fallback"


def test_an_old_carrier_does_not_seal_a_new_chunk_chain(plugin_package, state_db, tmp_path):
    engine = _engine(plugin_package, state_db, interval=10, hermes_home=str(tmp_path))
    store = engine._memory_store()
    store.add_chunk_summary(SESSION, 1.0, 2.0, "new active chunk")
    carrier = plugin_package.context_engine.build_carrier(
        1, 2, [_msg("user", "old", uid="u1")], gist="old carrier")

    engine.on_turn_complete([carrier])
    assert store.chunk_summaries(SESSION), "an old carrier must not supersede a live chain"

    store.put_conclusion("session_summary", "summary:" + SESSION, "## Goal\n- done",
                         session_id=SESSION, subject="session", status="pending")
    engine.on_turn_complete([carrier])
    assert store.chunk_summaries(SESSION) == []
