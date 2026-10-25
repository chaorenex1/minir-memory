"""Persistent adviser runtime: history, transcript delta, reasoning, turn-end notes, roster."""

from __future__ import annotations

import json
from types import SimpleNamespace

SESSION = "session-test"


def _advice(text: str, *, deferred: bool = False) -> str:
    item = {"severity": "concern", "target": "tool_choice", "text": text,
            "evidence": ["#1"], "deferred": deferred}
    return json.dumps({"action": "advise", "advices": [item]})


def _run(answer: str, calls: list | None = None):
    def _fake(message, **kwargs):
        if calls is not None:
            calls.append({"message": message, **kwargs})
        # The host returns history + this run; mirror that shape so the runtime's merge
        # detects the prefix instead of appending a duplicate copy.
        prior = list(kwargs.get("history") or ())
        messages = prior + [{"role": "user", "content": message},
                            {"role": "assistant", "content": answer}]
        return SimpleNamespace(answer=answer, api_calls=1, model="reviewer",
                               provider="test", toolsets=tuple(kwargs.get("toolsets") or ()),
                               messages=tuple(messages), usage={})
    return _fake


def _provider(make_provider, **overrides):
    cfg = {"enabled": True, "cadence": 99, "review_interval": 99,
           "severity_floor": "concern", "max_reviews_per_turn": 8, "max_advices": 3,
           "max_chars": 900, "host_steer": False, "host_continue": False,
           "host_interrupt": False, "trigger_turn": False, "claims": "off"}
    cfg.update(overrides)
    return make_provider({"adviser_mode": cfg})


def _event(provider, text: str = "ran a tool") -> None:
    provider._adviser_mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": text}, result="ok")


def _review(provider, trigger: str = "test") -> bool:
    return provider._adviser_mode.review_once(SESSION, trigger)


def test_second_review_continues_with_history_and_stable_session(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice("first"), calls))

    _event(provider, "one")
    assert _review(provider) is True
    _event(provider, "two")
    assert _review(provider) is True

    assert len(calls) == 2
    assert calls[0]["session_id"].startswith("minir-adviser-")
    assert calls[1]["session_id"] == calls[0]["session_id"]
    history = list(calls[1]["history"] or ())
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert "first" in history[-1]["content"]


def test_tool_event_keeps_the_full_result_without_a_cap(
        plugin_package, make_provider):
    provider = _provider(make_provider)
    big = "R" * 20_000
    provider._adviser_mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args="A" * 6_000, result=big)

    event = list(provider._adviser_mode._ring(SESSION))[-1]
    assert event["result"] == big
    assert "elided" not in event
    prompt = provider._adviser_mode._prompt(SESSION, "t1", "tool", [event])
    assert "R" * 20_000 in prompt
    assert "chars elided" not in prompt


def test_full_reasoning_is_written_without_the_prompt_cap(
        plugin_package, make_provider, tmp_path):
    provider = _provider(make_provider, include_reasoning=True, reasoning_log=True)
    mode = provider._adviser_mode
    mode.on_stream_delta(SESSION, "R" * 9_000)

    assert len(mode._reasoning[SESSION]) == 9_000
    mode._flush_reasoning(SESSION)

    path = tmp_path / "minir-memory" / "sessions" / "default" / "reasoning" / "session-test.log"
    assert path.exists()
    assert "R" * 9_000 in path.read_text(encoding="utf-8")


def test_deferred_note_is_held_mid_turn_and_rendered_at_turn_end(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice("later", deferred=True)))
    mode = provider._adviser_mode

    mode.on_pre_llm_call(SESSION, "t1", user_message="start")
    _event(provider, "one")
    assert _review(provider, "test") is True
    rows = provider._store.pending_advisories(SESSION, source="adviser_mode")
    assert rows and rows[0]["deferred"] == 1
    assert mode._deliverable_rows(SESSION, "req") == []

    mode.on_pre_llm_call(SESSION, "t2", user_message="next")
    block, included = plugin_package.adviser_mode.render_block(
        rows, 900, current_turn=2,
        guidance=plugin_package.adviser_prompts.boundary_guidance(50))
    assert "later" in block and "1 turn ago" in block
    assert "[boundary-guidance]" in block


def test_boundary_guidance_only_on_the_configured_turn(plugin_package):
    guidance = plugin_package.adviser_prompts.boundary_guidance
    assert guidance(49) == ""
    assert guidance(50)
    assert guidance(100)
    assert guidance(50, 25) == guidance(50)


def test_roster_has_no_five_member_cap_and_skips_a_paused_member(
        plugin_package, make_provider, monkeypatch):
    roster = [{"name": f"a{index}"} for index in range(5)]
    roster[2]["paused"] = True
    provider = _provider(make_provider, roster=roster)
    specs = provider._adviser_mode._advisers(provider._adviser_mode._cfg())
    assert len(specs) == 5
    assert [spec["paused"] for spec in specs] == [False, False, True, False, False]

    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice("same"), calls))
    _event(provider, "one")
    provider._adviser_mode.review_once(SESSION, "test")
    assert len(calls) == 4


def test_usage_limit_pauses_only_the_failing_adviser(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, roster=[{"name": "bad"}, {"name": "good"}])

    def run(message, **kwargs):
        if "--bad-" in str(kwargs.get("session_id") or ""):
            raise RuntimeError("429 rate limit exceeded")
        return _run(_advice("ok"))(message, **kwargs)

    monkeypatch.setattr(plugin_package.agent_loop, "run", run)
    _event(provider, "one")
    provider._adviser_mode.review_once(SESSION, "test")

    mode = provider._adviser_mode
    assert mode._runtime(SESSION, "bad").paused()
    assert not mode._runtime(SESSION, "good").paused()


def test_on_post_llm_call_runs_the_worker_with_the_final_trigger(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, review_interval=1)
    calls: list = []
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice("done"), calls))
    mode = provider._adviser_mode
    mode.on_pre_llm_call(SESSION, "t1", user_message="start")
    _event(provider, "one")

    mode.on_post_llm_call(SESSION, "t1", assistant_response="finished")
    assert mode.wait_idle(2.0)
    assert len(calls) == 1
    assert "trigger: final" in calls[0]["message"]


def test_advisories_carry_turn_and_deferred(plugin_package, make_provider):
    provider = _provider(make_provider)
    provider._store.add_advisories(
        SESSION,
        [{"content": "later", "severity": "concern", "turn": 7, "deferred": True}],
        source="adviser_mode")
    row = provider._store.pending_advisories(SESSION, source="adviser_mode")[0]
    assert row["turn"] == 7
    assert row["deferred"] == 1


def test_runtime_resumes_history_from_the_transcript_file(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _run(_advice("keep me")))
    _event(provider, "one")
    assert _review(provider) is True

    mode = provider._adviser_mode
    mode._runtimes.clear()  # simulate a process restart; the JSON trail survives
    runtime = mode._runtime(SESSION, "default")
    assert [row["role"] for row in runtime.messages] == ["user", "assistant"]
    assert "keep me" in runtime.messages[-1]["content"]
