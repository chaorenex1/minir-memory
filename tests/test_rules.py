"""Rule files and the assembly contract they feed.

The six packaged files are prompt assets: a pass receives its field contract through a ``<rules>``
block read at call time, not through a constant compiled into the module. These tests pin the two
failures that are invisible at runtime: a rule that no longer parses, and a rule set that silently
outgrows the layer's token budget.
"""

from __future__ import annotations

import importlib
import shutil

import pytest


@pytest.fixture()
def rules(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.rules")


def _packaged_ids(rules) -> list[str]:
    return sorted(path.stem for path in rules.PACKAGE_RULES_DIR.glob("*.md"))


def test_the_six_packaged_rules_parse_with_both_sections(rules):
    assert _packaged_ids(rules) == [
        "ai-identity", "ai-self", "markers-category", "markers-org",
        "user-preferences", "user-self",
    ]
    for rule_id in _packaged_ids(rules):
        rule = rules.parse_rule(rules.PACKAGE_RULES_DIR / f"{rule_id}.md")
        assert rule.id == rule_id
        assert set(rule.sections) == {"contract", "review_checks"}
        assert rule.sections["contract"].strip()
        assert rule.sections["review_checks"].strip()
        assert rule.applies_to, rule_id
        assert rule.slot == "fields"


def test_every_pass_assembles_core_rules_then_output(rules):
    loader = rules.RulesLoader()
    for (layer, pass_id), contract in rules.PASS_CONTRACTS.items():
        system, digest = rules.build_system(
            layer, pass_id, contract.consumer, core="CORE", output="OUTPUT", loader=loader)
        assert system.startswith("CORE\n\n")
        assert system.rstrip().endswith("OUTPUT")
        first_rule = system.index("<rules id=")
        last_output = system.rindex("OUTPUT")
        assert first_rule < last_output
        assert system.count("<rules id=") == len(contract.rules)
        assert len(digest) == 16 and digest == digest.lower()


def test_the_contract_lists_only_layers_that_exist_in_code(rules):
    assert set(rules.PASS_CONTRACTS) == {
        ("base", "B1"), ("base", "B2"), ("base", "B3"), ("dialectic", "review"),
        ("card_review", "review"),
    }
    assert rules.PASS_CONTRACTS[("base", "B1")].consumer == rules.CONTRACT
    assert rules.PASS_CONTRACTS[("dialectic", "review")].consumer == rules.REVIEW_CHECKS
    assert rules.PASS_CONTRACTS[("card_review", "review")].consumer == rules.CONTRACT


def test_a_consumer_cannot_be_swapped_for_another_pass(rules):
    with pytest.raises(rules.RulesError, match="reads contract"):
        rules.build_system("base", "B1", "review_checks", core="C", output="O")


def test_an_unknown_pass_is_an_error_not_an_empty_prompt(rules):
    with pytest.raises(rules.RulesError, match="no rule contract"):
        rules.build_system("base", "B9", "contract", core="C", output="O")


def test_a_rule_that_does_not_apply_to_the_layer_is_rejected(rules):
    loader = rules.RulesLoader()
    with pytest.raises(rules.RulesError, match="does not apply"):
        loader.load("markers-category", "contract", layer="dialectic")


def test_the_hash_changes_when_a_rule_version_changes(rules, tmp_path):
    packaged = rules.RulesLoader()
    _, before = rules.build_system("base", "B1", "contract", core="C", output="O",
                                   loader=packaged)
    for path in rules.PACKAGE_RULES_DIR.glob("*.md"):
        shutil.copy(path, tmp_path / path.name)
    user_self = tmp_path / "user-self.md"
    text = user_self.read_text(encoding="utf-8").replace("version: 1", "version: 2", 1)
    user_self.write_text(text, encoding="utf-8")
    _, after = rules.build_system("base", "B1", "contract", core="C", output="O",
                                  loader=rules.RulesLoader(default_dir=tmp_path))
    assert before != after


def test_a_missing_packaged_rule_fails_the_pass(rules, tmp_path):
    with pytest.raises(rules.RulesError, match="missing"):
        rules.RulesLoader(default_dir=tmp_path).load("user-self", "contract")


def test_an_invalid_override_falls_back_to_the_packaged_file(rules, tmp_path, caplog):
    (tmp_path / "user-self.md").write_text("not a rule file\n", encoding="utf-8")
    loader = rules.RulesLoader(override_dir=tmp_path)
    section = loader.load("user-self", "contract", layer="base:B1")
    assert section.path == rules.PACKAGE_RULES_DIR / "user-self.md"
    assert "override" in caplog.text.lower()


def test_a_valid_override_wins(rules, tmp_path):
    override = tmp_path / "user-self.md"
    shutil.copy(rules.PACKAGE_RULES_DIR / "user-self.md", override)
    override.write_text(override.read_text(encoding="utf-8").replace(
        "self-contained", "self-contained and terse", 1), encoding="utf-8")
    section = rules.RulesLoader(override_dir=tmp_path).load("user-self", "contract")
    assert section.path == override
    assert "self-contained and terse" in section.text


def test_an_override_keeps_the_packaged_layer_scope(rules, tmp_path):
    shutil.copy(rules.PACKAGE_RULES_DIR / "markers-category.md", tmp_path / "markers-category.md")
    section = rules.RulesLoader(override_dir=tmp_path).load(
        "markers-category", "contract", layer="base:B3")
    assert section.path == tmp_path / "markers-category.md"
    with pytest.raises(rules.RulesError, match="does not apply"):
        rules.RulesLoader(override_dir=tmp_path).load(
            "markers-category", "contract", layer="dialectic")


def test_a_rule_body_over_its_own_limit_is_rejected(rules, tmp_path):
    source = (rules.PACKAGE_RULES_DIR / "user-self.md").read_text(encoding="utf-8")
    (tmp_path / "user-self.md").write_text(
        source.replace("max_tokens: 350", "max_tokens: 10", 1), encoding="utf-8")
    with pytest.raises(rules.RulesError, match="tokens"):
        rules.RulesLoader(default_dir=tmp_path).load("user-self", "contract")


def test_a_front_matter_id_must_match_the_filename(rules, tmp_path):
    shutil.copy(rules.PACKAGE_RULES_DIR / "user-self.md", tmp_path / "renamed.md")
    with pytest.raises(rules.RulesError, match="does not match the filename"):
        rules.RulesLoader(default_dir=tmp_path).load("renamed", "contract")


def test_unchanged_files_are_parsed_once(rules, monkeypatch):
    calls = {"count": 0}
    original = rules.parse_rule

    def counting(path):
        calls["count"] += 1
        return original(path)

    monkeypatch.setattr(rules, "parse_rule", counting)
    loader = rules.RulesLoader()
    first = loader.load("user-self", "contract")
    second = loader.load("user-self", "contract")
    assert calls["count"] == 1
    assert first == second


def test_token_estimate_treats_cjk_per_character(rules):
    assert rules.estimate_tokens("") == 0
    assert rules.estimate_tokens("abcd") == 2
    assert rules.estimate_tokens("知识") == 3
    assert rules.estimate_tokens("知识abcd") == 4


def test_the_profile_rules_dir_is_the_override_location(rules, tmp_path, monkeypatch):
    monkeypatch.delenv("MINIR_RULES_DIR", raising=False)
    loader = rules.RulesLoader(hermes_home=str(tmp_path))
    assert loader.override_dir == tmp_path / "minir-memory" / "rules"
    monkeypatch.setenv("MINIR_RULES_DIR", str(tmp_path / "elsewhere"))
    assert rules.RulesLoader(hermes_home=str(tmp_path)).override_dir == tmp_path / "elsewhere"
