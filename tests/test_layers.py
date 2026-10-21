"""Layer contracts: the field validation that must fail before an artifact is published."""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def layers(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.layers")


def _unit(**over):
    unit = {"id": "k1", "org": "experiences", "category": "KNOWLEDGE", "epistemic": "fact",
            "topic": "pytest verification", "content": "pytest is the verify command.",
            "time": "2026-10-21", "relations": [], "objects": []}
    unit.update(over)
    return unit


def _review(layers, **over):
    value = {
        "user_card": {"user_self_representation": layers.empty_user_self_representation(),
                      "user_preferences": layers.empty_user_preferences()},
        "ai_card": {"ai_self_representation": layers.empty_ai_self_representation(),
                    "ai_identity": layers.empty_ai_identity()},
        "window_summary": "## Window Summary",
        "base_changed": {"changed": False, "fields": []},
        "changes": [], "prunings": [],
    }
    value.update(over)
    return value


def test_an_identity_body_is_accepted_as_markdown(layers):
    body = "# Identity\n\n- Name: minir\n- Version: 9.9"
    assert layers.validate_ai_identity(body) == body
    assert layers.validate_ai_identity("") == "", "an empty body is a valid empty card"
    with pytest.raises(layers.LayerContractError, match="must be a string"):
        layers.validate_ai_identity({"name": "minir"})


def test_a_knowledge_unit_needs_an_epistemic_value(layers):
    unit = _unit()
    unit.pop("epistemic")
    with pytest.raises(layers.LayerContractError, match="KNOWLEDGE requires"):
        layers.validate_value_markers({"value_markers": [unit]})


def test_a_non_knowledge_unit_must_not_carry_an_epistemic_value(layers):
    with pytest.raises(layers.LayerContractError, match="only KNOWLEDGE"):
        layers.validate_value_markers({"value_markers": [_unit(category="METHOD", epistemic="fact")]})


def test_a_unit_needs_one_known_category(layers):
    with pytest.raises(layers.LayerContractError, match="must be one of KNOWLEDGE"):
        layers.validate_value_markers({"value_markers": [_unit(category="OTHER")]})


def test_a_relation_must_target_a_unit_in_the_same_window(layers):
    unit = _unit(relations=[{"type": "caused_by", "ref": "k9"}])
    with pytest.raises(layers.LayerContractError, match="not in this window"):
        layers.validate_value_markers({"value_markers": [unit]})


def test_objects_must_be_typed(layers):
    with pytest.raises(layers.LayerContractError, match='"type:name"'):
        layers.validate_value_markers({"value_markers": [_unit(objects=["Docker"])]})


def test_units_merge_on_category_org_and_topic(layers):
    previous = [_unit(content="old evidence")]
    merged = layers.validate_value_markers({"value_markers": [_unit(content="new evidence")]}, previous=previous)
    assert len(merged) == 1 and merged[0]["content"] == "new evidence"


def test_a_review_cannot_prune(layers):
    with pytest.raises(layers.LayerContractError, match="may not prune"):
        layers.validate_review(_review(layers, prunings=[{"part": "user_self_representation"}]))


def test_a_review_that_claims_a_change_must_list_the_part(layers):
    with pytest.raises(layers.LayerContractError, match="fields is empty"):
        layers.validate_review(_review(layers, base_changed={"changed": True, "fields": []}))


def test_a_change_scope_must_match_its_part(layers):
    change = {"scope": "ai_card", "part": "user_self_representation", "op": "update",
              "reason": "moved", "sessions": []}
    with pytest.raises(layers.LayerContractError, match="does not match part"):
        layers.validate_review(_review(layers, changes=[change]))


def test_the_preset_is_a_markdown_seed_and_placeholders_are_refused(layers):
    seed = layers.preset_user_self_representation("en")
    assert "Software Development Engineer" in seed
    assert layers.validate_user_self_representation(seed) == seed
    assert "软件开发工程师" in layers.preset_user_self_representation("zh")
    with pytest.raises(layers.LayerContractError, match="placeholder"):
        layers.validate_user_self_representation("N/A")


def test_the_b3_core_is_a_text_asset(layers):
    """A missing prompt asset fails the pass; there is no empty-prompt fallback."""
    core = layers.load_core("b3_window_summary")
    assert core.startswith("PROMPT_VERSION: 1.0.0\n")
    assert "You close one base refresh" in core
    assert "<window_template>" in core
    assert "value_markers" not in core and "value_markers" not in core.lower(), \
        "markers belong to the value_markers pass and the output contract"
    with pytest.raises(layers.AgentPromptError, match="cannot read agent prompt"):
        layers.load_core("no_such_pass")


PROMPT_ASSETS = ("b1_user_card", "b2_ai_card", "b3_window_summary", "b4_value_markers",
                 "dialectic", "card_review_user", "card_review_ai", "knowledge_render")


@pytest.mark.parametrize("name", PROMPT_ASSETS)
def test_every_prompt_asset_loads_with_a_version(layers, name):
    """One agent per pass: a versioned core, no unresolved placeholder, real content."""
    core = layers.load_core(name)
    assert core.startswith("PROMPT_VERSION: "), name
    assert "$" not in core, f"{name} still carries an unresolved placeholder"
    assert len(core) > 500, name
    assert "ROLE" in core, f"{name} does not state its role"


def test_the_b3_core_states_the_caps_it_is_given(layers, plugin_package):
    """Every placeholder is filled from the constant that owns it; none reaches the model raw."""
    core = layers.load_core("b3_window_summary")
    summary = plugin_package.window_summary
    assert "$" not in core
    assert f"at most {summary.MAX_OBJECTIVE_CHARS} characters" in core
    assert f"at most {summary.MAX_STATE_ITEMS} items each" in core
    assert f"at most {summary.MAX_TOTAL_CHARS} characters" in core
    priorities = ", ".join(summary.PRIORITIES[:-1]) + " or " + summary.PRIORITIES[-1]
    kinds = ", ".join(summary.REFERENCE_KINDS[:-1]) + " or " + summary.REFERENCE_KINDS[-1]
    assert priorities in core, "the priority enum the prompt states is missing"
    assert kinds in core, "the reference-kind enum the prompt states is missing"


def test_artifact_from_columns_reads_legacy_and_current_marker_keys(layers):
    legacy = {"value_markers": json.dumps({"units": [{"id": "k1"}]})}
    current = {"value_markers": json.dumps({"value_markers": [{"id": "k2"}]})}
    assert layers.artifact_from_columns(legacy)["value_markers"] == [{"id": "k1"}]
    assert layers.artifact_from_columns(current)["value_markers"] == [{"id": "k2"}]
