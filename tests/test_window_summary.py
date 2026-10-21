"""Window summary: a model-written markdown section with a shape asset and prompt caps."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def summary(plugin_package):
    return importlib.reload(plugin_package.window_summary)


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
    for name in ("MAX_OBJECTIVE_CHARS", "MAX_STATE_ITEMS", "MAX_STATE_ITEM_CHARS",
                 "MAX_DECISIONS", "MAX_SECTION_ITEMS", "MAX_SECTION_ITEM_CHARS",
                 "MAX_REFERENCE_CHARS", "MAX_TOTAL_CHARS"):
        assert summary.PROMPT_VARS[name] == getattr(summary, name)
    assert summary.MAX_TOTAL_CHARS == 880
    assert summary.PROMPT_VARS["PRIORITIES"] == "high, normal or low"
    assert summary.PROMPT_VARS["REFERENCE_KINDS"] == "file, log, config, url, issue or other"


def test_placeholders_are_whole_values(summary):
    assert summary.is_placeholder("N/A") and summary.is_placeholder("待补充")
    assert not summary.is_placeholder("无法复现，暂无结论")
