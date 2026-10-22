"""SOUL constraint sections: mapping, parsing, cache and unreadable-file behavior."""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def soul(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.soul_rules")


def _write(home, text: str, *, bump: float = 0.0):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "SOUL.md"
    path.write_text(text, encoding="utf-8")
    if bump:
        os.utime(path, (bump, bump))
    return path


def test_sections_are_mapped_per_target(soul, tmp_path):
    home = tmp_path / "home"
    _write(home, "# Role\nrole\n\n# Precedence\nprecedence line\n\n# Tool policy\ntool line\n\n"
                 "# Boundaries\nboundary line\n\n# Workflow\nworkflow line\n\n"
                 "# Craft\ncraft line\n")

    tool = soul.sections_for(home, "soul_tool_policy_check")
    assert "# Precedence" in tool and "# Tool policy" in tool and "# Boundaries" in tool
    assert "workflow line" not in tool and "# Craft" not in tool

    workflow = soul.sections_for(home, "soul_workflow_check")
    assert "# Workflow" in workflow and "boundary line" not in workflow

    craft = soul.sections_for(home, "soul_craft_check")
    assert craft.startswith("# Craft") and "craft line" in craft
    assert "precedence line" not in craft


def test_missing_soul_disables_every_target(soul, tmp_path):
    home = tmp_path / "absent"
    assert soul.load(home) is None
    assert soul.available(home) is False
    assert soul.block(home) == ""
    assert soul.sections_for(home, "soul_craft_check") == ""


def test_cache_follows_the_file_stamp(soul, tmp_path):
    home = tmp_path / "home"
    _write(home, "# Craft\nfirst\n", bump=1_000_000.0)
    first = soul.load(home)
    assert first is not None and "first" in first[0]["Craft"]

    _write(home, "# Craft\nsecond\n", bump=1_000_100.0)
    second = soul.load(home)
    assert second is not None and "second" in second[0]["Craft"]
    assert first[1] != second[1]


SKILL_REL = "skills/agent-workflows/hermes-dispatch/SKILL.md"


def _write_skill(home, text: str):
    path = home / SKILL_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_dispatch_skill_supplies_execution_sections(soul, tmp_path):
    home = tmp_path / "home"
    _write(home, "# Role\nrole\n\n# Boundaries\nboundary line\n")
    _write_skill(home, "# Workflow\nworkflow from skill\n\n# Craft\ncraft from skill\n")

    workflow = soul.sections_for(home, "soul_workflow_check")
    assert "workflow from skill" in workflow and "boundary line" not in workflow

    craft = soul.sections_for(home, "soul_craft_check")
    assert craft.startswith("# Craft") and "craft from skill" in craft
    assert soul.available(home) is True


def test_soul_wins_over_the_skill_for_the_same_heading(soul, tmp_path):
    home = tmp_path / "home"
    _write(home, "# Workflow\nworkflow from soul\n")
    _write_skill(home, "# Workflow\nworkflow from skill\n")

    merged = soul.load(home)
    assert merged is not None
    assert merged[0]["Workflow"] == "workflow from soul"


def test_missing_skill_keeps_soul_sections_only(soul, tmp_path):
    home = tmp_path / "home"
    _write(home, "# Critical\ncritical line\n")

    assert "critical line" in soul.sections_for(home, "soul_workflow_check")
    assert soul.sections_for(home, "soul_craft_check") == ""
    assert soul.available(home) is True
