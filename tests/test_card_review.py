"""The three-day review: card files, validation, operator edits and the wall-clock anchor."""

from __future__ import annotations

import json
import time

import pytest

PLUGIN_YAML = "name: minir\nversion: 0.8.0\nauthor: chaorenex1\n"


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "hermes" / "minir-memory" / "memory.db")
    yield instance
    instance.close()


@pytest.fixture()
def cards(plugin_package):
    module = plugin_package.card_review

    def payload(**over):
        value = {
            "user_identity": {"occupation": "Engineer", "skills": ["Python"],
                              "thinking_models": [], "interests": [], "communication": [],
                              "values_motivation": []},
            "user_preference": {field: [] for field in (
                "communication", "presentation", "method", "decision", "technology",
                "collaboration", "taboos")},
            "ai_identity": {"id": "", "name": "minir", "version": "0.8.0", "type": "",
                            "role_duties": ["maintain memory"], "domains_level": [],
                            "system": "", "creator": "chaorenex1", "maintainer": "",
                            "_sources": ["plugin.yaml"]},
            "ai_preference": {"persona_style": [], "communication_style": [],
                              "permissions": [], "prohibitions": []},
        }
        value.update(over)
        return value

    return module, payload


def test_validation_rejects_session_state(cards):
    module, payload = cards
    bad = payload()
    bad["user_identity"]["current_task"] = "ship it"
    with pytest.raises(module.CardReviewError) as info:
        module.validate_cards(bad, plugin_yaml=PLUGIN_YAML)
    assert "current_task" in str(info.value)


def test_validation_rejects_a_missing_or_extra_field(cards):
    module, payload = cards
    missing = payload()
    missing["user_preference"].pop("taboos")
    with pytest.raises(module.CardReviewError):
        module.validate_cards(missing, plugin_yaml=PLUGIN_YAML)
    extra = payload()
    extra["ai_identity"]["current_goal"] = "x"
    with pytest.raises(module.CardReviewError):
        module.validate_cards(extra, plugin_yaml=PLUGIN_YAML)


def test_a_hard_identity_value_not_in_the_manifest_is_dropped(cards):
    module, payload = cards
    value = payload()
    value["ai_identity"]["version"] = "9.9"
    cleaned = module.validate_cards(value, plugin_yaml=PLUGIN_YAML)
    assert cleaned["ai_identity"]["version"] == ""
    assert cleaned["ai_identity"]["name"] == "minir"


def test_a_file_over_budget_fails_the_whole_review(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    with pytest.raises(module.CardReviewError):
        module.write_cards(store, home, "default", payload(), plugin_yaml=PLUGIN_YAML,
                           budget=5)
    assert not (tmp_path / "hermes" / "minir-memory" / "user" / "identity.md").exists()


def test_write_cards_publishes_four_files_and_revisions(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    revisions = module.write_cards(store, home, "default", payload(), plugin_yaml=PLUGIN_YAML,
                                   evidence=["s1"], reviewer="model")
    assert len(revisions) == 4
    paths = module.card_paths(home, "default")
    for part, path in paths.items():
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "schema: minir-card/v1" in text
    rows = store.card_revisions(limit=10)
    assert {row["part"] for row in rows} == {"identity", "preference"}
    assert {row["scope"] for row in rows} == {"user", "ai"}
    again = module.write_cards(store, home, "default", payload(), plugin_yaml=PLUGIN_YAML,
                               evidence=["s2"], reviewer="model")
    latest = store.latest_card_revision("user", "identity")
    assert json.loads(latest["changed_fields"]) == []
    assert latest["source"] == "review" and again


def test_a_hand_edit_is_recorded_before_the_review_overwrites_it(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    module.write_cards(store, home, "default", payload(), plugin_yaml=PLUGIN_YAML)
    path = module.card_paths(home, "default")[module.USER_SELF]
    text = path.read_text(encoding="utf-8").replace("Engineer", "Hand Edited")
    path.write_text(text, encoding="utf-8")
    module.write_cards(store, home, "default", payload(), plugin_yaml=PLUGIN_YAML)
    rows = store.card_revisions("user", "identity", limit=10)
    assert any(row["source"] == "operator_edit" for row in rows)
    assert path.read_text(encoding="utf-8").find("Engineer") >= 0


def test_a_legacy_file_without_front_matter_is_kept_as_declared(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    path = module.card_paths(home, "default")[module.USER_SELF]
    path.parent.mkdir(parents=True)
    path.write_text("# hand written identity\n", encoding="utf-8")
    module.write_cards(store, home, "default", payload(), plugin_yaml=PLUGIN_YAML)
    declared = path.with_name("identity.declared.md")
    assert declared.read_text(encoding="utf-8") == "# hand written identity\n"
    assert "schema: minir-card/v1" in path.read_text(encoding="utf-8")


def test_load_global_cards_merges_the_ai_files(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    module.write_cards(store, home, "default", payload(), plugin_yaml=PLUGIN_YAML)
    loaded = module.load_global_cards(home, "default")
    assert loaded["user_self"]["occupation"] == "Engineer"
    assert loaded["ai_identity"]["name"] == "minir"
    assert loaded["ai_identity"]["role_duties"] == ["maintain memory"]


def test_the_review_prompt_assembles_the_four_card_rules(cards, plugin_package, store,
                                                        tmp_path):
    loader = plugin_package.rules.RulesLoader(hermes_home=str(tmp_path / "hermes"))
    system, prompt = plugin_package.card_review.build_prompt(
        store, str(tmp_path / "hermes"), "default", since=0.0,
        plugin_yaml=PLUGIN_YAML, loader=loader)
    assert "four durable files" in system
    assert "Merge, do not rebuild" in system, "a card rule's CONTRACT was not applied"
    assert "Return exactly one JSON object" in system
    assert "Current cards" in prompt and "Manifest" in prompt


def test_the_scheduler_waits_three_days_and_advances_only_on_success(cards, plugin_package,
                                                                     store):
    module = plugin_package.card_review
    calls: list[float] = []

    def run(since):
        calls.append(since)
        return []

    scheduler = module.CardReviewScheduler(store, run, subject="user:default", period=3 * 86400)
    now = time.time()
    assert scheduler.ensure_anchor(now) == now
    assert scheduler.due(now + 86400) is False
    assert scheduler.tick(now + 86400) is False
    assert scheduler.due(now + 3 * 86400 + 1) is True

    def failing(since):
        calls.append(since)
        raise module.CardReviewError("model down")

    broken = module.CardReviewScheduler(store, failing, subject="user:default",
                                        period=3 * 86400)
    assert broken.tick(now + 3 * 86400 + 1) is False
    state = broken._state()
    assert state["attempts"] == 1
    assert state["next_attempt_at"] == pytest.approx(now + 3 * 86400 + 1 + 1800, abs=5)
    assert broken.due(now + 3 * 86400 + 1000) is False

    healthy = module.CardReviewScheduler(store, run, subject="user:default", period=3 * 86400)
    retry_at = now + 3 * 86400 + 1 + 1800
    assert healthy.tick(retry_at - 1) is False, "the retry backoff gates the next attempt"
    assert healthy.due(retry_at + 1) is True, healthy._state()
    assert healthy.tick(retry_at + 1) is True
    assert calls and calls[-1] == pytest.approx(now, abs=5)
    assert healthy._state()["attempts"] == 0
    assert healthy._state()["last_success_at"] == pytest.approx(retry_at + 1, abs=5)
