"""Window summary: a model-written markdown section with a shape asset and prompt caps."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def summary(plugin_package):
    return importlib.reload(plugin_package.window_summary)


def test_the_heading_is_written_by_code_not_by_the_model(summary):
    """A body gets the heading; a stored section that already has it is not doubled."""
    body = "### Objective\nRestart docker"
    assert summary.render_section(body) == "## Window Summary\n\n" + body
    assert summary.render_section(_filled()) == _filled()
    assert summary.render_section("") == "## Window Summary"
    # A body that merely mentions the phrase keeps it: only the first line is the heading.
    mentioned = "### References\n- see ## Window Summary above"
    assert summary.render_section(mentioned).startswith("## Window Summary\n")
    assert summary.render_section(mentioned) != mentioned


def test_body_text_strips_only_the_heading(summary):
    assert summary.body_text(_filled()).startswith("### Objective")
    assert "## Window Summary" not in summary.body_text(_filled())
    assert summary.body_text("### Objective\n- x") == "### Objective\n- x"


def _filled() -> str:
    """One section with content under every heading, in the shape the template fixes."""
    return "\n\n".join([
        "## Window Summary",
        "### Objective\nShip the plugin",
        "### Current State\n#### Done\n- published the artifact",
        "#### Doing\n- wiring B3",
        "#### Todo",
        "### Decisions\n- runtime rules — edit without a restart",
        "### Constraints\n- rules before the output contract",
        "### Verified Facts\n- pytest passes — evidence: pytest -q",
        "### Open Issues",
        "### Next Actions\n- [high] wire B2",
        "### References\n- file: plugin/minir/layers.py",
    ])


def test_the_template_carries_the_heading_skeleton(summary):
    headings = [line for line in summary.load_template().splitlines() if line.startswith("#")]
    assert headings == [
        "## Window Summary", "### Objective", "### Current State", "#### Done", "#### Doing",
        "#### Todo", "### Decisions", "### Constraints", "### Verified Facts", "### Open Issues",
        "### Next Actions", "### References",
    ]


def test_the_template_holds_no_placeholders(summary):
    template = summary.load_template()
    assert "{" not in template and "$" not in template


def test_the_empty_section_is_the_template(summary):
    assert summary.empty_summary() == summary.load_template()
    assert summary.is_empty(summary.empty_summary())


def test_validate_accepts_a_string_and_strips_it(summary):
    assert summary.validate("  ## Window Summary\n") == "## Window Summary"
    assert summary.validate("") == ""


def test_validate_rejects_a_non_string(summary):
    for value in ({}, [], None, 0, {"objective": "x"}):
        with pytest.raises(summary.WindowSummaryError, match="markdown string"):
            summary.validate(value)


def test_is_empty_reads_the_lines_not_the_bytes(summary):
    assert summary.is_empty("## Window Summary\n\n### Objective\n")
    assert not summary.is_empty(_filled())
    assert summary.is_empty(None)


def test_a_missing_template_fails_loudly(summary, tmp_path, monkeypatch):
    monkeypatch.setattr(summary, "TEMPLATE_PATH", tmp_path / "absent.md")
    with pytest.raises(summary.WindowTemplateError, match="cannot read"):
        summary.load_template()
    assert not issubclass(summary.WindowTemplateError, summary.WindowSummaryError), \
        "a broken asset must not be retried as if the model had returned a bad value"


def test_prompt_vars_publish_the_caps(summary):
    for name in ("MAX_GOAL_CHARS", "MAX_DECISIONS", "MAX_SECTION_ITEMS",
                 "MAX_SECTION_ITEM_CHARS", "MAX_REFERENCE_CHARS", "MAX_TOTAL_CHARS"):
        assert summary.PROMPT_VARS[name] == getattr(summary, name)
    assert summary.MAX_TOTAL_CHARS == 880
    assert not hasattr(summary, "MAX_STATE_ITEMS"), \
        "the goal ledger has no item cap and no 880 share"
    assert summary.PROMPT_VARS["PRIORITIES"] == "high, normal or low"
    assert summary.PROMPT_VARS["REFERENCE_KINDS"] == "file, log, config, url, issue or other"


def test_placeholders_are_whole_values(summary):
    assert summary.is_placeholder("N/A") and summary.is_placeholder("待补充")
    assert not summary.is_placeholder("无法复现，暂无结论")


def test_for_prompt_keeps_the_goal_ledger_and_cuts_the_rest(summary):
    text = "\n\n".join([
        "## Window Summary",
        "### Objective\n" + "\n".join(f"- goal {index}" for index in range(30)),
        "### Current State\n#### Done\n#### Doing\n- goal 1\n#### Todo",
        "### Decisions\n" + "\n".join("- " + "d" * 120 for _ in range(10)),
        "### Constraints", "### Verified Facts", "### Open Issues", "### Next Actions",
        "### References"])
    bounded = summary.for_prompt(text, 800)
    assert "- goal 29" in bounded, "a goal is never dropped by the prompt budget"
    assert "[summary truncated]" in bounded
    assert bounded.count("d" * 120) < 10
    assert summary.for_prompt("### Objective\n- small", 800) == "### Objective\n- small"
