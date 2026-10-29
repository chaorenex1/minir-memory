"""Adviser history, read-only review, and roster cadence (no database rows)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from adviser_fakes import drive_advise

SESSION = "session-test"


def _advice(text: str) -> str:
    return json.dumps({"action": "advise", "advices": [{
        "severity": "concern", "target": "tool_choice", "text": text, "evidence": ["#1"],
    }]})


def _fake_run(answer: str, *, messages=None, usage=None):
    def _run(message, **kwargs):
        prior = tuple(kwargs.get("history") or ())
        drive_advise(kwargs, answer)
        return SimpleNamespace(
            answer=answer, api_calls=1, model="reviewer", provider="test",
            toolsets=tuple(kwargs.get("toolsets") or ()),
            messages=tuple(messages or (prior + ({"role": "user", "content": message},
                                                 {"role": "assistant", "content": answer}))),
            usage=dict(usage or {}),
        )
    return _run


def _provider(make_provider, **overrides):
    cfg = {"enabled": True, "mode": "watch", "cadence": 99, "review_interval": 99,
           "severity_floor": "concern",
           "max_reviews_per_turn": 8, "max_advices": 3, "max_chars": 900,
           "host_steer": False, "host_continue": False}
    cfg.update(overrides)
    return make_provider({"adviser_mode": cfg})


def _event(provider, text: str = "ran a tool") -> None:
    provider._adviser_mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": text}, result="ok")


def _review(provider, trigger: str = "test") -> bool:
    return provider._adviser_mode.review_once(SESSION, trigger)


def test_reviewer_runs_read_only_and_records_the_run(plugin_package, make_provider,
                                                     tmp_path, monkeypatch):
    provider = _provider(make_provider)
    seen: dict = {}

    def run(message, **kwargs):
        seen.update(kwargs)
        return _fake_run(
            _advice("check the failing test"),
            usage={"total_tokens": 120, "estimated_cost_usd": 0.0012},
        )(message, **kwargs)

    monkeypatch.setattr(plugin_package.agent_loop, "run", run)
    _event(provider)
    assert _review(provider) is True
    assert seen.get("read_only") is True

    files = list((tmp_path / "minir-memory" / "sessions" / "default" / "adviser").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    messages = [row for row in rows if row["kind"] == "msg" and row["role"] != "system"]
    run_end = [row for row in rows if row["kind"] == "run_end"]
    assert rows[0]["session_id"] == f"{SESSION}--default"
    assert run_end[-1]["usage"] == {"total_tokens": 120, "estimated_cost_usd": 0.0012}
    assert [row["role"] for row in messages] == ["user", "assistant"]

    usage = provider._adviser_mode.usage_stats(SESSION)
    assert usage["default"]["tokens"] == 120
    assert usage["default"]["turns"] == 1


def test_a_second_review_appends_to_the_same_adviser_history(plugin_package, make_provider,
                                                             tmp_path, monkeypatch):
    provider = _provider(make_provider)
    run, _calls = _fake_run(_advice("first")), None
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)
    _event(provider, "one")
    assert _review(provider) is True
    _event(provider, "two")
    assert _review(provider) is True

    path = next((tmp_path / "minir-memory" / "sessions" / "default" / "adviser").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    messages = [row for row in rows if row["kind"] == "msg" and row["role"] != "system"]
    # Two runs, two message pairs each: the file is the adviser's conversation, not one review.
    assert [row["role"] for row in messages] == [
        "user", "assistant", "user", "assistant"]
    assert messages[0].get("text") != messages[2].get("text")


def test_watchdog_roster_merges_and_config_overrides(plugin_package, make_provider, monkeypatch):
    monkeypatch.setattr(plugin_package.adviser_mode, "_watchdog_roster", lambda: [
        {"name": "quality", "cadence": 5, "tools": ["file"]},
        {"name": "cost", "review_mode": "agent-end"},
    ])
    provider = _provider(make_provider, roster=[{"name": "cost", "instructions": "override"}])
    specs = provider._adviser_mode._advisers(provider._adviser_mode._cfg())

    assert [spec["name"] for spec in specs] == ["quality", "cost"]
    assert specs[0]["cadence"] == 5 and specs[0]["toolsets"] == ["file"]
    assert specs[1]["instructions"] == "override"
    assert specs[1]["review_mode"] == "agent-end"


def test_roster_cadence_keeps_skipped_events_for_the_next_review(plugin_package, make_provider,
                                                                 monkeypatch):
    provider = _provider(make_provider, roster=[{"name": "quality", "cadence": 3}])
    run, calls = _fake_run(_advice("after three")), []
    monkeypatch.setattr(plugin_package.agent_loop, "run",
                        lambda message, **kwargs: calls.append(message) or run(message, **kwargs))

    _event(provider, "one")
    _event(provider, "two")
    assert _review(provider, "tool") is False
    _event(provider, "three")
    assert _review(provider, "tool") is True
    assert len(calls) == 1
    assert "three" in calls[0]


def test_agent_end_roster_entry_only_reviews_the_final_answer(plugin_package, make_provider,
                                                             monkeypatch):
    provider = _provider(make_provider,
                         roster=[{"name": "late", "review_mode": "agent-end"}])
    run, calls = _fake_run(_advice("at the end")), []
    monkeypatch.setattr(plugin_package.agent_loop, "run",
                        lambda message, **kwargs: calls.append(message) or run(message, **kwargs))

    _event(provider)
    assert _review(provider, "tool") is False
    assert _review(provider, "final") is True
    assert len(calls) == 1


def _call(call_id: str, tool: str) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "function": {"name": tool}}]}


def test_eviction_cuts_where_the_saving_beats_the_rewrite(plugin_package, make_provider):
    """T3.5/T3.7: token-priced margin-max cut, tail protected from the newest user message."""
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    big = "x" * 5000
    rows = [
        {"role": "user", "content": "review turn 1"},
        _call("c1", "read_file"),
        {"role": "tool", "content": big, "tool_call_id": "c1"},
        {"role": "assistant", "content": "the tool failed to say anything"},
        {"role": "user", "content": "review turn 2"},
        _call("c2", "search_files"),
        {"role": "tool", "content": "small result", "tool_call_id": "c2"},
        {"role": "assistant", "content": "none"},
        {"role": "user", "content": "review turn 3"},
        {"role": "assistant", "content": "advice 3"},
    ]
    runtime = plugin_package.adviser_mode.AdvisorRuntime(session=SESSION, adviser="default")

    trimmed = mode._trim_history(rows, runtime)

    assert trimmed[0]["content"] == "review turn 1", "the start never moves"
    assert trimmed[2]["content"].startswith("[Stale result elided - ")
    assert trimmed[2]["content"].endswith(" tokens]")
    assert trimmed[-1]["content"] == "advice 3"
    assert runtime.evicted_chars > 4000
    assert runtime.cache_epoch == 1, "the cut is a cache-epoch boundary"

    # Pruned means pruned: a second pass finds no candidate and changes nothing.
    again = mode._trim_history(trimmed, runtime)
    assert again[2]["content"] == trimmed[2]["content"]
    assert runtime.cache_epoch == 1


def test_an_error_result_is_never_evicted(plugin_package, make_provider):
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    body = "Traceback (most recent call last): " + "y" * 5000
    rows = ([{"role": "user", "content": "review turn 1"}, _call("c1", "read_file"),
             {"role": "tool", "content": body, "tool_call_id": "c1", "status": "error"},
             {"role": "user", "content": "review turn 2"},
             {"role": "assistant", "content": "advice"}])

    trimmed, saved, evicted = mode._evict_tool_results(rows)

    assert (saved, evicted) == (0, 0)
    assert any(body in row["content"] for row in trimmed)


def test_only_rederivable_tool_results_are_candidates(plugin_package, make_provider):
    """T3.6: read/grep are re-derivable; a write or a terminal run is evidence."""
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    big = "z" * 4000
    rows = [
        {"role": "user", "content": "review turn 1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "w", "function": {"name": "write_file"}},
            {"id": "t", "function": {"name": "terminal"}},
            {"id": "r", "function": {"name": "read_file"}}]},
        {"role": "tool", "content": big, "tool_call_id": "w"},
        {"role": "tool", "content": big, "tool_call_id": "t"},
        {"role": "tool", "content": big, "tool_call_id": "r"},
        {"role": "user", "content": "review turn 2"},
    ]

    trimmed, _saved, evicted = mode._evict_tool_results(rows)

    assert evicted == 1
    assert trimmed[2]["content"] == big and trimmed[3]["content"] == big
    assert trimmed[4]["content"].startswith("[Stale result elided - ")


def test_a_result_below_the_prune_threshold_is_left_alone(plugin_package, make_provider):
    """T3.8: MIN_PRUNE_TOKENS — a result too small to pay for the rewrite is not a candidate."""
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    rows = [{"role": "user", "content": "review turn 1"}, _call("c1", "read_file"),
            {"role": "tool", "content": "ok", "tool_call_id": "c1"},
            {"role": "user", "content": "review turn 2"}]

    assert mode._evict_tool_results(rows)[1:] == (0, 0)


def test_the_newest_user_message_protects_the_tail(plugin_package, make_provider):
    """T3.7: everything from the latest review's prompt on is the current evidence."""
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    stale, current = "q" * 8000, "r" * 1200
    rows = [
        {"role": "user", "content": "review turn 1"},
        _call("c1", "read_file"), {"role": "tool", "content": stale, "tool_call_id": "c1"},
        {"role": "assistant", "content": "advice 1"},
        {"role": "user", "content": "review turn 2"},
        _call("c2", "read_file"), {"role": "tool", "content": current, "tool_call_id": "c2"},
        {"role": "assistant", "content": "advice 2"},
    ]

    trimmed, _saved, evicted = mode._evict_tool_results(rows)

    assert evicted == 1
    assert trimmed[2]["content"].startswith("[Stale result elided - ")
    assert trimmed[6]["content"] == current, "the latest review's own evidence is untouched"


def test_the_ledger_row_owns_what_this_review_evicted(plugin_package, make_provider,
                                                      monkeypatch):
    """T3.7 acceptance: eviction runs before the request, so its chars land on this row."""
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    runtime = mode._runtime(SESSION, "default")
    runtime.messages = [
        {"role": "user", "content": "review turn 1"},
        _call("c1", "read_file"),
        {"role": "tool", "content": "x" * 8000, "tool_call_id": "c1"},
        {"role": "assistant", "content": "advice 1"},
        {"role": "user", "content": "review turn 2"},
        {"role": "assistant", "content": "advice 2"},
    ]

    def _fake(message, **kwargs):
        drive_advise(kwargs, _advice("one more note"))
        return SimpleNamespace(answer="", api_calls=1, model="reviewer", provider="test",
                               toolsets=(), messages=tuple(kwargs.get("history") or ()))

    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake)
    _event(provider, "second")

    assert mode.review_once(SESSION, "test") is True

    row = provider._store.adviser_reviews(SESSION)[0]
    assert row["evicted_chars"] > 4000
    assert row["cache_epoch"] == 1


def test_a_long_assistant_message_never_moves_the_start(plugin_package, make_provider):
    """T4.7: without a total budget the history is never cut at a start boundary."""
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    rows = [{"role": "user", "content": "review turn 1"},
            {"role": "assistant", "content": "w" * 6000},
            {"role": "user", "content": "review turn 2"},
            {"role": "assistant", "content": "advice"}]
    runtime = plugin_package.adviser_mode.AdvisorRuntime(session=SESSION, adviser="default")

    trimmed = mode._trim_history(rows, runtime)

    assert trimmed == rows
    assert runtime.cache_epoch == 0


def test_reasoning_rides_the_event_it_led_to(plugin_package, make_provider):
    """T7.2: the delta since the last event, as that block's `_thinking:_` line."""
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    mode.on_stream_delta(SESSION, "private thought")
    mode.record_tool(SESSION, "read_file", turn_id="t1", status="ok",
                     args={"path": "a.py"}, result="contents")

    block = plugin_package.adviser_mode._render_event(dict(mode._ring(SESSION)[-1]))

    assert "_thinking:_ private thought" in block
    # The delta does not carry over: the next event starts with no reasoning of its own.
    mode.record_tool(SESSION, "read_file", turn_id="t1", status="ok",
                     args={"path": "b.py"}, result="contents")
    assert "_thinking:" not in plugin_package.adviser_mode._render_event(
        dict(mode._ring(SESSION)[-1]))

    # Bounded in the buffer, not in the prompt: the default keeps the last 8,000 characters.
    mode.on_stream_delta(SESSION, "y" * 9_000)
    assert len(mode._reasoning_since[SESSION]) == 8_000

    capped = _provider(make_provider, max_reasoning_chars=100)
    capped._adviser_mode.on_stream_delta(SESSION, "z" * 250)
    assert len(capped._adviser_mode._reasoning_since[SESSION]) == 100

    prompt = mode._prompt(SESSION, "t1", "manual", [{"seq": 1, "kind": "intent", "text": "go"}])
    assert "<recent_reasoning>" not in prompt, "reasoning rides the events, not a block"


def test_reasoning_can_be_switched_off(plugin_package, make_provider):
    off = _provider(make_provider, include_reasoning=False)
    off._adviser_mode.on_stream_delta(SESSION, "private thought")
    assert SESSION not in off._adviser_mode._reasoning_since


def test_plan_mode_tool_state_is_mirrored(plugin_package, make_provider):
    provider = _provider(make_provider)
    mode = provider._adviser_mode

    mode._note_plan_mode(SESSION, {"action": "on"}, "")
    assert mode._plan_mode[SESSION] is True
    # A submitted plan stays conservative until the tool reports off.
    mode._note_plan_mode(SESSION, {"action": "submit"}, "Plan mode: on")
    assert mode._plan_mode[SESSION] is True
    mode._note_plan_mode(SESSION, {"action": "off"}, "")
    assert mode._plan_mode[SESSION] is False
    mode._note_plan_mode(SESSION, {"action": "status"}, {"message": "Plan mode: on"})
    assert mode._plan_mode[SESSION] is True
    mode._note_plan_mode(SESSION, '{"action": "on"}', "")
    assert mode._plan_mode[SESSION] is True


def test_strict_backlog_is_preserved_and_capped(plugin_package, make_provider):
    provider = _provider(make_provider, sync_backlog="strict", strict_max_ms=999_999)
    cfg = provider._adviser_mode._cfg()
    assert cfg["sync_backlog"] == "strict"
    assert cfg["strict_max_ms"] == 120_000

    provider = _provider(make_provider, sync_backlog="wait", wait_ms=999_999)
    cfg = provider._adviser_mode._cfg()
    assert cfg["sync_backlog"] == "wait"
    assert cfg["wait_ms"] == 60_000
