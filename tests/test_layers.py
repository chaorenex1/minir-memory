"""Layer contracts: the field validation that must fail before an artifact is published."""

from __future__ import annotations

import importlib

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
        "user_card": {"user_self": layers.empty_user_self(),
                      "user_preferences": layers.empty_user_preferences()},
        "ai_card": {"ai_self": layers.empty_ai_self(),
                    "ai_identity": layers.empty_ai_identity()},
        "window_summary": {
            "objective": "", "current_state": {"done": [], "doing": [], "todo": []},
            "decisions": [], "constraints": [], "verified_facts": [], "open_issues": [],
            "next_actions": [], "references": []},
        "base_changed": {"changed": False, "fields": []},
        "changes": [], "prunings": [],
    }
    value.update(over)
    return value


def test_identity_hard_fields_must_be_declared_in_the_manifest(layers):
    manifest = "name: minir\nversion: 0.8.0\n"
    card = layers.empty_ai_identity()
    card.update({"name": "minir", "version": "0.8.0", "creator": "someone", "type": "plugin"})
    cleaned = layers.validate_ai_identity(card, manifest)
    assert cleaned["name"] == "minir" and cleaned["version"] == "0.8.0"
    assert cleaned["creator"] == "" and cleaned["type"] == "", \
        "a value the manifest does not declare is dropped, not trusted"


def test_a_knowledge_unit_needs_an_epistemic_value(layers):
    unit = _unit()
    unit.pop("epistemic")
    with pytest.raises(layers.LayerContractError, match="KNOWLEDGE requires"):
        layers.validate_units({"units": [unit]})


def test_a_non_knowledge_unit_must_not_carry_an_epistemic_value(layers):
    with pytest.raises(layers.LayerContractError, match="only KNOWLEDGE"):
        layers.validate_units({"units": [_unit(category="METHOD", epistemic="fact")]})


def test_a_unit_needs_one_known_category(layers):
    with pytest.raises(layers.LayerContractError, match="must be one of KNOWLEDGE"):
        layers.validate_units({"units": [_unit(category="OTHER")]})


def test_a_relation_must_target_a_unit_in_the_same_window(layers):
    unit = _unit(relations=[{"type": "caused_by", "ref": "k9"}])
    with pytest.raises(layers.LayerContractError, match="not in this window"):
        layers.validate_units({"units": [unit]})


def test_objects_must_be_typed(layers):
    with pytest.raises(layers.LayerContractError, match='"type:name"'):
        layers.validate_units({"units": [_unit(objects=["Docker"])]})


def test_units_merge_on_category_org_and_topic(layers):
    previous = [_unit(content="old evidence")]
    merged = layers.validate_units({"units": [_unit(content="new evidence")]}, previous=previous)
    assert len(merged) == 1 and merged[0]["content"] == "new evidence"


def test_a_review_cannot_prune(layers):
    with pytest.raises(layers.LayerContractError, match="may not prune"):
        layers.validate_review(_review(layers, prunings=[{"part": "user_self"}]))


def test_a_review_that_claims_a_change_must_list_the_part(layers):
    with pytest.raises(layers.LayerContractError, match="fields is empty"):
        layers.validate_review(_review(layers, base_changed={"changed": True, "fields": []}))


def test_a_change_scope_must_match_its_part(layers):
    change = {"scope": "ai_card", "part": "user_self", "op": "update",
              "reason": "moved", "sessions": []}
    with pytest.raises(layers.LayerContractError, match="does not match part"):
        layers.validate_review(_review(layers, changes=[change]))


def test_a_preset_is_valid_but_an_unknown_one_is_not(layers):
    card = layers.preset_user_self("en")
    assert layers.validate_user_self(card)["_preset"] == layers.PRESET_ID
    card["_preset"] = "astronaut"
    with pytest.raises(layers.LayerContractError, match="unknown preset"):
        layers.validate_user_self(card)
