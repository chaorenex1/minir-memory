"""The reviewer's half of JEV: fork candidates, verdict parsing, the learned rule file, and the
pull consult. A review is a model call, so ``agent_loop.run`` is faked here; every parser and
every write is the real one.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from adviser_fakes import drive_advise


def _mode_provider(make_provider, **fork_overrides):
    block = {"enabled": True, "mode": "watch", "cadence": 1, "severity_floor": "nit",
             "max_reviews_per_turn": 4, "max_advices": 3, "max_chars": 900,
             "claims": "off", "review_interval": 1, "forks": "on"}
    block.update(fork_overrides)
    return make_provider({"adviser_mode": block})


# -- parse_forks ---------------------------------------------------------------

def _active(plugin_package, args=None):
    args = args or {"path": "C:/repo/doc/a.md"}
    return [{"id": "f1", "kind": "arg", "tool": "read_file",
             "key": plugin_package.fork_rules.fork_key("arg", "read_file", args=args),
             "args": json.dumps(args), "arg_keys": sorted(args), "recent": "", "why": "failed"}]


def test_adviser_fork_verdict_rejects_an_unknown_id(plugin_package):
    dropped: dict[str, int] = {}
    rows = plugin_package.adviser_mode.parse_forks(
        json.dumps({"action": "silent", "forks": [
            {"id": "f9", "decision": "rewrite", "action": {"args": {"path": "C:/x"}},
             "reason": "not a candidate"}]}),
        _active(plugin_package), dropped)

    assert rows == []
    assert dropped["unknown_fork"] == 1


def test_adviser_fork_verdict_never_blocks(plugin_package):
    dropped: dict[str, int] = {}
    rows = plugin_package.adviser_mode.parse_forks(
        json.dumps({"action": "silent", "forks": [
            {"id": "f1", "decision": "block", "reason": "stop"}]}),
        _active(plugin_package), dropped)
    assert rows == []
    assert dropped["bad_fork_decision"] == 1

    # The accepted set is exactly the rule decisions — block is not one of them.
    assert "block" not in plugin_package.fork_rules.DECISIONS


def test_a_rewrite_only_touches_existing_argument_keys(plugin_package):
    dropped: dict[str, int] = {}
    rows = plugin_package.adviser_mode.parse_forks(
        json.dumps({"action": "silent", "forks": [{
            "id": "f1", "decision": "rewrite",
            "action": {"args": {"path": "C:/repo/doc/b.md", "encoding": "utf-16"}},
            "reason": "the live draft is b.md"}]}),
        _active(plugin_package), dropped)

    assert len(rows) == 1
    assert rows[0]["action"] == {"args": {"path": "C:/repo/doc/b.md"}}
    assert rows[0]["learn"] is True
    assert dropped["new_fork_arg"] == 1


def test_a_rewrite_may_not_climb_out_of_the_project(plugin_package):
    dropped: dict[str, int] = {}
    rows = plugin_package.adviser_mode.parse_forks(
        json.dumps({"action": "silent", "forks": [{
            "id": "f1", "decision": "rewrite",
            "action": {"args": {"path": "../../secrets/.env"}},
            "reason": "read the config"}]}),
        _active(plugin_package), dropped)

    assert rows == []
    assert dropped["fork_path_escapes"] == 1


def test_a_reasonless_verdict_writes_no_rule(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    rows = plugin_package.adviser_mode.parse_forks(
        json.dumps({"action": "silent", "forks": [
            {"id": "f1", "decision": "dispatch", "reason": ""}]}),
        _active(plugin_package))

    assert rows and rows[0]["learn"] is False
    assert provider._fork.apply_review_forks("s", rows) == 0
    assert not provider._fork._learned_path().exists()  # no rule file was written
    assert provider._store.fork_ledger()  # the decision itself is still recorded


# -- grades --------------------------------------------------------------------

def test_an_unexplained_overturn_is_refused(plugin_package):
    dropped: dict[str, int] = {}
    rows = plugin_package.adviser_mode.parse_fork_grades(
        json.dumps({"action": "silent", "fork_grades": [
            {"id": 7, "verdict": "overturned"}]}),
        [{"id": 7, "key": "arg:read_file:{}"}], dropped)

    assert rows == []
    assert dropped["unexplained_overturn"] == 1


def test_a_grade_must_name_an_applied_row(plugin_package):
    dropped: dict[str, int] = {}
    rows = plugin_package.adviser_mode.parse_fork_grades(
        json.dumps({"action": "silent", "fork_grades": [
            {"id": 99, "verdict": "accepted"}]}),
        [{"id": 7, "key": "arg:read_file:{}"}], dropped)

    assert rows == []
    assert dropped["unknown_fork_grade"] == 1


def test_an_overturn_demotes_the_rule_it_graded(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    fork = provider._fork
    key = plugin_package.fork_rules.fork_key("arg", "read_file", args={"path": "C:/repo/d/a.md"})
    fork.apply_review_forks("s", [{"kind": "arg", "key": key, "decision": "rewrite",
                                   "action": {"args": {"path": "C:/repo/d/b.md"}},
                                   "reason": "b.md is the live one", "learn": True}])
    rule = fork.rules("arg")[0]
    provider._store.record_fork_hit(rule["id"])
    provider._store.record_fork_hit(rule["id"])
    fork.refresh_learned()
    assert fork.rules("arg")[0]["strength"] == "medium"

    ledger_id = fork.record(
        plugin_package.fork_dispatch.Verdict(key=key, decision="rewrite", source="rule",
                                             strength="medium", rule_id=rule["id"]),
        applied=True, session="s")
    assert ledger_id

    graded = fork.apply_grades("s", [{"id": ledger_id, "verdict": "overturned",
                                      "reason": "the user opened a.md again"}])

    assert graded == 1
    assert fork.rules("arg")[0]["strength"] == "low"


# -- candidates ----------------------------------------------------------------

def test_candidates_score_a_failed_call_and_skip_a_covered_one(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    fork = provider._fork
    arguments = {"command": "pytest -q"}
    events = [{"seq": 1, "kind": "user", "text": "make the pytest failures pass"},
              {"seq": 2, "kind": "tool", "tool": "terminal", "args": json.dumps(arguments),
               "result": "FAILED", "error": "exit 1", "status": "error"},
              {"seq": 3, "kind": "tool", "tool": "terminal", "args": json.dumps(arguments),
               "result": "FAILED", "error": "exit 1", "status": "error"}]

    rows = fork.candidates(events, session="s")

    assert len(rows) == 1
    assert rows[0]["id"] == "f1"
    assert "failed" in rows[0]["why"] and "2x" in rows[0]["why"]
    assert rows[0]["arg_keys"] == ["command"]

    # Once an active rule answers that key, the reviewer is not asked again.
    key = rows[0]["key"]
    with fork._lock:
        fork._rules.setdefault("arg", []).append(
            {"id": "r-x", "kind": "arg", "key": key, "decision": "rewrite", "strength": "high",
             "action": {"args": {"command": "pytest -q tests/test_fork.py"}}, "scope": "",
             "authority": "operator", "ttl_days": 0, "created_at": 0.0})
    assert fork.candidates(events, session="s") == []


# -- the review round trip -----------------------------------------------------

def test_a_review_writes_a_low_rule_and_the_ledger(plugin_package, make_provider, monkeypatch):
    provider = _mode_provider(make_provider)
    answer = json.dumps({
        "action": "silent",
        "forks": [{"id": "f1", "decision": "rewrite",
                   "action": {"args": {"command": "pytest -q tests/test_fork.py"}},
                   "reason": "the failing run named that file"},
                  ],
    })
    calls: list = []

    def _fake_run(message, **kwargs):
        calls.append({"message": message, **kwargs})
        drive_advise(kwargs, answer)
        return SimpleNamespace(answer=answer, api_calls=1, model="reviewer", provider="test",
                               toolsets=list(kwargs.get("toolsets") or []), messages=[])

    monkeypatch.setattr(plugin_package.agent_loop, "run", _fake_run)

    provider._adviser_mode.on_post_tool_call(
        session_id="session-test", turn_id="t1", tool_name="terminal", status="error",
        args={"command": "pytest -q"}, result="FAILED tests/test_fork.py", error_message="exit 1")
    assert provider._adviser_mode.wait_idle(5.0)

    assert calls, "the review never ran"
    prompt = calls[0]["message"]
    assert "<active_forks>" in prompt and '"id": "f1"' in prompt
    assert "<applied_forks>" in prompt

    learned = json.loads(provider._fork._learned_path().read_text(encoding="utf-8"))
    assert [rule["strength"] for rule in learned["rules"]] == ["low"]
    assert learned["rules"][0]["decision"] == "rewrite"
    assert learned["rules"][0]["key"].startswith("arg:terminal:")
    # A low rule is readable, listable, and inert.
    assert provider._fork.resolve_now("arg", "terminal", args={"command": "pytest -q"}) is None
    rows = provider._store.fork_ledger("session-test")
    assert rows and rows[0]["source"] == "adviser" and rows[0]["applied"] == 0


# -- delegation target ---------------------------------------------------------

def test_delegation_target_is_delivered(plugin_package):
    prompting = plugin_package.prompting
    assert "delegation" in prompting.ACTION_TARGETS
    assert "delegation" in prompting.DELIVERED_TARGETS
    assert set(prompting.DELIVERED_TARGETS) == {name for name, *_ in prompting.ADVICE_TARGETS}

    rule = plugin_package.rules.rule_text("advice_target")
    assert "`delegation`" in rule


# -- minir_advise --------------------------------------------------------------

class _Ctx:
    """The registration surface the plugin toolset needs, and nothing else."""

    def __init__(self) -> None:
        self.tools: list[str] = []

    def register_tool(self, **kwargs):
        self.tools.append(str(kwargs.get("name") or ""))


def test_minir_advise_is_not_registered_in_the_minir_toolset(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    ctx = _Ctx()

    plugin_package.plugin_tools.register(ctx, provider)

    assert plugin_package.plugin_tools.ADVISE_TOOL not in ctx.tools
    assert "minir_search" in ctx.tools
    # It still exists on the provider surface: that is the only path to the main agent.
    assert plugin_package.plugin_tools.ADVISE_TOOL in [
        schema["name"] for schema in provider.get_tool_schemas()]
    # And every adviser run is granted the toolset that does not contain it.
    assert plugin_package.plugin_tools.TOOLSET in plugin_package.plugin_tools.agent_toolsets([])


def test_minir_advise_refuses_a_dispatched_child(plugin_package, make_provider):
    child = plugin_package.agent_watch.child_session_id("parent", "worker", "job-1")
    plugin_package.agent_watch.register(parent="parent", agent="worker", job_id="job-1")
    try:
        provider = _mode_provider(make_provider)
        provider._session_id = child
        payload = json.loads(provider.handle_tool_call("minir_advise", {"phase": "plan"}))
    finally:
        plugin_package.agent_watch.unregister(child)

    assert "child" in payload["error"]


def test_minir_advise_returns_the_verdict_it_was_given(plugin_package, make_provider,
                                                     monkeypatch):
    provider = _mode_provider(make_provider)

    def _consult(self, question, *, session, timeout):
        return {"answer": "CONCERN - the plan names no test.\nTARGET: current_step"}

    monkeypatch.setattr(plugin_package.fork_dispatch.ForkDispatcher, "_consult", _consult)

    payload = json.loads(provider.handle_tool_call(
        "minir_advise", {"phase": "plan", "question": "is this plan sound?"}))

    assert payload["verdict"] == "concern"
    assert payload["target"] == "current_step"
    assert payload["advice"].startswith("CONCERN")
    # Read-only: no ledger row, no rule file.
    assert provider._store.fork_ledger() == []
    assert not provider._fork._learned_path().exists()


def test_minir_advise_rejects_an_unknown_phase(plugin_package, make_provider):
    provider = _mode_provider(make_provider)
    payload = json.loads(provider.handle_tool_call("minir_advise", {"phase": "shipit"}))
    assert "phase must be one of" in payload["error"]


def test_the_review_contract_declares_the_fork_fields(plugin_package):
    schema = plugin_package.adviser_mode.advise_tool_schema()["parameters"]
    assert "forks" in schema["properties"]
    assert "fork_grades" in schema["properties"]
    assert schema["properties"]["forks"]["items"]["properties"]["decision"]["enum"] == list(
        plugin_package.fork_rules.DECISIONS)
