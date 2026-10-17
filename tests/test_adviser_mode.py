"""Adviser mode — per-turn review, cadence, delivery, storage migration."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace


def _run(answer: str, calls: list | None = None):
    def _fake(message, **kwargs):
        if calls is not None:
            calls.append({"message": message, **kwargs})
        return SimpleNamespace(answer=answer, api_calls=1, model="reviewer",
                               provider="test", toolsets=tuple(kwargs.get("toolsets") or ()))
    return _fake


def _advice(text: str = "check stderr before retrying", severity: str = "concern") -> str:
    return json.dumps({"action": "advise", "advices": [{
        "severity": severity, "target": "tool_choice", "text": text,
        "evidence": ["#1"], "confidence": 0.8}]})


def _mode_provider(make_provider, *, cadence: int = 2, floor: str = "nit",
                   mode: str = "watch"):
    return make_provider({"adviser_mode": {
        "enabled": True, "mode": mode, "cadence": cadence, "severity_floor": floor,
        "max_reviews_per_turn": 4, "max_advices": 3, "max_chars": 900,
    }})


# -- scheduling ---------------------------------------------------------------

def test_cadence_enqueues_a_review(plugin_package, make_provider, monkeypatch):
    provider = _mode_provider(make_provider, cadence=2)
    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice(), calls))

    provider._adviser_mode.on_post_tool_call(
        session_id="session-test", turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="contents")
    assert provider._adviser_mode.wait_idle(2.0)
    assert calls == [], "one tool call before cadence=2 must not review"

    provider._adviser_mode.on_post_tool_call(
        session_id="session-test", turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "b.py"}, result="contents")
    assert provider._adviser_mode.wait_idle(2.0)
    assert len(calls) == 1
    pending = provider._store.pending_advisories("session-test", source="adviser_mode")
    assert [row["content"] for row in pending] == ["check stderr before retrying"]


def test_error_and_mutation_force_a_review(plugin_package, make_provider, monkeypatch):
    provider = _mode_provider(make_provider, cadence=99)
    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice(), calls))

    provider._adviser_mode.on_post_tool_call(
        session_id="session-test", turn_id="t1", tool_name="terminal", status="error",
        args={"command": "pytest"}, result="exit 1", error_message="failed")
    assert provider._adviser_mode.wait_idle(2.0)
    assert len(calls) == 1


def test_review_input_contains_intent_and_tool_outcome(plugin_package, make_provider, monkeypatch):
    provider = _mode_provider(make_provider, cadence=1)
    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice(), calls))

    provider._adviser_mode.on_interim_message(
        session_id="session-test", turn_id="t1", text="I will rerun the migration")
    provider._adviser_mode.on_post_tool_call(
        session_id="session-test", turn_id="t1", tool_name="terminal", status="error",
        args={"command": "migrate"}, result="boom", error_message="boom")
    assert provider._adviser_mode.wait_idle(2.0)
    prompt = calls[0]["message"]
    assert "I will rerun the migration" in prompt
    assert "terminal" in prompt and "boom" in prompt


def test_post_api_request_records_tool_call_intent(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    provider._adviser_mode.on_post_api_request(
        session_id="session-test", turn_id="t1",
        assistant_message={"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "terminal", "arguments": "{\"command\": \"pytest\"}"}}]},
        assistant_tool_call_count=1)
    events = list(provider._adviser_mode._rings["session-test"])
    assert events[-1]["kind"] == "intent"
    assert "terminal" in events[-1]["text"] and "pytest" in events[-1]["text"]


def test_mode_off_never_reviews(plugin_package, make_provider, monkeypatch):
    provider = make_provider({"adviser_mode": {"enabled": False, "cadence": 1}})
    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice(), calls))
    provider._adviser_mode.on_post_tool_call(
        session_id="session-test", turn_id="t1", tool_name="terminal", status="error",
        result="boom", error_message="boom")
    assert provider._adviser_mode.wait_idle(0.3)
    assert calls == []


def test_session_end_drops_a_late_review(plugin_package, make_provider, monkeypatch):
    provider = _mode_provider(make_provider, cadence=1)

    def _late(message, **kwargs):
        provider._adviser_mode.session_end("session-test")
        return SimpleNamespace(answer=_advice("late advice"), api_calls=1, model="reviewer",
                               provider="test", toolsets=("adviser",))

    monkeypatch.setattr(plugin_package.agent_loop, "run", _late)
    provider._adviser_mode.record_tool("session-test", "terminal", turn_id="t1",
                                       status="error", result="boom", error="boom")
    assert provider._adviser_mode.review_once("session-test", trigger="test") is False
    assert provider._store.pending_advisories(
        "session-test", source="adviser_mode") == []
    assert provider._store.adviser_reviews("session-test")[0]["status"] == "dropped"


def test_adviser_only_policy_survives_the_compression_gate(plugin_package, make_provider):
    provider = _mode_provider(make_provider, mode="adviser_only")
    provider._store.set_compression_gate("session-test", expires_at=time.time() + 60)
    try:
        assert provider._adviser_mode.active("session-test") is False
        assert provider._adviser_mode.mode_brief("session-test")
        assert provider._adviser_mode.blocks_tool("session-test", "write_file") is True
        assert provider._adviser_mode.blocks_tool("session-test", "delegate_task") is True
        assert provider._adviser_mode.blocks_tool("session-test", "read_file") is False
    finally:
        provider._store.clear_compression_gate("session-test")


def test_turn_boundary_does_not_drain_adviser_mode_advice(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "target": "current_step", "text": "mid-turn only",
    }], source="adviser_mode")
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="t1", provider=provider,
        user_message="hi") is None
    assert len(provider._store.pending_advisories(
        "session-test", source="adviser_mode")) == 1

    request = {"messages": [{"role": "user", "content": "hi"}]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-1")


def test_compression_gate_pauses_reviews(plugin_package, make_provider, monkeypatch):
    provider = _mode_provider(make_provider, cadence=1)
    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice(), calls))
    provider._store.set_compression_gate("session-test", expires_at=time.time() + 60)
    try:
        assert provider._adviser_mode.active("session-test") is False
        provider._adviser_mode.on_post_tool_call(
            session_id="session-test", turn_id="t1", tool_name="terminal", status="error",
            result="boom", error_message="boom")
        assert provider._adviser_mode.wait_idle(0.3)
        assert calls == []
    finally:
        provider._store.clear_compression_gate("session-test")


# -- delivery -----------------------------------------------------------------

def test_middleware_injects_and_confirms_delivery(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "blocker", "target": "tool_choice",
        "text": "verify the target before deleting", "evidence": ["#3"],
    }], source="adviser_mode")

    request = {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "exit 1"},
    ]}
    result = provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-1")
    assert result and result["request"] is request
    assert "verify the target before deleting" in request["messages"][-1]["content"]
    assert request["messages"][-1]["role"] == "tool", "advice rides the tool result, not a new role"
    assert provider._store.staged_advisories("req-1")

    provider._adviser_mode.on_post_api_request(
        session_id="session-test", api_request_id="req-1")
    assert provider._store.pending_advisories(
        "session-test", source="adviser_mode") == []
    assert provider._store.staged_advisories("req-1") == []


def test_middleware_falls_back_to_the_active_provider(plugin_package, make_provider):
    active = _mode_provider(make_provider)
    active._store.add_advisories("session-test", [{
        "severity": "concern", "target": "current_step", "text": "active provider advice",
    }], source="adviser_mode")
    shadow = plugin_package.MinirMemoryProvider({})  # constructed by register(), not initialized

    request = {"messages": [{"role": "user", "content": "hi"}]}
    assert plugin_package._on_llm_request(
        request=request, provider=shadow, session_id="session-test",
        api_request_id="req-shadow")
    assert "active provider advice" in request["messages"][-1]["content"]


def test_duplicate_middleware_callback_does_not_append_twice(plugin_package, make_provider):
    """The host can register the same plugin directory twice; one request gets one block."""
    provider = _mode_provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "target": "current_step", "text": "check the diff",
    }], source="adviser_mode")
    request = {"messages": [{"role": "user", "content": "hi"}]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-dup")
    before = len(request["messages"])
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-dup") is None
    assert len(request["messages"]) == before
    assert sum("check the diff" in str(message.get("content"))
               for message in request["messages"]) == 1


def test_failed_request_returns_advice_to_pending(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "target": "current_step", "text": "check the diff",
    }], source="adviser_mode")
    request = {"messages": [{"role": "user", "content": "hi"}]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-2")
    assert provider._store.staged_advisories("req-2")
    provider._adviser_mode.on_api_request_error(
        session_id="session-test", api_request_id="req-2")
    assert len(provider._store.pending_advisories(
        "session-test", source="adviser_mode")) == 1

    retry = {"messages": [{"role": "user", "content": "hi"}]}
    assert provider._adviser_mode.handle_llm_request(
        request=retry, session_id="session-test", api_request_id="req-3")


def test_dialectic_advice_is_not_injected_mid_turn(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "target": "user_peer_card", "text": "dialectic only",
    }], source="dialectic")
    request = {"messages": [{"role": "user", "content": "hi"}]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-4") is None
    assert request["messages"] == [{"role": "user", "content": "hi"}]


def test_unknown_wire_shape_defers_injection(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "target": "current_step", "text": "keep pending",
    }], source="adviser_mode")
    request = {"messages": [{"role": "tool", "content": [{"toolResult": {"content": []}}]}]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-bedrock",
        api_mode="bedrock_converse") is None
    assert len(provider._store.pending_advisories(
        "session-test", source="adviser_mode")) == 1


def test_pending_tool_call_defers_injection(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "target": "current_step", "text": "wait for the result",
    }], source="adviser_mode")
    request = {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
    ]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="req-5") is None
    assert len(provider._store.pending_advisories(
        "session-test", source="adviser_mode")) == 1


# -- parsing and mode state ---------------------------------------------------

def test_parser_defaults_to_silent_and_applies_floor(plugin_package):
    module = plugin_package.adviser_mode
    assert module.parse_review('{"action":"silent"}') == []
    assert module.parse_review("not json") == []
    rows = module.parse_review(json.dumps({"action": "advise", "advices": [
        {"severity": "nit", "text": "polish"},
        {"severity": "concern", "text": "fix it"},
    ]}), severity_floor="concern")
    assert [row["content"] for row in rows] == ["fix it"]
    rows = module.parse_review(json.dumps({"action": "advise", "advices": [
        {"severity": "blocker", "text": "stop"}]}), severity_floor="concern")
    assert rows[0]["severity"] == "blocker"


def test_block_rendering_is_bounded(plugin_package):
    rows = [{"id": index, "severity": "concern", "target": "current_step",
             "content": "x" * 400} for index in range(1, 7)]
    block, included = plugin_package.adviser_mode.render_block(rows, max_chars=500)
    assert block and len(block) <= 500
    assert 0 < len(included) < len(rows)


def test_adviser_only_injects_brief_and_blocks_mutations(plugin_package, make_provider):
    provider = _mode_provider(make_provider, mode="adviser_only")

    brief = plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="t1", provider=provider,
        user_message="fix the bug")
    assert brief and "[adviser-only mode" in brief

    blocked = plugin_package._on_pre_tool_call(
        tool_name="write_file", session_id="session-test", provider=provider)
    assert blocked and blocked["action"] == "block"
    assert plugin_package._on_pre_tool_call(
        tool_name="read_file", session_id="session-test", provider=provider) is None

    watch = _mode_provider(make_provider, mode="watch")
    assert plugin_package._on_pre_tool_call(
        tool_name="write_file", session_id="session-test", provider=watch) is None


def test_enabled_mode_and_cadence_come_only_from_config(plugin_package, make_provider):
    """No slash command, no per-session ledger: ``memory.minir.adviser_mode`` is the switch."""
    off = make_provider({"adviser_mode": {"enabled": False, "mode": "watch", "cadence": 7}})
    assert off._adviser_mode.enabled("session-test") is False
    assert off._adviser_mode.mode("session-test") == "watch"
    assert off._adviser_mode.cadence("session-test") == 7

    on = make_provider({"adviser_mode": {"enabled": True, "mode": "adviser_only",
                                         "cadence": 2}})
    assert on._adviser_mode.enabled("session-test") is True
    assert on._adviser_mode.mode("session-test") == "adviser_only"
    assert on._adviser_mode.cadence("session-test") == 2


# -- migration ----------------------------------------------------------------

def test_old_advisories_table_migrates_before_staged_reset(plugin_package, tmp_path):
    """An old ledger lacks request_id; the reset must run after the ALTER loop."""
    MinirStore = plugin_package.store.MinirStore

    db_path = Path(tmp_path) / "minir-memory" / "memory.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(
            "CREATE TABLE advisories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, revision_id INTEGER NOT NULL DEFAULT 0,"
            "session_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'dialectic',"
            "severity TEXT NOT NULL DEFAULT 'concern', target TEXT NOT NULL DEFAULT '',"
            "content TEXT NOT NULL DEFAULT '', decision TEXT NOT NULL DEFAULT '',"
            "evidence TEXT NOT NULL DEFAULT '[]', fingerprint TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,"
            "injected_at REAL, turn_id TEXT NOT NULL DEFAULT '');")
        connection.execute(
            "INSERT INTO advisories (session_id, fingerprint, status, created_at, turn_id) "
            "VALUES ('s', 'f', 'staged', 1.0, '')")
        connection.commit()
    finally:
        connection.close()

    store = MinirStore(db_path)
    try:
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(advisories)")}
        assert {"request_id", "delivered_at", "review_id", "expires_at"} <= columns
        row = store._conn.execute("SELECT status, request_id FROM advisories").fetchone()
        assert (row["status"], row["request_id"]) == ("pending", "")
    finally:
        store.close()
