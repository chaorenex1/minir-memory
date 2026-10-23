"""Adviser history, read-only review, and roster cadence (no database rows)."""

from __future__ import annotations

import json
from types import SimpleNamespace

SESSION = "session-test"


def _advice(text: str) -> str:
    return json.dumps({"action": "advise", "advices": [{
        "severity": "concern", "target": "tool_choice", "text": text, "evidence": ["#1"],
    }]})


def _fake_run(answer: str, *, messages=None, usage=None):
    def _run(message, **kwargs):
        prior = tuple(kwargs.get("history") or ())
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


def test_reasoning_is_captured_by_default_and_bounded(plugin_package, make_provider):
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    mode.on_stream_delta(SESSION, "private thought")
    assert mode._reasoning[SESSION] == "private thought"

    off = _provider(make_provider, include_reasoning=False)
    off_mode = off._adviser_mode
    off_mode.on_stream_delta(SESSION, "private thought")
    assert SESSION not in off_mode._reasoning

    mode.on_stream_delta(SESSION, "x" * 9000)
    assert len(mode._reasoning[SESSION]) == 15 + 9000

    capped = _provider(make_provider, max_reasoning_chars=100)
    capped._adviser_mode.on_stream_delta(SESSION, "y" * 250)
    assert len(capped._adviser_mode._reasoning[SESSION]) == 100

    prompt = mode._prompt(
        SESSION, "t1", "test",
        [{"seq": 1, "kind": "tool", "text": "read_file"}],
        {"name": "default", "instructions": ""},
    )
    assert "<recent_reasoning>" in prompt
    assert "x" * 9000 in prompt


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
