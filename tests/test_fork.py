"""JEV fork dispatch: keys, the strength ladder, the hot path, the gate and the learned rules.

Everything here runs against the real ``MinirStore`` on a temporary file. The only stubbed seam
is ``ForkDispatcher._consult``: a gate consult is a model call, and these tests are about what
the dispatcher does with an answer (or with no answer), not about the model.
"""

from __future__ import annotations

import json


def _write_rules(home, rules, name="static.json"):
    target = home / "minir-memory" / "forks"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps({"version": 1, "rules": rules}), encoding="utf-8")


def _arg_key(plugin_package, tool, args):
    return plugin_package.fork_rules.fork_key("arg", tool, args=args)


def _fork_config(**overrides):
    block = {"enabled": True, "forks": "on", "claims": "off"}
    block.update(overrides)
    return {"adviser_mode": block}


# -- keys ----------------------------------------------------------------------

def test_signature_collapses_sibling_paths(plugin_package):
    left = plugin_package.fork_rules.signature(
        {"path": "C:/Users/zarag/projects/minir-memory/doc/a.md"})
    right = plugin_package.fork_rules.signature(
        {"path": "C:/Users/zarag/projects/minir-memory/doc/b.md"})
    assert left == right
    assert "doc/*" in left
    # A different directory is a different family: the collapse stops at the leaf.
    other = plugin_package.fork_rules.signature(
        {"path": "C:/Users/zarag/projects/minir-memory/tests/a.md"})
    assert other != left


def test_signature_keeps_different_tools_apart(plugin_package):
    rules = plugin_package.fork_rules
    args = {"path": "C:/repo/doc/a.md"}
    assert rules.fork_key("arg", "read_file", args=args) != rules.fork_key("arg", "patch", args=args)
    # The retry key is its own namespace: a failure counter cannot collide with a rewrite rule.
    assert rules.fork_key("retry", "read_file", args=args) != rules.fork_key("arg", "read_file",
                                                                            args=args)
    # Long numbers are placeholders, so a turn counter cannot fragment the key space.
    assert rules.signature({"line": 1201}) == rules.signature({"line": 42})


def test_a_low_rule_is_shadow_only(plugin_package):
    rules = plugin_package.fork_rules
    low = {"decision": "rewrite", "strength": "low"}
    assert not rules.may_apply(low, hook="pre_tool")
    assert rules.may_apply({**low, "strength": "medium"}, hook="pre_tool")
    # Each hook accepts exactly one decision shape.
    assert not rules.may_apply({"decision": "compress", "strength": "high"}, hook="pre_tool")
    assert rules.may_apply({"decision": "compress", "strength": "high"}, hook="transform")


def test_a_learned_rule_never_blocks(plugin_package, make_provider, tmp_path):
    """``block`` is not a rule decision: it is dropped at load, so no file can stop a call."""
    key = _arg_key(plugin_package, "read_file", {"path": "C:/repo/doc/a.md"})
    _write_rules(tmp_path, [{"id": "r-block", "kind": "arg", "key": key, "decision": "block",
                             "strength": "exact", "action": {"args": {"path": "C:/x"}}}])
    provider = make_provider(_fork_config())
    fork = provider._fork

    assert fork.rules() == []
    assert fork.resolve_now("arg", "read_file", args={"path": "C:/repo/doc/b.md"}) is None
    assert provider._store.fork_ledger() == []


def test_a_malformed_rule_is_skipped_not_fatal(plugin_package, make_provider, tmp_path):
    good_key = _arg_key(plugin_package, "read_file", {"path": "C:/repo/doc/a.md"})
    _write_rules(tmp_path, [
        {"id": "broken", "kind": "arg", "key": "arg:read_file:{}", "decision": "rewrite"},
        {"id": "r-ok", "kind": "arg", "key": good_key, "decision": "rewrite", "strength": "high",
         "action": {"args": {"path": "C:/repo/doc/b.md"}}},
    ])
    provider = make_provider(_fork_config())

    assert [rule["id"] for rule in provider._fork.rules()] == ["r-ok"]
    verdict = provider._fork.resolve_now("arg", "read_file", args={"path": "C:/repo/doc/a.md"})
    assert verdict is not None and verdict.decision == "rewrite"


# -- retry guard ---------------------------------------------------------------

def test_retry_guard_blocks_after_the_limit(make_provider):
    provider = make_provider(_fork_config(fork_retry_limit=2))
    fork = provider._fork
    args = {"command": "pytest -q tests/test_fork.py"}

    fork.observe_tool("s", "terminal", args, error="exit 1")
    assert fork.retry_guard("terminal", args) is None  # one failure: try again
    fork.observe_tool("s", "terminal", args, error="exit 1")
    guard = fork.retry_guard("terminal", args)  # two failures: refuse the third attempt
    assert guard is not None and guard.decision == "block"
    assert "exit 1" in guard.reason
    # The refusal is in the ledger as applied, with the guard as its source.
    rows = provider._store.fork_ledger()
    assert rows and rows[0]["source"] == "guard" and rows[0]["applied"] == 1


def test_retry_guard_resets_after_a_success(make_provider):
    provider = make_provider(_fork_config(fork_retry_limit=2))
    fork = provider._fork
    args = {"command": "make test"}
    fork.observe_tool("s", "terminal", args, error="boom")
    fork.observe_tool("s", "terminal", args, error="boom")
    fork.observe_tool("s", "terminal", args)  # success clears the streak
    assert fork.retry_guard("terminal", args) is None


def test_the_guard_is_off_while_forks_are_shadow(make_provider):
    provider = make_provider(_fork_config(forks="shadow"))
    fork = provider._fork
    args = {"command": "make test"}
    fork.observe_tool("s", "terminal", args, error="boom")
    fork.observe_tool("s", "terminal", args, error="boom")
    assert fork.retry_guard("terminal", args) is None


# -- result compression --------------------------------------------------------

def _compress_rule(plugin_package, tool="read_file", args=None):
    args = args or {"path": "C:/repo/logs/a.txt"}
    return {"id": "r-scope-1", "kind": "scope", "key": plugin_package.fork_rules.fork_key(
                "scope", tool, args=args), "decision": "compress", "strength": "high",
            "action": {}}


def test_compress_marks_the_result_and_keeps_line_numbers(make_provider, tmp_path,
                                                          plugin_package):
    args = {"path": "C:/repo/logs/a.txt"}
    _write_rules(tmp_path, [_compress_rule(plugin_package, args=args)])
    provider = make_provider(_fork_config(fork_compress_chars=400))
    body = "\n".join(f"line {index} detail" for index in range(1, 200))

    out = provider._fork.transform("s", "read_file", args, body, status="ok")

    assert out is not None
    assert out.startswith("[jev:rule=r-scope-1 original=")
    assert "lines omitted]" in out
    assert "1: line 1 detail" in out          # the head keeps its original numbers
    assert "199: line 199 detail" in out      # and so does the tail
    assert len(out) < len(body)


def test_transform_returns_none_for_error_results(make_provider, tmp_path, plugin_package):
    args = {"path": "C:/repo/logs/a.txt"}
    _write_rules(tmp_path, [_compress_rule(plugin_package, args=args)])
    provider = make_provider(_fork_config(fork_compress_chars=400))

    assert provider._fork.transform("s", "read_file", args, "x" * 5000, status="error") is None
    assert provider._fork.transform("s", "read_file", args, "x" * 5000, status="timeout") is None


def test_transform_returns_none_when_no_rule_matches(make_provider):
    provider = make_provider(_fork_config(fork_compress_chars=10))
    assert provider._fork.transform("s", "read_file", {"path": "C:/x/y"}, "x" * 5000) is None


# -- the pre_tool hook merge ---------------------------------------------------

def test_the_hook_applies_a_rewrite_as_a_patch(make_provider, tmp_path, plugin_package):
    args = {"path": "C:/repo/doc/draft.md"}
    key = _arg_key(plugin_package, "read_file", args)
    _write_rules(tmp_path, [{"id": "r-arg-1", "kind": "arg", "key": key,
                             "decision": "rewrite", "strength": "exact",
                             "action": {"args": {"path": "C:/repo/doc/live.md"}}}])
    provider = make_provider(_fork_config())

    result = plugin_package._on_pre_tool_call(
        tool_name="read_file", session_id="session-test", args=args, provider=provider)

    # A patch, not the whole dict: the host shallow-merges it into the original arguments.
    assert result == {"action": "modify", "args": {"path": "C:/repo/doc/live.md"}}


def test_the_hook_leaves_an_unmatched_call_alone(make_provider, plugin_package):
    provider = make_provider(_fork_config())
    result = plugin_package._on_pre_tool_call(
        tool_name="read_file", session_id="session-test",
        args={"path": "C:/repo/doc/other.md"}, provider=provider)
    assert result is None


def test_the_hook_never_forks_its_own_tools(make_provider, tmp_path, plugin_package):
    args = {"path": "C:/repo/doc/draft.md"}
    key = _arg_key(plugin_package, "minir_search", args)
    _write_rules(tmp_path, [{"id": "r-arg-2", "kind": "arg", "key": key,
                             "decision": "rewrite", "strength": "exact",
                             "action": {"args": {"path": "C:/repo/doc/live.md"}}}])
    provider = make_provider(_fork_config())
    assert provider._fork.resolve_now("arg", "minir_search", args=args) is None


def test_the_ledger_records_only_non_allow_forks(make_provider, tmp_path, plugin_package):
    provider = make_provider(_fork_config())
    plugin_package._on_pre_tool_call(tool_name="read_file", session_id="s",
                                     args={"path": "C:/repo/doc/a.md"}, provider=provider)
    assert provider._store.fork_ledger() == []   # a split is not an event

    args = {"path": "C:/repo/doc/a.md"}
    _write_rules(tmp_path, [{"id": "r-arg-3", "kind": "arg",
                             "key": _arg_key(plugin_package, "read_file", args),
                             "decision": "rewrite", "strength": "exact",
                             "action": {"args": {"path": "C:/repo/doc/b.md"}}}])
    provider._fork.load(str(provider._fork.scope()))
    plugin_package._on_pre_tool_call(tool_name="read_file", session_id="s", args=args,
                                     provider=provider)

    rows = provider._store.fork_ledger()
    assert len(rows) == 1 and rows[0]["decision"] == "rewrite" and rows[0]["applied"] == 1


# -- the gate ------------------------------------------------------------------

def _stub_consult(plugin_package, monkeypatch, answer=None, calls=None):
    def _consult(self, question, *, session, timeout):
        if calls is not None:
            calls.append((question, session, timeout))
        return answer

    monkeypatch.setattr(plugin_package.fork_dispatch.ForkDispatcher, "_consult", _consult)


def test_gate_go_returns_none(make_provider, plugin_package, monkeypatch):
    _stub_consult(plugin_package, monkeypatch, {"answer": "GO — the branch is clean."})
    provider = make_provider(_fork_config(gates="merge"))

    verdict = provider._fork.resolve_gate(
        "s", "terminal", {"command": "git merge feature"})

    assert verdict is None
    rows = provider._store.fork_ledger()
    assert rows and rows[0]["outcome"] == "go" and rows[0]["applied"] == 0


def test_gate_no_go_blocks(make_provider, plugin_package, monkeypatch):
    _stub_consult(plugin_package, monkeypatch,
                  {"answer": "NO-GO — the tests for this change never ran (target: current_step)."})
    provider = make_provider(_fork_config(gates="merge"))

    verdict = provider._fork.resolve_gate(
        "s", "terminal", {"command": "git merge feature"})

    assert verdict is not None and verdict.decision == "block"
    assert "current_step" in verdict.reason
    assert provider._store.fork_ledger()[0]["applied"] == 1


def test_gate_timeout_blocks_with_a_named_reason(make_provider, plugin_package, monkeypatch):
    _stub_consult(plugin_package, monkeypatch, None)  # run_with_timeout: no answer
    provider = make_provider(_fork_config(gates="merge", gate_timeout_s=90))

    verdict = provider._fork.resolve_gate(
        "s", "terminal", {"command": "git rebase main"})

    assert verdict is not None and verdict.decision == "block"
    assert "timed out" in verdict.reason and "90s" in verdict.reason


def test_gate_shadow_records_without_blocking(make_provider, plugin_package, monkeypatch):
    _stub_consult(plugin_package, monkeypatch, {"answer": "NO-GO — missing a test."})
    provider = make_provider(_fork_config(gates="shadow"))

    assert provider._fork.resolve_gate("s", "terminal",
                                       {"command": "git merge feature"}) is None
    rows = provider._store.fork_ledger()
    assert rows and rows[0]["decision"] == "block" and rows[0]["applied"] == 0


def test_gate_is_not_consulted_for_an_ordinary_tool(make_provider, plugin_package, monkeypatch):
    calls = []
    _stub_consult(plugin_package, monkeypatch, {"answer": "GO"}, calls=calls)
    provider = make_provider(_fork_config(gates="merge"))

    for command in ("git status", "git log --oneline -3", "pytest -q"):
        assert provider._fork.is_integration("terminal", {"command": command}) is False
        assert provider._fork.resolve_gate("s", "terminal", {"command": command}) is None
    assert plugin_package._on_pre_tool_call(
        tool_name="read_file", session_id="s", args={"path": "C:/x"}, provider=provider) is None
    assert calls == []


def test_a_gate_without_an_adviser_does_not_block_every_merge(make_provider, plugin_package,
                                                             monkeypatch):
    calls = []
    _stub_consult(plugin_package, monkeypatch, {"answer": "NO-GO"}, calls=calls)
    provider = make_provider(_fork_config(gates="merge", enabled=False))

    assert provider._fork.resolve_gate("s", "terminal", {"command": "git merge x"}) is None
    assert calls == []  # nobody to ask, so nothing was asked and nothing was blocked


def test_retry_and_gate_are_not_rule_kinds(plugin_package):
    for kind in ("retry", "gate"):
        assert kind not in plugin_package.fork_rules.KINDS
        assert plugin_package.fork_rules.normalize_rule({
            "kind": kind, "key": f"{kind}:terminal:{{}}", "decision": "rewrite",
            "strength": "high", "action": {"args": {"command": "x"}},
        }) is None
    # Their keys still exist: the retry counter and the gate verdict need a namespace.
    assert plugin_package.fork_rules.fork_key("retry", "terminal", args={"command": "x"}).startswith(
        "retry:terminal:")
    assert plugin_package.fork_rules.fork_key("gate", "terminal", args={"command": "x"}).startswith(
        "gate:terminal:")


# -- route ---------------------------------------------------------------------

def test_inline_node_must_have_a_registered_handler(make_provider, plugin_package):
    provider = make_provider(_fork_config())
    fork = provider._fork
    node = {"id": "n1", "stage": "fork", "goal": "split the repo", "mechanical": True}
    key = plugin_package.fork_rules.fork_key("route", goal=node["goal"])
    with fork._lock:
        fork._rules.setdefault("route", []).append(
            {"id": "r-route-1", "kind": "route", "key": key, "decision": "inline",
             "strength": "high"})

    decision = fork.route(node)
    assert decision["decision"] == "dispatch"
    assert "no inline handler" in decision["reason"]

    fork.register_handler("fork", lambda item: {"ok": True})
    assert fork.route(node)["decision"] == "inline"


def test_route_falls_back_to_default_when_the_judge_times_out(make_provider):
    provider = make_provider(_fork_config())
    fork = provider._fork
    fork.register_handler("merge", lambda node: None)
    node = {"id": "m1", "stage": "merge", "goal": "merge the branch", "mechanical": True}

    decision = fork.route(node, judge=lambda item: None)  # the slow path gave nothing

    assert decision["decision"] == "dispatch"
    assert decision["handler"] is None


def test_route_needs_a_mechanical_stage(make_provider, plugin_package):
    provider = make_provider(_fork_config())
    fork = provider._fork
    fork.register_handler("plan", lambda node: None)
    key = plugin_package.fork_rules.fork_key("route", goal="fan out")
    with fork._lock:
        fork._rules.setdefault("route", []).append(
            {"id": "r-route-2", "kind": "route", "key": key, "decision": "inline",
             "strength": "high"})
    # A stage outside INLINE_STAGES never runs inline, whatever the rule says.
    decision = fork.route({"stage": "plan", "goal": "fan out", "mechanical": True})
    assert decision["decision"] == "dispatch"
    assert "mechanical acceptance" in decision["reason"]


# -- learned rules -------------------------------------------------------------

def test_overturn_demotes_then_expires(make_provider, plugin_package):
    provider = make_provider(_fork_config())
    fork = provider._fork
    key = plugin_package.fork_rules.fork_key("arg", "read_file",
                                            args={"path": "C:/repo/doc/a.md"})
    verdict = {"id": "f1", "kind": "arg", "key": key, "decision": "rewrite",
               "action": {"args": {"path": "C:/repo/doc/b.md"}},
               "reason": "the live draft is b.md", "learn": True}
    assert fork.apply_review_forks("s", [verdict]) == 1
    rule = fork.rules("arg")[0]
    assert rule["strength"] == "low"
    assert fork.resolve_now("arg", "read_file", args={"path": "C:/repo/doc/a.md"}) is None

    provider._store.record_fork_hit(rule["id"])
    provider._store.record_fork_hit(rule["id"])
    assert fork.refresh_learned() >= 1
    promoted = fork.rules("arg")[0]
    assert promoted["strength"] == "medium"
    assert fork.resolve_now("arg", "read_file", args={"path": "C:/repo/doc/a.md"}) is not None

    provider._store.record_fork_overturn(promoted["id"])
    fork.refresh_learned()
    assert fork.rules("arg")[0]["strength"] == "low"

    provider._store.record_fork_overturn(promoted["id"])
    fork.refresh_learned()
    assert fork.rules("arg") == []  # two overturns: the rule is gone
