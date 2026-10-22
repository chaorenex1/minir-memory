"""Review statistics: coverage denominators, advice/delivery counts, recurrence ordering."""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


def _review(store, session: str, coverage: dict[str, str],
            dropped: dict[str, int] | None = None) -> int:
    review_id = store.start_adviser_review(session, "t1", "tool", 0, 1)
    store.finish_adviser_review(
        review_id, status="advised", coverage=json.dumps(coverage),
        drop_detail=json.dumps(dropped or {}))
    return review_id


def test_counts_coverage_advice_delivery_and_recurrence(plugin_package, store):
    targets = importlib.import_module(f"{plugin_package.__name__}.prompting").CONSISTENCY_TARGETS
    review_id = _review(store, "s1", {"user_preferences_check": "clean",
                                      "soul_craft_check": "found"})
    ids = store.add_advisories("s1", [
        {"content": "preference advice", "target": "user_preferences_check",
         "severity": "concern"},
        {"content": "craft advice", "target": "soul_craft_check", "severity": "nit"},
    ], source="adviser_mode", review_id=review_id)
    store.mark_advisories_delivered(ids)
    _review(store, "s1", {"user_preferences_check": "found"},
            {"duplicate/user_preferences_check": 2})

    rows = {row["target"]: row for row in store.review_stats("s1", 8, targets)}

    preference = rows["user_preferences_check"]
    assert (preference["reviews"], preference["clean"], preference["found"]) == (2, 1, 1)
    assert (preference["adv"], preference["deliv"], preference["recur"]) == (1, 1, 2)
    assert preference["follow_up"] == "recurred"

    craft = rows["soul_craft_check"]
    assert craft["adv"] == 1 and craft["deliv"] == 1
    assert craft["follow_up"] == "not_reobserved"

    untouched = rows["soul_tool_policy_check"]
    assert untouched["reviews"] == 0 and untouched["follow_up"] == "no_followup"
    # Safety targets sort ahead of the rest.
    assert list(rows)[0] == "soul_tool_policy_check"


def test_window_limits_the_review_denominator(plugin_package, store):
    targets = importlib.import_module(f"{plugin_package.__name__}.prompting").CONSISTENCY_TARGETS
    _review(store, "s1", {"soul_craft_check": "clean"})
    _review(store, "s1", {"soul_craft_check": "found"})
    _review(store, "s1", {"soul_craft_check": "found"})

    rows = store.review_stats("s1", 2, targets)
    row = next(item for item in rows if item["target"] == "soul_craft_check")
    assert (row["reviews"], row["clean"], row["found"]) == (2, 0, 2)


def test_no_targets_means_no_rows(store):
    assert store.review_stats("s1", 8, ()) == []
