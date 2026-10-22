"""Adviser channels — roster, emission guard, gates, gateway notify."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace


def _advice(text: str, severity: str = "concern") -> str:
    return json.dumps({"action": "advise", "advices": [{
        "severity": severity, "target": "tool_choice", "text": text, "evidence": ["#1"]}]})


def _silent() -> str:
    return json.dumps({"action": "silent"})


def _fake_run(answer_for):
    """``answer_for(message, index) -> answer`` so a roster can return different notes."""
    calls: list[dict] = []

    def _run(message, **kwargs):
        calls.append({"message": message, **kwargs})
        answer = answer_for(message, len(calls) - 1)
        return SimpleNamespace(answer=answer, api_calls=1, model="reviewer", provider="test",
                               toolsets=tuple(kwargs.get("toolsets") or ()))

    return _run, calls


def _provider(make_provider, **overrides):
    cfg = {"enabled": True, "mode": "watch", "cadence": 99, "severity_floor": "concern",
           "max_reviews_per_turn": 4, "max_advices": 3, "max_chars": 900,
           "block_tools": True, "verify_continue": False}
    cfg.update(overrides)
    return make_provider({"adviser_mode": cfg})


def _review_event(provider, text: str = "ran a tool", tool: str = "read_file") -> None:
    provider._adviser_mode.on_post_tool_call(
        session_id="session-test", turn_id="t1", tool_name=tool, status="ok",
        args={"query": text}, result="ok")


# -- roster -------------------------------------------------------------------

def test_roster_runs_each_adviser_with_its_own_cursor(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, roster=[
        {"name": "quality", "instructions": "look for robustness"},
        {"name": "cost", "instructions": "look for wasted model calls"},
    ])
    run, calls = _fake_run(lambda message, index: _advice(f"note from {index}"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider, "first")
    assert provider._adviser_mode.review_once("session-test", "test") is True
    assert len(calls) == 2
    rows = provider._store.pending_advisories("session-test", source="adviser_mode")
    assert sorted(row["adviser"] for row in rows) == ["cost", "quality"]
    assert "look for robustness" in calls[0]["message"]
    assert "adviser: quality" in calls[0]["message"]

    # Both cursors advanced: the next review sees only the new event.
    _review_event(provider, "second")
    assert provider._adviser_mode.review_once("session-test", "test") is True
    assert len(calls) == 4
    assert "second" in calls[2]["message"]
    assert '"first"' not in calls[2]["message"]


def test_roster_names_are_unique(plugin_package, make_provider):
    provider = _provider(make_provider, roster=[{"name": "same"}, {"name": "same"}])
    names = [spec["name"] for spec in provider._adviser_mode._advisers(provider._adviser_mode._cfg())]
    assert names == ["same", "same-2"]


# -- scheduling ---------------------------------------------------------------

def test_agent_end_mode_skips_tool_triggered_reviews(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, review_mode="agent-end")
    run, calls = _fake_run(lambda message, index: _advice("late review"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider)
    assert provider._adviser_mode.wait_idle(2.0)
    assert calls == []

    provider._adviser_mode.on_post_llm_call(
        session_id="session-test", turn_id="t1", assistant_response="done")
    assert provider._adviser_mode.wait_idle(2.0)
    assert len(calls) == 1


def test_review_interval_counts_tool_updates(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, cadence=1, review_interval=2)
    run, calls = _fake_run(lambda message, index: _advice("interval note"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider)
    assert provider._adviser_mode.wait_idle(1.0)
    assert calls == [], "first update is below the interval"
    _review_event(provider)
    assert provider._adviser_mode.wait_idle(2.0)
    assert len(calls) == 1


# -- perception ---------------------------------------------------------------

def test_prompt_carries_project_instructions(plugin_package, make_provider, monkeypatch, tmp_path):
    (tmp_path / "AGENTS.md").write_text("Always run the verify script.", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    provider = _provider(make_provider)
    run, calls = _fake_run(lambda message, index: _silent())
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider)
    provider._adviser_mode.review_once("session-test", "test")
    assert "Always run the verify script." in calls[0]["message"]
    assert "<project_context>" in calls[0]["message"]


def test_prompt_is_passed_through_the_host_redactor(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    seen: list[str] = []

    def _spy(text: str) -> str:
        seen.append(text)
        return text

    monkeypatch.setattr(plugin_package.adviser_mode, "_redact_for_egress", _spy)
    run, _ = _fake_run(lambda message, index: _silent())
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider, "some work")
    provider._adviser_mode.review_once("session-test", "test")
    assert seen and "<transcript_delta>" in seen[0]


def test_recorded_events_drop_injected_advisory_blocks(plugin_package, make_provider):
    provider = _provider(make_provider)
    block = "[adviser — advisory only]\n- [concern · current_step] check the diff\n[end adviser]"
    provider._adviser_mode.record_intent(
        "session-test", f"before\n{block}\nafter", turn_id="t1")
    ring = list(provider._adviser_mode._ring("session-test"))
    assert "check the diff" not in ring[-1]["text"]
    assert ring[-1]["text"].strip() == "before after"


# -- emission guard -----------------------------------------------------------

def test_noise_only_review_is_silent_and_records_drop_reason(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    run, _ = _fake_run(lambda message, index: _advice("looks good"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider)
    assert provider._adviser_mode.review_once("session-test", "test") is True
    assert provider._store.pending_advisories("session-test", source="adviser_mode") == []
    review = provider._store.adviser_reviews("session-test")[0]
    assert review["status"] == "silent"
    assert review["notes_dropped"] == 1
    assert json.loads(review["drop_detail"]) == {"noise": 1}
    stats = provider._store.adviser_mode_stats("session-test")
    assert stats["dropped"] == {"noise": 1}


def test_repeat_across_reviews_is_dropped(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    run, _ = _fake_run(lambda message, index: _advice("check the failing test first"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider, "first")
    provider._adviser_mode.review_once("session-test", "test")
    _review_event(provider, "second")
    provider._adviser_mode.review_once("session-test", "test")
    rows = provider._store.pending_advisories("session-test", source="adviser_mode")
    assert len(rows) == 1
    assert json.loads(provider._store.adviser_reviews("session-test")[0]["drop_detail"]) == {
        "duplicate": 1}


# -- gates --------------------------------------------------------------------

def test_blocker_refuses_a_mutating_tool_once(plugin_package, make_provider):
    provider = _provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "blocker", "target": "tool_choice",
        "text": "the target directory holds the only copy",
    }], source="adviser_mode")

    reason = provider._adviser_mode.block_reason("session-test", "write_file")
    assert reason and "only copy" in reason
    assert provider._store.pending_blocker("session-test", source="adviser_mode") is None
    assert provider._adviser_mode.block_reason("session-test", "write_file") is None


def test_block_gate_ignores_reads_and_can_be_disabled(plugin_package, make_provider):
    provider = _provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "blocker", "text": "stop",
    }], source="adviser_mode")
    assert provider._adviser_mode.block_reason("session-test", "read_file") is None

    provider._config["adviser_mode"]["block_tools"] = False
    assert provider._adviser_mode.block_reason("session-test", "write_file") is None
    assert provider._store.pending_blocker("session-test", source="adviser_mode") is not None


def test_one_turn_can_be_refused_at_most_twice(plugin_package, make_provider):
    provider = _provider(make_provider)
    for index in range(3):
        provider._store.add_advisories("session-test", [{
            "severity": "blocker", "text": f"blocker {index}",
        }], source="adviser_mode")
    provider._adviser_mode._turn_index("session-test", "t1")
    assert provider._adviser_mode.block_reason("session-test", "patch")
    assert provider._adviser_mode.block_reason("session-test", "patch")
    assert provider._adviser_mode.block_reason("session-test", "patch") is None


def test_verify_continue_fires_once_and_is_opt_in(plugin_package, make_provider):
    provider = _provider(make_provider, verify_continue=False)
    provider._store.add_advisories("session-test", [{
        "severity": "blocker", "text": "the migration would drop data",
    }], source="adviser_mode")
    assert provider._adviser_mode.verify_continue("session-test", ["a.py"]) is None

    provider._config["adviser_mode"]["verify_continue"] = True
    result = provider._adviser_mode.verify_continue("session-test", ["a.py"])
    assert result and result["action"] == "continue"
    assert "migration would drop data" in result["message"]
    assert provider._adviser_mode.verify_continue("session-test", ["a.py"]) is None


# -- delivery channels --------------------------------------------------------

def test_aside_block_publishes_a_nit_once(plugin_package, make_provider):
    provider = _provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "nit", "target": "current_step", "text": "rename this helper",
    }], source="adviser_mode")

    block = provider._adviser_mode.aside_block("session-test")
    assert "rename this helper" in block
    assert provider._adviser_mode.aside_block("session-test") == ""
    assert provider._store.pending_advisories("session-test", source="adviser_mode") == []


def test_immune_window_suppresses_concerns_but_not_blockers(plugin_package, make_provider):
    provider = _provider(make_provider)
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "text": "check the exit code",
    }], source="adviser_mode")
    provider._adviser_mode._turn_index("session-test", "t1")
    provider._adviser_mode._interventions["session-test"] = 1
    provider._adviser_mode._turn_index("session-test", "t2")

    request = {"messages": [{"role": "user", "content": "go"}]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="r1", turn_id="t2") is None

    provider._store.add_advisories("session-test", [{
        "severity": "blocker", "text": "stop before deleting",
    }], source="adviser_mode")
    request = {"messages": [{"role": "user", "content": "go"}]}
    assert provider._adviser_mode.handle_llm_request(
        request=request, session_id="session-test", api_request_id="r2", turn_id="t2")
    assert "stop before deleting" in request["messages"][-1]["content"]


# -- gateway preserve ---------------------------------------------------------

def test_gateway_turn_notifies_preserved_advice(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    sent: list[str] = []
    provider._adviser_mode.set_platform("telegram")
    provider._adviser_mode.set_notifier(lambda text: sent.append(text) or True)
    run, _ = _fake_run(lambda message, index: _advice("verify the deploy target"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider)
    provider._adviser_mode.on_post_llm_call(
        session_id="session-test", turn_id="t1", assistant_response="done")
    assert provider._adviser_mode.wait_idle(3.0)
    assert len(sent) == 1 and "verify the deploy target" in sent[0]
    assert provider._store.pending_advisories(
        "session-test", source="adviser_mode"), "a notification does not consume the advice"


def test_turn_end_notifies_advice_published_before_the_terminal_marker(
        plugin_package, make_provider, monkeypatch):
    """A review that finished mid-turn is invisible until the turn ends; the sweep sends it."""
    provider = _provider(make_provider)
    sent: list[str] = []
    provider._adviser_mode.set_platform("telegram")
    provider._adviser_mode.set_notifier(lambda text: sent.append(text) or True)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(lambda m, i: _silent())[0])
    provider._store.add_advisories("session-test", [{
        "severity": "concern", "text": "the retry loop has no backoff",
    }], source="adviser_mode")

    provider._adviser_mode.on_post_llm_call(
        session_id="session-test", turn_id="t1", assistant_response="done")
    assert provider._adviser_mode.wait_idle(2.0)
    assert len(sent) == 1 and "no backoff" in sent[0]


def test_send_reads_the_host_tool_result(plugin_package, make_provider, monkeypatch):
    import sys
    import types

    provider = _provider(make_provider)
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # mark it as a package so the submodule import resolves
    module = types.ModuleType("tools.send_message_tool")
    tools_pkg.send_message_tool = module
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", module)

    module.send_message_tool = lambda args: json.dumps({"success": True})
    assert provider._adviser_mode._send("telegram", "hi") is True
    module.send_message_tool = lambda args: json.dumps({"error": "no home channel"})
    assert provider._adviser_mode._send("telegram", "hi") is False


def test_cli_turn_does_not_notify(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    sent: list[str] = []
    provider._adviser_mode.set_platform("cli")
    provider._adviser_mode.set_notifier(lambda text: sent.append(text) or True)
    run, _ = _fake_run(lambda message, index: _advice("verify the deploy target"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider)
    provider._adviser_mode.on_post_llm_call(
        session_id="session-test", turn_id="t1", assistant_response="done")
    assert provider._adviser_mode.wait_idle(3.0)
    assert sent == []


def test_review_interval_counts_the_final_update(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, cadence=1, review_interval=3)
    run, calls = _fake_run(lambda message, index: _advice("third update only"))
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)

    _review_event(provider)
    assert provider._adviser_mode.wait_idle(1.0)
    assert calls == [], "first update is below the interval"

    provider._adviser_mode.on_post_llm_call(
        session_id="session-test", turn_id="t1", assistant_response="done")
    assert provider._adviser_mode.wait_idle(1.0)
    assert calls == [], "the finished turn is the second eligible update, not the third"

    _review_event(provider)
    assert provider._adviser_mode.wait_idle(2.0)
    assert len(calls) == 1


def test_reasoning_is_enabled_by_default(plugin_package, make_provider):
    provider = _provider(make_provider)
    assert provider._adviser_mode._cfg()["include_reasoning"] is True


def test_session_end_waits_for_an_in_flight_review(plugin_package, make_provider, monkeypatch):
    import threading

    provider = _provider(make_provider, cadence=1, shutdown_wait_ms=3000)
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def run(message, **kwargs):
        started.set()
        release.wait(2.0)
        completed.set()
        return SimpleNamespace(answer=_silent(), api_calls=1, model="reviewer",
                               provider="test", toolsets=())

    monkeypatch.setattr(plugin_package.agent_loop, "run", run)
    _review_event(provider)
    assert started.wait(2.0), "the review did not start"

    ending = threading.Thread(
        target=provider._adviser_mode.session_end, args=("session-test",))
    ending.start()
    assert ending.is_alive(), "session_end dropped the queued review instead of waiting"

    release.set()
    ending.join(3.0)
    assert not ending.is_alive()
    assert completed.is_set()
