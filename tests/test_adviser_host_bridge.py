"""Adviser → host control bridge: steer, continuation, fallback."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

from adviser_fakes import drive_advise

SOURCE = "adviser_mode"
SESSION = "session-test"


def _advice(text: str = "steer this", severity: str = "concern") -> str:
    return json.dumps({"action": "advise", "advices": [{
        "severity": severity, "target": "tool_choice", "text": text, "evidence": ["#1"],
    }]})


def _fake_run(answer: str):
    def _run(message, **kwargs):
        drive_advise(kwargs, answer)
        return SimpleNamespace(answer=answer, api_calls=1, model="reviewer", provider="test",
                               toolsets=tuple(kwargs.get("toolsets") or ()))
    return _run


def _provider(make_provider, **overrides):
    cfg = {
        "enabled": True, "mode": "watch", "cadence": 99, "severity_floor": "concern",
        "max_reviews_per_turn": 4, "max_advices": 3, "max_chars": 900,
        "host_steer": True, "host_continue": False, "claims": "off",
    }
    cfg.update(overrides)
    return make_provider({"adviser_mode": cfg})


def _review(provider) -> None:
    provider._adviser_mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="ok")
    assert provider._adviser_mode.review_once(SESSION, "test") is True


def _patch_bridge(plugin_package, monkeypatch, **overrides):
    bridge = plugin_package.host_bridge
    calls: dict = {"steer": [], "continue": [], "interrupt": [], "post": [], "preserve": []}
    monkeypatch.setattr(bridge, "available", lambda: overrides.get("available", True))
    monkeypatch.setattr(bridge, "live", lambda session: overrides.get("live", True))
    state = overrides.get("live_state")
    monkeypatch.setattr(
        bridge, "live_state",
        lambda session: dict(state) if state is not None else {
            "live": overrides.get("live", True), "streaming": False, "aborting": False,
            "tool_batch": False, "terminal_idle": overrides.get("live", True),
        },
    )
    monkeypatch.setattr(bridge, "flags", lambda session: overrides.get("flags", {}))
    monkeypatch.setattr(
        bridge, "steer",
        lambda session, text: (
            calls["steer"].append((session, text))
            or overrides.get("steer", {"ok": True, "mode": "steer"})
        ),
    )
    monkeypatch.setattr(
        bridge, "interrupt",
        lambda session, text: (
            calls["interrupt"].append((session, text))
            or overrides.get("interrupt", {"ok": True, "mode": "interrupt"})
        ),
    )
    monkeypatch.setattr(
        bridge, "queue_post_turn",
        lambda session, text: (
            calls["post"].append((session, text))
            or overrides.get("post", {"ok": True})
        ),
    )
    monkeypatch.setattr(
        bridge, "continue_turn",
        lambda session, text, source="", token="": (
            calls["continue"].append((session, text))
            or overrides.get("continuation", {"ok": False, "token": ""})
        ),
    )
    monkeypatch.setattr(
        bridge, "continuation_status",
        lambda session, token: overrides.get("status", "unknown"),
    )
    monkeypatch.setattr(
        bridge, "preserve_handle",
        lambda session: (
            lambda text, metadata=None: (
                calls["preserve"].append((session, text, metadata))
                or overrides.get("preserve", {"ok": True, "id": "row-1"})
            )
        ) if overrides.get("preserve_available", True) else None,
    )
    return calls


def test_host_steer_removes_fresh_advice_from_the_pending_queue(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("steer this")))
    calls = _patch_bridge(plugin_package, monkeypatch)

    _review(provider)

    assert len(calls["steer"]) == 1
    assert calls["steer"][0][0] == SESSION
    assert "steer this" in calls["steer"][0][1]
    assert provider._store.pending_advisories(SESSION, source=SOURCE) == []


def test_blocker_interrupts_the_tool_batch(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(
        plugin_package.agent_loop, "run",
        _fake_run(_advice("this cannot work", severity="blocker")),
    )
    calls = _patch_bridge(plugin_package, monkeypatch)

    _review(provider)

    assert len(calls["interrupt"]) == 1
    assert "this cannot work" in calls["interrupt"][0][1]
    assert calls["steer"] == []
    assert provider._store.pending_advisories(SESSION, source=SOURCE) == []


def test_an_aborting_turn_is_left_pending(plugin_package, make_provider, monkeypatch):
    """C2: the host says a stop is unwinding the turn. Another interrupt adds nothing and a
    steer may die with the aborted request, so the note stays in the ledger."""
    provider = _provider(make_provider)
    monkeypatch.setattr(
        plugin_package.agent_loop, "run",
        _fake_run(_advice("this cannot work", severity="blocker")),
    )
    calls = _patch_bridge(plugin_package, monkeypatch, live_state={
        "live": True, "streaming": True, "aborting": True, "tool_batch": False,
        "terminal_idle": False,
    })

    _review(provider)

    assert calls["interrupt"] == [] and calls["steer"] == [] and calls["continue"] == []
    rows = provider._store.pending_advisories(SESSION, source=SOURCE)
    assert [row["content"] for row in rows] == ["this cannot work"]


def test_llm_request_does_not_inject_into_an_aborting_turn(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    _patch_bridge(plugin_package, monkeypatch, live_state={
        "live": True, "streaming": True, "aborting": True, "tool_batch": False,
        "terminal_idle": False,
    })
    provider._store.add_advisories(SESSION, [{
        "severity": "concern", "target": "current_step", "text": "check the diff first",
    }], source=SOURCE)

    result = provider._adviser_mode.handle_llm_request(
        {"messages": [{"role": "user", "content": "hi"}]},
        session_id=SESSION, api_request_id="req-1")

    assert result is None
    assert provider._store.staged_advisories("req-1") == []


def test_plan_mode_flag_suppresses_every_host_channel(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(
        plugin_package.agent_loop, "run",
        _fake_run(_advice("do not edit yet", severity="blocker")),
    )
    calls = _patch_bridge(plugin_package, monkeypatch, flags={"plan_mode": True})

    _review(provider)

    assert calls["interrupt"] == [] and calls["steer"] == [] and calls["continue"] == []
    assert calls["post"] == []
    rows = provider._store.pending_advisories(SESSION, source=SOURCE)
    assert [row["content"] for row in rows] == ["do not edit yet"]


def test_plan_mode_mirror_suppresses_every_host_channel(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(
        plugin_package.agent_loop, "run",
        _fake_run(_advice("do not edit yet", severity="blocker")),
    )
    calls = _patch_bridge(plugin_package, monkeypatch)
    provider._adviser_mode._plan_mode[SESSION] = True

    _review(provider)

    assert calls["interrupt"] == [] and calls["steer"] == [] and calls["continue"] == []
    assert calls["post"] == []
    assert provider._store.pending_advisories(SESSION, source=SOURCE)


def test_turn_end_aggregates_a_deferred_note_but_mid_turn_does_not(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    calls = _patch_bridge(plugin_package, monkeypatch)
    mode = provider._adviser_mode

    ids = provider._store.add_advisories(
        SESSION,
        [{"content": "later", "severity": "concern", "turn": 1, "deferred": True}],
        source=SOURCE)
    mode._turn_ids[SESSION] = ("t2", 2)

    assert mode._deliver_host(SESSION, ids, mode._cfg()) == 0
    assert calls["steer"] == []

    assert mode._deliver_host(SESSION, [], mode._cfg(), turn_end=True) == 1
    block = calls["steer"][0][1]
    assert "later" in block
    assert "1 turn ago" in block


def test_final_review_flushes_pending_notes_without_fresh_events(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    calls = _patch_bridge(plugin_package, monkeypatch)
    mode = provider._adviser_mode
    provider._store.add_advisories(
        SESSION, [{"content": "held note", "severity": "concern", "turn": 1}], source=SOURCE)
    mode._turn_ids[SESSION] = ("t2", 2)

    # No event is newer than the runtime cursor, so no member runs; the final aggregate must
    # still drain the pending row.
    assert mode.review_once(SESSION, "final") is False
    assert calls["steer"] and "held note" in calls["steer"][0][1]
    assert "1 turn ago" in calls["steer"][0][1]


def test_terminal_blocker_queues_a_post_turn_once(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(
        plugin_package.agent_loop, "run",
        _fake_run(_advice("wrong direction", severity="blocker")),
    )
    calls = _patch_bridge(plugin_package, monkeypatch)
    provider._adviser_mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="ok")
    # The final answer already ran: this review cannot continue that turn.
    provider._adviser_mode._terminal[SESSION] = "t1"

    assert provider._adviser_mode.review_once(SESSION, "test") is True
    assert len(calls["post"]) == 1
    assert "wrong direction" in calls["post"][0][1]

    # A second terminal review in the same session must not start another turn.
    provider._adviser_mode._terminal[SESSION] = "t2"
    provider._adviser_mode.on_post_tool_call(
        session_id=SESSION, turn_id="t2", tool_name="read_file", status="ok",
        args={"path": "b.py"}, result="ok")
    assert provider._adviser_mode.review_once(SESSION, "test") is True
    assert len(calls["post"]) == 1


def test_terminal_concern_queues_a_post_turn_by_default(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    mode = provider._adviser_mode
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("soften the claim")))
    calls = _patch_bridge(plugin_package, monkeypatch)
    assert mode._cfg()["trigger_severity"] == "concern"

    mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="ok")
    # The final answer already ran: only the post-turn channel is left.
    mode._terminal[SESSION] = "t1"

    assert mode.review_once(SESSION, "test") is True
    assert len(calls["post"]) == 1
    assert "soften the claim" in calls["post"][0][1]
    assert calls["steer"] == [] and calls["continue"] == []


def test_host_continuation_is_queued_and_reconciled_when_it_never_fires(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, host_continue=True)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("wait for the test")))
    calls = _patch_bridge(
        plugin_package, monkeypatch,
        continuation={"ok": True, "token": "tok-1"}, status="expired",
    )

    _review(provider)

    assert calls["continue"] and "wait for the test" in calls["continue"][0][1]
    assert calls["steer"] == []
    assert provider._store.pending_advisories(SESSION, source=SOURCE) == []
    assert provider._adviser_mode._host_tokens[SESSION]["token"] == "tok-1"

    # The next turn starts after the continuation's turn died without a stop gate.
    provider._adviser_mode.on_pre_llm_call(
        session_id=SESSION, turn_id="t2", user_message="next request")
    rows = provider._store.pending_advisories(SESSION, source=SOURCE)
    assert [row["status"] for row in rows] == ["pending"]
    assert SESSION not in provider._adviser_mode._host_tokens


def test_a_consumed_continuation_is_not_replayed(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, host_continue=True)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("wait for the test")))
    _patch_bridge(plugin_package, monkeypatch, continuation={"ok": True, "token": "tok-2"},
                  status="taken")
    _review(provider)

    provider._adviser_mode.on_pre_llm_call(
        session_id=SESSION, turn_id="t2", user_message="next request")
    assert provider._store.pending_advisories(SESSION, source=SOURCE) == []
    assert SESSION not in provider._adviser_mode._host_tokens


def test_an_unpatched_host_keeps_the_ledger_channel(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("ledger only")))
    calls = _patch_bridge(plugin_package, monkeypatch, available=False)

    _review(provider)

    assert calls["steer"] == [] and calls["continue"] == []
    rows = provider._store.pending_advisories(SESSION, source=SOURCE)
    assert [row["content"] for row in rows] == ["ledger only"]


def test_a_refused_host_delivery_releases_the_reservation(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("try again")))
    _patch_bridge(
        plugin_package, monkeypatch,
        steer={"ok": False, "mode": "", "reason": "host_refused"},
        continuation={"ok": False, "token": ""},
    )

    _review(provider)

    rows = provider._store.pending_advisories(SESSION, source=SOURCE)
    assert [row["content"] for row in rows] == ["try again"]


def test_advice_after_the_final_answer_is_not_queued_as_a_continuation(
        plugin_package, make_provider, monkeypatch):
    # The post-turn trigger is off here so the ledger fallback for a terminal miss stays visible.
    provider = _provider(make_provider, host_continue=True, trigger_turn=False)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("late finding")))
    calls = _patch_bridge(
        plugin_package, monkeypatch, continuation={"ok": True, "token": "tok-late"})
    provider._adviser_mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="ok")
    # The full stop gate already ran; a review that lands now cannot continue this turn.
    provider._adviser_mode._terminal[SESSION] = "t1"
    assert provider._adviser_mode.review_once(SESSION, "test") is True

    assert calls["continue"] == [] and calls["steer"] == []
    rows = provider._store.pending_advisories(SESSION, source=SOURCE)
    assert [row["content"] for row in rows] == ["late finding"]


def test_bridge_is_a_noop_when_the_host_module_is_absent(plugin_package, monkeypatch):
    bridge = plugin_package.host_bridge
    bridge.reset_cache()
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    try:
        assert bridge.capability() == ""
        assert bridge.available() is False
        assert bridge.live("session-test") is False
        assert bridge.live_state("session-test") == {}
        assert bridge.steer("session-test", "text") == {
            "ok": False, "mode": "", "reason": "bridge_unavailable"}
        assert bridge.continue_turn("session-test", "text")["ok"] is False
        assert bridge.continuation_status("session-test", "tok") == "unknown"
    finally:
        bridge.reset_cache()


def test_bridge_reports_live_state_only_when_the_host_carries_it(plugin_package, monkeypatch):
    """P-0023 is optional: an older patched host has no ``live_state``, and an empty dict is
    the signal that the caller must keep its own inference."""
    bridge = plugin_package.host_bridge
    bridge.reset_cache()
    runtime = SimpleNamespace(
        CAPABILITY_VERSION="plugin_runtime_v2",
        live_turn=lambda session: True,
        live_state=lambda session: {
            "live": True, "streaming": False, "aborting": True, "tool_batch": False,
            "terminal_idle": False},
    )
    host_pkg = types.ModuleType("hermes_cli")
    host_pkg.plugin_runtime = runtime
    monkeypatch.setitem(sys.modules, "hermes_cli", host_pkg)
    try:
        state = bridge.live_state("session-test")
        assert state["aborting"] is True and state["terminal_idle"] is False
        del runtime.live_state
        assert bridge.live_state("session-test") == {}
    finally:
        bridge.reset_cache()


def test_bridge_detects_the_patched_host_capability(plugin_package, monkeypatch):
    bridge = plugin_package.host_bridge
    bridge.reset_cache()
    runtime = SimpleNamespace(
        CAPABILITY_VERSION="plugin_runtime_v1",
        live_turn=lambda session: session == "session-test",
        turn_state=lambda session: {"live": True, "turn_id": "t1"},
        steer=lambda session, text: {"ok": True, "mode": "steer"},
        request_continuation=lambda session, text, source="", token="": {
            "ok": True, "token": "tok-1"},
        continuation_status=lambda session, token: "pending",
        max_continuations=lambda: 2,
    )
    host_pkg = types.ModuleType("hermes_cli")
    host_pkg.plugin_runtime = runtime
    monkeypatch.setitem(sys.modules, "hermes_cli", host_pkg)
    try:
        assert bridge.capability() == "plugin_runtime_v1"
        assert bridge.available() is True
        assert bridge.live("session-test") is True
        assert bridge.steer("session-test", "text")["mode"] == "steer"
        assert bridge.continue_turn("session-test", "text")["token"] == "tok-1"
        assert bridge.continuation_status("session-test", "tok-1") == "pending"
        assert bridge.max_continuations() == 2
    finally:
        bridge.reset_cache()


# -- preserve card (end-of-turn slot) ----------------------------------------


def test_post_llm_call_captures_the_preserve_writer(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider)
    monkeypatch.setattr(
        plugin_package.agent_loop, "run",
        _fake_run(json.dumps({"action": "silent"})))
    _patch_bridge(plugin_package, monkeypatch)
    mode = provider._adviser_mode

    mode.on_post_llm_call(session_id=SESSION, turn_id="t1", assistant_response="done")

    assert SESSION in mode._preserve_handles
    assert mode.wait_idle(2.0)


def test_terminal_concern_is_preserved_as_an_end_of_turn_card(
        plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, trigger_turn=False)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("left a gap")))
    # The review runs after the host unbound the turn; preserve must still work.
    calls = _patch_bridge(plugin_package, monkeypatch, live=False)
    mode = provider._adviser_mode

    mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="ok")
    mode._terminal[SESSION] = "t1"
    mode._preserve_handles[SESSION] = (
        mode._current_turn(SESSION), plugin_package.host_bridge.preserve_handle(SESSION))
    assert mode.review_once(SESSION, "test") is True

    assert calls["post"] == []
    assert len(calls["preserve"]) == 1
    _session, block, metadata = calls["preserve"][0]
    assert "left a gap" in block
    assert metadata["preserved"] is True
    assert metadata["severity"] == "concern"
    assert provider._store.pending_advisories(SESSION, source=SOURCE) == []


def test_a_stale_preserve_writer_is_not_used(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, trigger_turn=False)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("late card")))
    calls = _patch_bridge(plugin_package, monkeypatch, live=False)
    mode = provider._adviser_mode

    mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="ok")
    mode._terminal[SESSION] = "t1"
    # Captured for an older turn: appending it would put a previous turn's card after a
    # newer user message.
    mode._preserve_handles[SESSION] = (
        mode._current_turn(SESSION) - 1, plugin_package.host_bridge.preserve_handle(SESSION))
    assert mode.review_once(SESSION, "test") is True

    assert calls["preserve"] == []
    assert provider._store.pending_advisories(SESSION, source=SOURCE)


def test_a_refused_preserve_keeps_the_rows_pending(plugin_package, make_provider, monkeypatch):
    provider = _provider(make_provider, trigger_turn=False)
    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run(_advice("keep me")))
    calls = _patch_bridge(plugin_package, monkeypatch, preserve={"ok": False}, live=False)
    mode = provider._adviser_mode

    mode.on_post_tool_call(
        session_id=SESSION, turn_id="t1", tool_name="read_file", status="ok",
        args={"path": "a.py"}, result="ok")
    mode._terminal[SESSION] = "t1"
    mode._preserve_handles[SESSION] = (
        mode._current_turn(SESSION), plugin_package.host_bridge.preserve_handle(SESSION))
    assert mode.review_once(SESSION, "test") is True

    assert len(calls["preserve"]) == 1
    rows = provider._store.pending_advisories(SESSION, source=SOURCE)
    assert [row["content"] for row in rows] == ["keep me"]
