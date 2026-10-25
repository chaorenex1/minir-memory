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
