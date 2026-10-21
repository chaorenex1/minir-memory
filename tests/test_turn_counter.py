"""Base / dialectic layer counting — cadence, windows, and session boundaries.

The tracker is pure bookkeeping with an injectable clock, so these tests drive it
directly instead of sleeping. Provider-level wiring is covered in ``test_provider.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def tracker(plugin_package):
    """Base every 3 turns, dialectic every 2 — small enough to exercise in a few ticks."""
    return plugin_package.turn_counter.TurnTracker(base_interval=3, dialectic_interval=2,
                                                   default_mode="chat")


def _tick(tracker, session, count, *, step=1.0, first=1):
    """Feed *count* consecutive host turn numbers, one second apart; return the triggers."""
    triggers = []
    for index, turn in enumerate(range(first, first + count)):
        triggers.extend(tracker.tick(session, turn, now=(index + 1) * step))
    return triggers


def test_each_layer_fires_on_its_own_cadence(tracker):
    triggers = _tick(tracker, "s", 6)
    assert [(t.layer, t.turns) for t in triggers] == [
        ("dialectic", 2), ("base", 3), ("dialectic", 4), ("base", 6), ("dialectic", 6)]


def test_the_default_schedule_fires_base_at_24_and_review_every_8(plugin_package):
    """The layer draft's table: B=24, D=8, same-turn base first."""
    tracker = plugin_package.turn_counter.TurnTracker()
    assert (tracker.base_interval, tracker.dialectic_interval) == (24, 8)
    fired: dict[int, list[str]] = {}
    for turn in range(1, 49):
        for trigger in tracker.tick("s", turn, now=float(turn)):
            fired.setdefault(trigger.turns, []).append(trigger.layer)
    assert sorted(fired[24]) == ["base", "dialectic"], "same turn: base first"
    assert fired[24] == ["base", "dialectic"]
    assert sorted(fired[8]) == ["dialectic"] and sorted(fired[16]) == ["dialectic"]
    assert sorted(fired[48]) == ["base", "dialectic"]


def test_windows_are_edge_to_edge(tracker):
    """A gap between two windows would drop turns; an overlap would re-reason them."""
    triggers = _tick(tracker, "s", 6)
    base = [t for t in triggers if t.layer == "base"]
    assert [t.to_ts for t in base] == [3.0, 6.0]
    assert base[1].from_ts == base[0].to_ts
    assert base[0].from_ts == tracker.snapshot("s")["started_at"]


def test_boundary_tail_is_skipped_when_the_cadence_flushed_it(tracker):
    """The boundary flushes only the layer that still owes turns.

    Turn 4 is covered by the dialectic's turn-4 job, but the base layer last fired on turn 3:
    the boundary owns that layer's uncovered turn and nothing else.
    """
    _tick(tracker, "s", 4)
    assert [(t.layer, t.turns, t.from_ts, t.to_ts) for t in tracker.tail("s", now=99.0, close=True)] \
        == [("base", 4, 3.0, 99.0)]
    assert tracker.snapshot("s")["closed"] is True


def test_tail_flushes_uncovered_turns_exactly_once(tracker):
    _tick(tracker, "s", 5)  # base fired at 3; dialectic at 2 and 4
    triggers = tracker.tail("s", now=9.0)
    assert [(t.layer, t.turns, t.from_ts, t.to_ts) for t in triggers] == [
        ("base", 5, 3.0, 9.0), ("dialectic", 5, 4.0, 9.0)]
    assert tracker.tail("s", now=10.0) == []  # same boundary, second hook


def test_stop_freezes_counting(tracker):
    _tick(tracker, "s", 2)
    tracker.stop("s")
    assert tracker.tick("s", 3, now=5.0) == []
    assert tracker.snapshot("s")["closed"] is True


def test_tail_still_flushes_after_a_stop(tracker):
    """CLI ``/new`` stops the session (``on_session_finalize``) before the queued
    ``on_session_end`` flushes the tail: the stop must not swallow the tail."""
    _tick(tracker, "s", 5)  # base fired at 3; dialectic at 2 and 4
    tracker.stop("s")
    assert [t.turns for t in tracker.tail("s", now=9.0, close=True)] == [5, 5]
    assert tracker.tail("s", now=10.0) == []  # watermark, not the stop, keeps it exactly-once


def test_begin_reopens_a_stopped_session(tracker):
    """A re-initialized session id is live again; ``tick`` alone must never resurrect it."""
    _tick(tracker, "s", 2)
    tracker.stop("s")
    tracker.begin("s", now=5.0)
    assert [t.turns for t in tracker.tick("s", 3, now=6.0)] == [3]  # base due; dialectic fired at 2


def test_a_coarse_clock_still_moves_the_window_edge(tracker):
    """Windows are keyed by their exact ``(from_ts, to_ts)`` pair in the store, and Windows'
    clock granularity (~15 ms on the host's Python 3.11) can be wider than the gap between two
    dispatches. A repeated edge would make the store collapse the second job into the first."""
    triggers = [trigger for turn in range(1, 7)
                for trigger in tracker.tick("s", turn, now=5.0) if trigger.layer == "base"]
    assert [trigger.turns for trigger in triggers] == [3, 6]
    assert triggers[0].from_ts < triggers[0].to_ts
    assert triggers[0].to_ts == triggers[1].from_ts  # edge-to-edge, no gap and no overlap
    assert triggers[1].from_ts < triggers[1].to_ts


def test_sessions_are_counted_independently(tracker):
    assert _tick(tracker, "a", 3)
    assert tracker.snapshot("b") is None
    assert [t.session_id for t in tracker.tick("b", 1, now=4.0)] == []


def test_branch_inherits_parent_progress(tracker):
    _tick(tracker, "parent", 4)  # dialectic fired at 2 and 4
    branch = tracker.inherit("parent", "branch", now=5.0)
    assert branch.layers["dialectic"].turns == 4
    assert tracker.tick("branch", 5, now=6.0) == []  # 5 - 4 < 2: nothing new to reason over
    dialectic = [t for t in tracker.tick("branch", 6, now=7.0) if t.layer == "dialectic"]
    assert [(t.turns, t.from_ts) for t in dialectic] == [(6, 4.0)]


def test_host_turn_number_moves_the_counter_forward(tracker):
    """A resume rehydrates the host count past ours; adopting it must not double-count."""
    tracker.tick("s", 1, now=1.0)
    assert [t.turns for t in tracker.tick("s", 7, now=2.0)] == [7, 7]
    assert tracker.snapshot("s")["last_turn_number"] == 7


def test_intervals_are_clamped_and_mode_applied(plugin_package):
    tracker = plugin_package.turn_counter.TurnTracker(base_interval=0, dialectic_interval=-3,
                                                     default_mode="coding")
    assert tracker.begin("s", now=1.0).mode == "coding"
    assert [(t.layer, t.turns) for t in tracker.tick("s", 1, now=2.0)] == [("base", 1), ("dialectic", 1)]


def test_session_state_is_bounded(plugin_package):
    tracker = plugin_package.turn_counter.TurnTracker()
    for index in range(plugin_package.turn_counter._MAX_SESSIONS):
        tracker.begin(f"s{index}", now=1.0)
        tracker.stop(f"s{index}")
    tracker.begin("fresh", now=2.0)
    assert tracker.snapshot("s0") is None  # oldest closed session evicted
    assert tracker.snapshot("fresh") is not None


def test_speculative_sessions_evict_before_a_counting_one(plugin_package):
    """A subagent's ``on_session_start`` opens an entry that is never ticked (only the bound
    primary session gets ``on_turn_start``); the cap must drop those first, never a live count."""
    tracker = plugin_package.turn_counter.TurnTracker(base_interval=50, dialectic_interval=50)
    tracker.begin("primary", now=1.0)
    tracker.tick("primary", 1, now=2.0)
    for index in range(plugin_package.turn_counter._MAX_SESSIONS - 1):
        tracker.begin(f"child-{index}", now=3.0)
    tracker.begin("one-more", now=4.0)  # over the cap: one entry has to go
    assert tracker.snapshot("primary") is not None
    assert tracker.snapshot("child-0") is None
