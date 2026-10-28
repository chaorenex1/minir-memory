"""Per-route usage accounting into the host ledger (``session_model_usage.task``).

The gap these pin: a layer pass runs as a background agent loop, outside any published
``agent.aux_accounting`` context, so the host never wrote its row and per-layer cost had to be
reconstructed from prompt caps.
"""

from __future__ import annotations


def test_record_maps_usage_and_task(plugin_package, monkeypatch):
    accounting = plugin_package.accounting
    written: dict = {}

    class FakeDb:
        def record_auxiliary_usage(self, session, task, **kwargs):
            written.update({"session": session, "task": task, **kwargs})

    monkeypatch.setattr(accounting, "_open_db", lambda path: FakeDb())
    monkeypatch.setattr(accounting, "_cost", lambda *args: 0.25)

    ok = accounting.record(
        "/tmp/state.db", "sess-1", "minir:base", model="glm-5.3-flash",
        provider="opencode-go",
        usage={"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 40,
               "reasoning_tokens": 5},
        api_calls=4)

    assert ok is True
    assert written["session"] == "sess-1"
    assert written["task"] == "minir:base"
    assert written["input_tokens"] == 100
    assert written["output_tokens"] == 20
    assert written["cache_read_tokens"] == 40
    assert written["reasoning_tokens"] == 5
    assert written["api_call_count"] == 4
    assert written["model"] == "glm-5.3-flash"
    assert written["billing_provider"] == "opencode-go"
    assert written["estimated_cost_usd"] == 0.25


def test_prompt_tokens_become_net_input(plugin_package):
    accounting = plugin_package.accounting

    counts = accounting.counts({"prompt_tokens": 1000, "completion_tokens": 30,
                                "cache_read_tokens": 600})

    assert counts == {"input_tokens": 400, "output_tokens": 30, "cache_read_tokens": 600,
                      "cache_write_tokens": 0, "reasoning_tokens": 0}


def test_record_refuses_incomplete_rows(plugin_package, monkeypatch):
    accounting = plugin_package.accounting
    opened: list[str] = []
    monkeypatch.setattr(accounting, "_open_db", lambda path: opened.append(path) or None)

    assert accounting.record("", "sess", "minir:base", usage={"input_tokens": 1}) is False
    assert accounting.record("/tmp/state.db", "", "minir:base", usage={"input_tokens": 1}) is False
    assert accounting.record("/tmp/state.db", "sess", "", usage={"input_tokens": 1}) is False
    assert accounting.record("/tmp/state.db", "sess", "minir:base", usage={}) is False

    assert opened == []  # nothing to bill: never open the store


def test_scope_sets_and_restores(plugin_package):
    accounting = plugin_package.accounting

    assert accounting.current() is None
    with accounting.accounting_scope("/tmp/state.db", "sess"):
        assert accounting.current() == ("/tmp/state.db", "sess")
    assert accounting.current() is None
    with accounting.accounting_scope("", "sess"):  # no store to bill
        assert accounting.current() is None


def test_agent_run_is_billed_to_the_scoped_conversation(plugin_package, monkeypatch):
    agent_loop = plugin_package.agent_loop
    accounting = plugin_package.accounting
    seen: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(accounting, "record", lambda *args, **kwargs: seen.append((args, kwargs)))

    run = agent_loop.AgentRun(answer="ok", api_calls=3, model="glm-5.3-flash",
                              provider="opencode-go", toolsets=(),
                              usage={"input_tokens": 10})

    with accounting.accounting_scope("/tmp/state.db", "sess"):
        agent_loop._account(run, "base")
        agent_loop._account(run, "adviser", accounting.ADVISER_TASK)

    assert seen[0][0] == ("/tmp/state.db", "sess", "minir:base")
    assert seen[0][1]["api_calls"] == 3
    assert seen[0][1]["model"] == "glm-5.3-flash"
    assert seen[1][0][2] == "adviser"

    seen.clear()
    agent_loop._account(run, "base")  # unscoped run: no conversation to charge
    assert seen == []
