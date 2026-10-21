"""The unified scheduler: turn + time tasks, one ledger, one log line per execution."""

from __future__ import annotations

import logging
import time

import pytest


@pytest.fixture()
def module(plugin_package):
    return plugin_package.scheduler


def test_turn_tasks_run_on_the_host_turn_and_record_their_result(module):
    scheduler = module.Scheduler()
    seen: list[tuple[str, int]] = []
    scheduler.add_turn("layers",
                       lambda session_id, turn_number: seen.append((session_id, turn_number))
                       or "queued=2")

    assert scheduler.on_turn("s-1", 7) == ["layers"]
    assert seen == [("s-1", 7)]
    row = scheduler.snapshot()["layers"]
    assert row["kind"] == module.TURN and row["runs"] == 1
    assert (row["last_outcome"], row["last_detail"]) == ("ok", "queued=2")


def test_a_time_task_runs_only_when_its_own_state_says_due(module):
    scheduler = module.Scheduler()
    calls: list[str] = []
    state = {"due": False}
    scheduler.add_time("card_review", 600, lambda: calls.append("run") or True,
                       due=lambda: state["due"], first_delay=0.0)

    assert scheduler.tick(now=time.monotonic() + 0.01) == []
    assert calls == []
    assert scheduler.snapshot()["card_review"]["skipped"] == 1

    state["due"] = True
    assert scheduler.tick(now=time.monotonic() + 601) == ["card_review"]
    assert calls == ["run"]
    row = scheduler.snapshot()["card_review"]
    assert (row["runs"], row["last_outcome"]) == (1, "ok")


def test_a_failing_task_is_contained_and_counted(module):
    scheduler = module.Scheduler()

    def boom():
        raise RuntimeError("model down")

    scheduler.add_time("session_extraction", 60, boom, first_delay=0.0)
    assert scheduler.tick(now=time.monotonic() + 0.01) == ["session_extraction"]
    row = scheduler.snapshot()["session_extraction"]
    assert (row["runs"], row["failures"], row["last_outcome"]) == (1, 1, "error")
    assert "model down" in row["last_detail"]


def test_false_is_a_retry_and_zero_is_a_count(module):
    scheduler = module.Scheduler()
    scheduler.add_time("card_review", 600, lambda: False, first_delay=0.0)
    scheduler.add_time("session_extraction", 60, lambda: 0, first_delay=0.0)

    scheduler.tick(now=time.monotonic() + 0.01)

    snapshot = scheduler.snapshot()
    assert snapshot["card_review"]["last_outcome"] == "retry"
    assert snapshot["card_review"]["failures"] == 1
    assert snapshot["session_extraction"]["last_outcome"] == "ok"
    assert snapshot["session_extraction"]["last_detail"] == "count=0"


def test_every_execution_logs_its_task_name(module, caplog):
    scheduler = module.Scheduler()
    scheduler.add_turn("layers", lambda session_id, turn_number: "queued=1")
    scheduler.add_time("session_extraction", 60, lambda: 1, first_delay=0.0)

    with caplog.at_level(logging.INFO):
        scheduler.on_turn("s-1", 3)
        scheduler.tick(now=time.monotonic() + 0.01)

    messages = [record.getMessage() for record in caplog.records]
    assert any("task=layers" in message and "trigger=turn" in message for message in messages)
    assert any("task=session_extraction" in message and "trigger=time" in message
               for message in messages)


def test_a_turn_only_scheduler_starts_no_clock(module):
    scheduler = module.Scheduler()
    scheduler.add_turn("layers", lambda session_id, turn_number: None)
    assert scheduler.start() is False
    assert scheduler.snapshot()["layers"]["kind"] == module.TURN


def test_the_clock_starts_and_stops(module):
    scheduler = module.Scheduler(tick=0.01)
    scheduler.add_time("session_extraction", 0.01, lambda: None, first_delay=0.0)
    assert scheduler.start() is True
    scheduler.stop()
    assert scheduler.snapshot()["session_extraction"]["every"] == pytest.approx(0.01)
