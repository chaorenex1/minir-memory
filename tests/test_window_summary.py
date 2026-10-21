"""The window summary template: one JSON contract, one fixed markdown shape.

The model returns JSON; the renderer writes the artifact section. The two forms must carry the
same eight keys, so a missing key fails here rather than being silently dropped from the prompt
the next base pass reads.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def summary(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.window_summary")


def _valid(**overrides):
    value = {
        "objective": "把规则接进 base 层",
        "current_state": {"done": ["规则文件已落盘"], "doing": ["加载器校验"], "todo": []},
        "decisions": [{"decision": "运行时读取", "reason": "改文件即生效"}],
        "constraints": ["规则块在 OUTPUT 之前"],
        "verified_facts": [{"fact": "预算 700 tokens", "evidence": "rule-files-draft.md 3.1"}],
        "open_issues": [],
        "next_actions": [{"action": "接线到 worker", "priority": "high"}],
        "references": [{"kind": "file", "ref": "plugin/minir/rules.py"}],
    }
    value.update(overrides)
    return value


def test_render_is_the_fixed_template(summary):
    assert summary.render(_valid()) == (
        "## Window Summary\n\n"
        "### Objective\n把规则接进 base 层\n\n"
        "### Current State\n"
        "#### Done\n- 规则文件已落盘\n\n"
        "#### Doing\n- 加载器校验\n\n"
        "#### Todo\n\n"
        "### Decisions\n- 运行时读取 — 改文件即生效\n\n"
        "### Constraints\n- 规则块在 OUTPUT 之前\n\n"
        "### Verified Facts\n- 预算 700 tokens — evidence: rule-files-draft.md 3.1\n\n"
        "### Open Issues\n\n"
        "### Next Actions\n- [high] 接线到 worker\n\n"
        "### References\n- file: plugin/minir/rules.py"
    )


def test_empty_lists_keep_every_heading(summary):
    empty = _valid(objective="", current_state={"done": [], "doing": [], "todo": []},
                   decisions=[], constraints=[], verified_facts=[], open_issues=[],
                   next_actions=[], references=[])
    text = summary.render(empty)
    for heading in ("### Objective", "### Current State", "#### Done", "#### Doing",
                    "#### Todo", "### Decisions", "### Constraints", "### Verified Facts",
                    "### Open Issues", "### Next Actions", "### References"):
        assert heading in text
    assert "N/A" not in text and "待补充" not in text


def test_a_missing_or_extra_key_is_rejected(summary):
    missing = _valid()
    missing.pop("open_issues")
    with pytest.raises(summary.WindowSummaryError, match="missing key"):
        summary.validate(missing)
    extra = _valid()
    extra["history"] = []
    with pytest.raises(summary.WindowSummaryError, match="unexpected key"):
        summary.validate(extra)


def test_current_state_requires_exactly_three_arrays(summary):
    with pytest.raises(summary.WindowSummaryError, match="missing key"):
        summary.validate(_valid(current_state={"done": [], "doing": []}))
    with pytest.raises(summary.WindowSummaryError, match="unexpected key"):
        summary.validate(_valid(current_state={"done": [], "doing": [], "todo": [],
                                               "later": []}))


def test_priority_is_required_and_enumerated(summary):
    with pytest.raises(summary.WindowSummaryError, match="priority"):
        summary.validate(_valid(next_actions=[{"action": "接线"}]))
    with pytest.raises(summary.WindowSummaryError, match="high, normal, low"):
        summary.validate(_valid(next_actions=[{"action": "接线", "priority": "urgent"}]))


def test_reference_kind_is_enumerated_and_ref_is_required(summary):
    with pytest.raises(summary.WindowSummaryError, match="file, log, config"):
        summary.validate(_valid(references=[{"kind": "memory", "ref": "x"}]))
    with pytest.raises(summary.WindowSummaryError, match="must not be empty"):
        summary.validate(_valid(references=[{"kind": "file", "ref": "  "}]))


def test_a_verified_fact_needs_its_evidence(summary):
    with pytest.raises(summary.WindowSummaryError, match="evidence"):
        summary.validate(_valid(verified_facts=[{"fact": "预算 700", "evidence": ""}]))


def test_caps_are_enforced_per_field(summary):
    with pytest.raises(summary.WindowSummaryError, match="limit is 120"):
        summary.validate(_valid(objective="x" * 121))
    with pytest.raises(summary.WindowSummaryError, match="limit is 5"):
        summary.validate(_valid(constraints=["c"] * 6))
    with pytest.raises(summary.WindowSummaryError, match="limit is 100"):
        summary.validate(_valid(constraints=["c" * 101]))
    with pytest.raises(summary.WindowSummaryError, match="limit is 80"):
        summary.validate(_valid(current_state={"done": ["d" * 81], "doing": [], "todo": []}))
    with pytest.raises(summary.WindowSummaryError, match="limit is 4"):
        summary.validate(_valid(decisions=[{"decision": "d", "reason": "r"}] * 5))


def test_placeholders_fail_but_honest_prose_passes(summary):
    with pytest.raises(summary.WindowSummaryError, match="placeholder"):
        summary.validate(_valid(open_issues=["N/A"]))
    with pytest.raises(summary.WindowSummaryError, match="placeholder"):
        summary.validate(_valid(open_issues=["unknown: provider route"]))
    assert summary.validate(_valid(open_issues=["无法复现的 flake"]))["open_issues"] == [
        "无法复现的 flake"]


def test_the_whole_summary_has_a_character_budget(summary):
    long_items = ["一项较长的约束说明" * 10] * 5
    with pytest.raises(summary.WindowSummaryError, match="880"):
        summary.validate(_valid(constraints=long_items, open_issues=long_items))


def test_render_never_truncates_it_fails(summary):
    with pytest.raises(summary.WindowSummaryError):
        summary.render(_valid(constraints=["x" * 200]))


def test_parse_strips_a_code_fence(summary):
    import json

    text = "```json\n" + json.dumps(_valid(), ensure_ascii=False) + "\n```"
    assert summary.parse(text) == summary.validate(_valid())


def test_parse_rejects_prose(summary):
    with pytest.raises(summary.WindowSummaryError, match="not valid JSON"):
        summary.parse("Here is the summary: {}")


def test_chars_counts_the_rendered_section(summary):
    value = _valid()
    assert summary.chars(value) == len(summary.render(value)) <= summary.MAX_TOTAL_CHARS
    assert summary.TEMPLATE_VERSION == 1
