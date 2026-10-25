"""Claim selection, authority ceiling and the reviewer's claim field."""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def claims(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.claims")


def _card(tmp_path, name, body, source="review"):
    path = tmp_path / "minir-memory" / "user" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nsource: {source}\n---\n\n{body}", encoding="utf-8")
    return tmp_path


def test_card_and_soul_authority_come_from_the_source(claims, tmp_path):
    home = _card(tmp_path, "identity.md", "## Work\n- maintains minir\n", source="review")
    path = tmp_path / "minir-memory" / "user" / "preference.md"
    path.write_text("---\nsource: operator_edit\n---\n\n## Style\n- terse\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Boundaries\n- never print secrets\n",
                                      encoding="utf-8")
    found = claims.card_claims(str(home), "default")
    assert {claim.authority for claim in found} == {"card", "operator"}
    soul = claims.soul_claims(str(home))
    assert soul and soul[0].authority == "operator"
    assert soul[0].test == "secret_read"
    assert soul[0].id == "soul:SOUL.md#2"


def test_two_files_named_preference_get_distinct_path_ids(claims, tmp_path):
    home = _card(tmp_path, "preference.md", "## Style\n- terse\n")
    agent = tmp_path / "minir-memory" / "agents" / "default" / "preference.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text("---\nsource: review\n---\n\n## Voice\n- measured\n", encoding="utf-8")

    ids = sorted(claim.id for claim in claims.card_claims(str(home), "default"))

    assert ids == ["card:agents/default/preference.md#2", "card:user/preference.md#2"]


def test_a_third_level_heading_narrows_the_scope(claims, tmp_path):
    home = _card(tmp_path, "preference.md",
                 "## Style\n### Delivery\n- never commit without asking\n")

    found = [claim for claim in claims.card_claims(str(home), "default")
             if "never commit" in claim.text]

    assert found and found[0].scope == "delivery"


def test_a_missing_card_falls_back_to_its_base_row(claims, tmp_path):
    home = _card(tmp_path, "preference.md", "## Style\n- terse\n")
    base = {"user_representation": "I am the operator.\nPrefers evidence.",
            "user_peer_card": "", "ai_identity_card": "", "ai_self_representation": ""}

    found = claims.card_claims(str(home), "default", base)

    assert [claim.text for claim in found if "#base:" in claim.id] == [
        "I am the operator.", "Prefers evidence."]
    assert all(claim.id.startswith("card:user/identity.md#base:") for claim in found
               if "#base:" in claim.id)


def test_claims_and_the_gate_are_on_by_default(plugin_package):
    defaults = plugin_package.adviser_mode._DEFAULTS
    assert (defaults["claims"], defaults["claim_gate"]) == ("on", True)


def test_an_empty_pool_cannot_produce_a_blocker(plugin_package):
    mode = plugin_package.adviser_mode
    answer = json.dumps({"action": "advise", "advices": [{
        "severity": "blocker", "target": "tool_choice", "text": "stop now",
        "claim": "knowledge:tools/KNOWLEDGE-docker.md#3"}]})
    dropped: dict[str, int] = {}

    notes = mode.parse_review(answer, severity_floor="nit", max_advices=3, dropped=dropped,
                              active_claims=[])

    assert notes == []
    assert dropped == {"unselected_claim": 1}


def test_a_card_claim_and_an_unclaimed_preference_stop_at_concern(claims, plugin_package):
    mode = plugin_package.adviser_mode
    card = claims.Claim(id="card:user/preference.md#2", text="be terse", scope="domain",
                        authority="card").as_dict()
    answer = json.dumps({"action": "advise", "advices": [
        {"severity": "blocker", "target": "tool_choice", "text": "one", "claim": card["id"]},
        {"severity": "blocker", "target": "user_preferences_check", "text": "two"}]})

    notes = mode.parse_review(answer, severity_floor="nit", max_advices=3, active_claims=[card])
    capped = claims.apply_ceiling(notes, [card])

    assert [note["severity"] for note in capped] == ["concern", "concern"]
    assert capped[0]["claim_text"] == "be terse"


def test_a_duplicate_claim_id_is_an_error_not_an_overwrite(claims):
    claim = claims.Claim(id="card:user/preference.md#3", text="x", scope="domain",
                         authority="card")
    with pytest.raises(ValueError):
        claims.unique([claim, claim])
    with pytest.raises(ValueError):
        claims.apply_ceiling([], [claim.as_dict(), claim.as_dict()])


def test_the_selector_matches_a_draft_write_and_stays_silent_otherwise(claims, tmp_path):
    _card(tmp_path, "preference.md",
          "## Method\n- 草稿轮不落地代码\n", source="operator_edit")
    pool = claims.card_claims(str(tmp_path), "default")
    hit = claims.select({"tool": "write_file", "args": "draft", "text": "写草稿"},
                        pool)
    assert hit and hit[0]["test"] == "draft_write"
    assert claims.select({"tool": "read_file", "args": "README"}, pool) == []


def test_the_ceiling_caps_by_authority(claims):
    notes = [{"severity": "blocker", "target": "user_preferences_check", "claim": "c"},
             {"severity": "blocker", "target": "general", "claim": "k"},
             {"severity": "concern", "target": "general", "claim": "o"}]
    capped = claims.apply_ceiling(notes, [
        {"id": "c", "authority": "card", "text": "card line"},
        {"id": "k", "authority": "knowledge", "text": "page line"},
        {"id": "o", "authority": "operator", "text": "soul line"},
    ])
    assert [note["severity"] for note in capped] == ["concern", "concern", "concern"]
    assert capped[2].get("claim_text") == "soul line"


def test_parse_review_drops_an_unselected_claim(plugin_package):
    mode = plugin_package.adviser_mode
    answer = json.dumps({"action": "advise", "advices": [
        {"severity": "concern", "target": "general", "text": "keep", "claim": "c1"},
        {"severity": "concern", "target": "general", "text": "drop", "claim": "c2"},
    ]})
    dropped: dict[str, int] = {}
    parsed = mode.parse_review(answer, active_claims=[{"id": "c1"}], dropped=dropped)
    assert [note["content"] for note in parsed] == ["keep"]
    assert dropped["unselected_claim"] == 1
