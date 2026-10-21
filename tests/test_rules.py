"""Rule files: text assets read at call time and passed to a pass untransformed.

The loader only reads the body: no front-matter parsing, no section slicing, no token budget. These
tests pin the two failures that are invisible at runtime: a rule that cannot be read, and an
override that would silently replace the packaged text with nothing.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def rules(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.rules")


def _packaged_ids(rules) -> list[str]:
    return sorted(path.stem for path in rules.PACKAGE_RULES_DIR.glob("*.md"))


def test_the_six_packaged_rules_load_without_front_matter(rules):
    assert _packaged_ids(rules) == [
        "ai-identity", "ai-self", "markers-category", "markers-org",
        "user-preferences", "user-self",
    ]
    for rule_id in _packaged_ids(rules):
        text = rules.rule_text(rule_id)
        assert text and not text.startswith("---"), rule_id
        assert "CONTRACT" in text and "REVIEW CHECKS" in text, rule_id


def test_a_missing_rule_is_an_error_not_an_empty_rule(rules):
    with pytest.raises(rules.RulesError, match="missing"):
        rules.rule_text("no-such-rule")


def test_an_override_wins_over_the_packaged_file(rules, tmp_path):
    (tmp_path / "user-self.md").write_text(
        "---\nid: user-self\n---\n\n## CONTRACT\n\nOVERRIDE BODY\n", encoding="utf-8")
    text = rules.rule_text("user-self", override_dir=tmp_path)
    assert "OVERRIDE BODY" in text
    assert not text.startswith("---")


def test_an_unusable_override_falls_back_to_the_packaged_file(rules, tmp_path):
    (tmp_path / "user-self.md").mkdir()  # a directory where the file should be
    text = rules.rule_text("user-self", override_dir=tmp_path)
    assert "CONTRACT" in text, "an unreadable override must not empty the rule"


def test_the_profile_rules_dir_is_the_default_override(rules, tmp_path):
    override = tmp_path / "minir-memory" / "rules"
    override.mkdir(parents=True)
    (override / "ai-self.md").write_text("## CONTRACT\n\nPROFILE OVERRIDE\n", encoding="utf-8")
    assert "PROFILE OVERRIDE" in rules.rule_text("ai-self", hermes_home=str(tmp_path))


def test_the_env_var_names_the_override_dir(rules, tmp_path, monkeypatch):
    (tmp_path / "user-preferences.md").write_text("## CONTRACT\n\nENV OVERRIDE\n",
                                                  encoding="utf-8")
    monkeypatch.setenv(rules.OVERRIDE_ENV, str(tmp_path))
    assert "ENV OVERRIDE" in rules.rule_text("user-preferences")


def test_a_file_without_front_matter_is_passed_as_written(rules, tmp_path):
    (tmp_path / "user-self.md").write_text("## CONTRACT\n\nRAW TEXT\n", encoding="utf-8")
    assert rules.rule_text("user-self", override_dir=tmp_path) == "## CONTRACT\n\nRAW TEXT"


def test_tokens_are_estimated_for_the_page_budgets(rules):
    assert rules.estimate_tokens("") == 0
    assert rules.estimate_tokens("abcd") == 2
    assert rules.estimate_tokens("知识") == 3
    assert rules.estimate_tokens("知识abcd") == 4
