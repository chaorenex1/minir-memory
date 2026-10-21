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
            "user_identity": "# Who this person is\n\nAn Engineer on backend services. Python is "
                             "the working language; they read a diff before they run it.\n",
            "user_preference": "# How they want answers\n\n- Terse first, detail on request.\n",
            "ai_identity": "# Who the assistant is\n\nminir 0.8.0, built by chaorenex1. Its duty "
                           "is to maintain memory.\n",
            "ai_preference": "# How the assistant answers\n\n- Short paragraphs, no preamble.\n",
        }
        value.update(over)
        return value

    return module, payload


def test_validation_rejects_a_non_string_body(cards):
    module, payload = cards
    bad = payload()
    bad["user_identity"] = {"current_task": "ship it"}
    with pytest.raises(module.CardReviewError) as info:
        module.validate_cards(bad)
    assert "user_identity" in str(info.value)
    assert "markdown" in str(info.value), "the body is text, not a field object"
    field_value = payload()
    field_value["user_preference"] = ["terse"]
    with pytest.raises(module.CardReviewError):
        module.validate_cards(field_value)


def test_validation_rejects_a_missing_or_extra_card(cards):
    module, payload = cards
    missing = payload()
    missing.pop("user_preference")
    with pytest.raises(module.CardReviewError):
        module.validate_cards(missing)
    extra = payload()
    extra["session_state"] = "# this window\n\nship it\n"
    with pytest.raises(module.CardReviewError):
        module.validate_cards(extra)


def test_an_identity_value_outside_the_manifest_is_kept(cards):
    module, payload = cards
    value = payload()
    value["ai_identity"] = ("# Who the assistant is\n\nminir 9.9, built by chaorenex1. "
                            "No manifest declares that version.\n")
    cleaned = module.validate_cards(value)
    assert "9.9" in cleaned["ai_identity"], "there is no manifest field contract left"
    assert "minir" in cleaned["ai_identity"]


def test_a_file_over_budget_fails_the_whole_review(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    with pytest.raises(module.CardReviewError):
        module.write_cards(store, home, "default", payload(),
                           budget=5)
    assert not (tmp_path / "hermes" / "minir-memory" / "user" / "identity.md").exists()


def test_write_cards_publishes_four_files_and_revisions(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    revisions = module.write_cards(store, home, "default", payload(), reviewer="model")
    assert len(revisions) == 4
    paths = module.card_paths(home, "default")
    for part, path in paths.items():
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "schema: minir-card/v1" in text
    rows = store.card_revisions(limit=10)
    assert {row["part"] for row in rows} == {"identity", "preference"}
    assert {row["scope"] for row in rows} == {"user", "ai"}
    first = store.latest_card_revision("user", "identity")
    assert json.loads(first["changed_fields"]) == ["body"], "the first body is a change"
    assert json.loads(first["evidence"]) == [], "history is the hash pair, not an audit log"
    again = module.write_cards(store, home, "default", payload(), reviewer="model")
    latest = store.latest_card_revision("user", "identity")
    assert json.loads(latest["changed_fields"]) == []
    assert latest["source"] == "review" and again
    module.write_cards(store, home, "default",
                       payload(user_identity="# Who this person is\n\nAn Engineer who now "
                                             "writes Go as well.\n"))
    moved = store.latest_card_revision("user", "identity")
    assert json.loads(moved["changed_fields"]) == ["body"], "a new body text is a body change"


def test_a_hand_edit_is_recorded_before_the_review_overwrites_it(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    module.write_cards(store, home, "default", payload())
    path = module.card_paths(home, "default")[module.USER_SELF]
    text = path.read_text(encoding="utf-8").replace("Engineer", "Hand Edited")
    path.write_text(text, encoding="utf-8")
    module.write_cards(store, home, "default", payload())
    rows = store.card_revisions("user", "identity", limit=10)
    assert any(row["source"] == "operator_edit" for row in rows)
    assert path.read_text(encoding="utf-8").find("Engineer") >= 0


def test_a_legacy_file_without_front_matter_is_kept_as_declared(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    path = module.card_paths(home, "default")[module.USER_SELF]
    path.parent.mkdir(parents=True)
    path.write_text("# hand written identity\n", encoding="utf-8")
    module.write_cards(store, home, "default", payload())
    declared = path.with_name("identity.declared.md")
    assert declared.read_text(encoding="utf-8") == "# hand written identity\n"
    assert "schema: minir-card/v1" in path.read_text(encoding="utf-8")


def test_load_global_cards_merges_the_ai_files(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    module.write_cards(store, home, "default", payload())
    loaded = module.load_global_cards(home, "default")
    assert "Engineer" in loaded["user_self_representation"]
    assert "Terse first" in loaded["user_preferences"]
    assert "minir 0.8.0" in loaded["ai_identity"]
    assert "maintain memory" in loaded["ai_identity"]
    assert loaded["ai_identity"].endswith(payload()["ai_preference"].strip()), \
        "the AI part is the identity body plus the preference body"


def test_each_review_file_has_its_own_agent_rule_and_output(cards, plugin_package, store,
                                                            tmp_path):
    module = plugin_package.card_review
    home = str(tmp_path / "hermes")
    cases = (
        ("user_identity", "You maintain the user side", "Who this person is", "user_identity", False),
        ("user_preference", "You maintain the user side", "How this person wants the assistant",
         "user_preference", False),
        ("ai_identity", "You maintain the AI side", "Who this assistant is in this profile",
         "ai_identity", True),
        ("ai_preference", "You maintain the AI side", "Who this assistant is in this profile",
         "ai_preference", True),
    )
    for scope, core_text, rule_text, key, has_manifest in cases:
        system, prompt = module.build_prompt(
            store, home, "default", since=0.0, scope=scope, plugin_yaml=PLUGIN_YAML)
        assert core_text in system
        assert rule_text in system, f"{scope} did not get its rule"
        assert f'"{key}"' in system, f"{scope} did not get its output contract"
        assert "Current user card" in prompt or "Current AI card" in prompt
        assert ("Manifest" in prompt) is has_manifest


def _scope_key(scope):
    return {
        "user_identity": "user_identity",
        "user_preference": "user_preference",
        "ai_identity": "ai_identity",
        "ai_preference": "ai_preference",
    }[scope]


def _reply(payload, scope):
    key = _scope_key(scope)
    return json.dumps({key: payload[key]})


def _scope_from_system(system):
    for scope, key in (
        ("user_identity", "user_identity"),
        ("user_preference", "user_preference"),
        ("ai_identity", "ai_identity"),
        ("ai_preference", "ai_preference"),
    ):
        if f'"{key}"' in system:
            return scope
    raise AssertionError("the system prompt names no single card key")


def test_run_review_calls_four_files_and_lets_the_database_record_history(cards, store, tmp_path):
    module, payload = cards
    scopes: list[str] = []

    def complete(system, prompt):
        scope = _scope_from_system(system)
        scopes.append(scope)
        return _reply(payload(), scope)

    revisions = module.run_review(store, str(tmp_path / "hermes"), "default",
                                  complete=complete, since=0.0, plugin_yaml=PLUGIN_YAML)
    assert scopes == list(module.CARD_SCOPE_ORDER), "the review runs one agent per file"
    assert len(revisions) == 4
    identity = store.latest_card_revision("user", "identity")
    assert json.loads(identity["changed_fields"]) == ["body"]
    assert json.loads(identity["evidence"]) == [], "the review carries no audit array"
    assert identity["after_hash"] and identity["before_hash"] in ("", identity["after_hash"])


def test_an_audit_array_is_a_format_failure(cards, store, tmp_path):
    module, payload = cards

    def complete(system, prompt):
        scope = _scope_from_system(system)
        reply = json.loads(_reply(payload(), scope))
        reply["changes"] = [{"scope": scope, "part": "identity", "op": "add"}]
        return json.dumps(reply)

    with pytest.raises(module.CardReviewError, match="unexpected key"):
        module.run_review(store, str(tmp_path / "hermes"), "default",
                          complete=complete, since=0.0, plugin_yaml=PLUGIN_YAML)


def test_build_prompt_flags_a_hand_edited_card(cards, store, tmp_path):
    module, payload = cards
    home = str(tmp_path / "hermes")
    module.write_cards(store, home, "default", payload())
    path = module.card_paths(home, "default")[module.USER_SELF]
    path.write_text(path.read_text(encoding="utf-8").replace("Engineer", "Operator"),
                    encoding="utf-8")
    _system, user_prompt = module.build_prompt(store, home, "default", since=0.0,
                                               scope="user_identity", plugin_yaml=PLUGIN_YAML)
    assert "Hand-edited since the last review" in user_prompt
    assert "user/identity" in user_prompt
    _ai_system, ai_prompt = module.build_prompt(store, home, "default", since=0.0,
                                                scope="ai_identity", plugin_yaml=PLUGIN_YAML)
    assert "Hand-edited since the last review" not in ai_prompt, \
        "each file is told about its own body only"


def test_a_missing_body_fails_the_review(cards, store, tmp_path):
    module, payload = cards

    def complete(system, prompt):
        scope = _scope_from_system(system)
        reply = json.loads(_reply(payload(), scope))
        reply.pop(_scope_key(scope))
        return json.dumps(reply)

    with pytest.raises(module.CardReviewError, match="missing"):
        module.run_review(store, str(tmp_path / "hermes"), "default",
                          complete=complete, since=0.0, plugin_yaml=PLUGIN_YAML)


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
