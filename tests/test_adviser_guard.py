"""Emission guard — normalization, noise, dedupe, escalation, budget."""

from __future__ import annotations

def _note(text: str, severity: str = "concern", **extra) -> dict:
    return {"content": text, "severity": severity, **extra}


def test_key_normalizes_case_punctuation_and_width(plugin_package):
    key_for = plugin_package.adviser_guard.key_for
    assert key_for("Check  the STDOUT!") == "check the stdout"
    assert key_for("ｆｕｌｌ　ｗｉｄｔｈ") == "full width"
    assert key_for("删掉 未使用 的 import。") == "删掉 未使用 的 import"


def test_noise_is_an_acknowledgement_not_advice(plugin_package):
    is_noise = plugin_package.adviser_guard.is_noise
    assert is_noise("Looks good!")
    assert is_noise("no issues")
    assert is_noise("   ")
    assert not is_noise("check stderr before retrying")
    assert not is_noise("删掉未使用的 import")


def test_duplicate_in_one_update_is_dropped(plugin_package):
    verdict = plugin_package.adviser_guard.EmissionGuard().admit(
        [_note("check the diff"), _note("Check the diff.")])
    assert [row["content"] for row in verdict.accepted] == ["check the diff"]
    assert verdict.count("duplicate") == 1


def test_repeat_of_a_ledger_note_is_dropped_but_escalation_passes(plugin_package):
    guard = plugin_package.adviser_guard.EmissionGuard()
    seen = {"check the diff": "concern"}
    repeat = guard.admit([_note("check the diff")], seen=seen)
    assert repeat.accepted == [] and repeat.count("duplicate") == 1
    escalated = guard.admit([_note("Check the diff!", "blocker")], seen=seen)
    assert [row["severity"] for row in escalated.accepted] == ["blocker"]


def test_budget_caps_non_blockers_and_blockers_are_exempt(plugin_package):
    notes = [_note(f"issue {index}") for index in range(6)]
    notes.append(_note("delete would lose the only copy", "blocker"))
    verdict = plugin_package.adviser_guard.EmissionGuard().admit(notes, budget=4)
    assert len(verdict.accepted) == 5
    assert verdict.accepted[0]["severity"] == "blocker", "blockers sort first"
    assert verdict.count("rate-limit") == 2


def test_noise_and_empty_do_not_spend_budget(plugin_package):
    verdict = plugin_package.adviser_guard.EmissionGuard().admit(
        [_note("looks good"), _note(""), _note("real issue")], budget=1)
    assert [row["content"] for row in verdict.accepted] == ["real issue"]
    assert verdict.count("noise") == 1 and verdict.count("empty") == 1


def test_cache_is_bounded_and_keeps_the_strongest_severity(plugin_package):
    guard = plugin_package.adviser_guard.EmissionGuard(capacity=2)
    guard.remember("a", "nit")
    guard.remember("b", "concern")
    guard.remember("a", "concern")
    guard.remember("c", "nit")
    assert len(guard) == 2
    assert guard.seen("a") == "concern"
    assert guard.seen("b") is None, "oldest key evicted first"
