"""``minir_aiagent_search``: the caller's own layer only, and the shapes it exposes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def history(plugin_package, tmp_path):
    from contextlib import contextmanager

    buffer = plugin_package.transcript.TranscriptBuffer(
        persist_dir=tmp_path / "sessions", run_dir=tmp_path / "run", profile="default")
    provider = SimpleNamespace(_transcript=buffer, _profile="default",
                               _session_id="session-test")
    module = plugin_package.agent_history

    @contextmanager
    def bound(layer: str, session: str = "session-test"):
        tokens = module.layer_context(layer, session)
        try:
            yield
        finally:
            module.reset_layer(tokens)

    buffer.append_event("base", "session-test", "user", text="deploy the staging tag",
                        profile="default")
    buffer.append_event("base", "session-test", "assistant", text="checking the manifest",
                        tool_calls=[{"id": "c1", "name": "exec", "args": "{}"}], profile="default")
    buffer.append_event("base", "session-test", "tool", text="", result="staging tag is 1.2.3",
                        name="exec", tool_call_id="c1", profile="default")
    buffer.append_event("base", "older-run", "user", text="an earlier base run", profile="default")
    buffer.append_event("dialectic", "session-test", "user", text="a dialectic secret",
                        profile="default")
    yield buffer, provider, module, bound
    buffer.close()


def _call(module, provider, *args, **kwargs):
    return json.loads(module.handle(*args, provider=provider, **kwargs))


def test_no_layer_bound_refuses_instead_of_reading_every_layer(history):
    _buffer, provider, module, _bound = history
    payload = _call(module, provider, {"mode": "messages"})
    assert payload["success"] is False
    assert payload["error"]["type"] == "layer_unbound"


def test_the_schema_takes_no_layer_or_store_argument(history):
    _buffer, _provider, module, _bound = history
    properties = module.SCHEMA["parameters"]["properties"]
    assert "layer" not in properties
    assert "profile" not in properties
    assert "path" not in properties
    assert module.SCHEMA["name"] == "minir_aiagent_search"


def test_messages_reads_the_callers_stream_only(history):
    _buffer, provider, module, bound = history
    with bound("base"):
        payload = _call(module, provider, {"mode": "messages", "limit": 10})
    data = payload["data"]
    assert payload["success"] is True
    assert data["layer"] == "base" and data["profile"] == "default"
    assert [row["role"] for row in data["rows"]] == ["user", "assistant", "tool"]
    assert data["coverage"] == [1, 3] and data["source"] == "buffer"
    assert "a dialectic secret" not in json.dumps(data)


def test_an_explicit_session_still_cannot_cross_layers(history):
    _buffer, provider, module, bound = history
    with bound("base"):
        payload = _call(module, provider, {"mode": "messages", "session_id": "session-test",
                                           "limit": 10})
    assert all(row["layer"] == "base" for row in payload["data"]["rows"])
    with bound("base"):
        other = _call(module, provider, {"mode": "messages", "session_id": "dialectic",
                                         "limit": 10})
    assert other["data"]["rows"] == []


def test_search_matches_text_and_tool_results(history):
    _buffer, provider, module, bound = history
    with bound("base"):
        payload = _call(module, provider, {"mode": "search", "query": "staging"})
    rows = payload["data"]["rows"]
    assert [row["role"] for row in rows] == ["user", "tool"]
    with bound("base"):
        payload = _call(module, provider, {"mode": "search", "query": "dialectic secret"})
    assert payload["data"]["rows"] == []


def test_around_and_stats(history):
    _buffer, provider, module, bound = history
    with bound("base"):
        around = _call(module, provider, {"mode": "around", "seq": 2, "window": 1})
        stats = _call(module, provider, {"mode": "stats"})
    assert [row["seq"] for row in around["data"]["rows"]] == [1, 2, 3]
    assert stats["data"]["layer"] == "base"
    assert stats["data"]["count"] >= 1
    assert all(stream["layer"] == "base" for stream in stats["data"]["streams"])


def test_scope_layer_reads_the_callers_own_sessions(history):
    _buffer, provider, module, bound = history
    with bound("base"):
        payload = _call(module, provider, {"mode": "messages", "scope": "layer", "limit": 10})
    assert set(payload["data"]["sessions"]) == {"session-test", "older-run"}
    assert all(row["layer"] == "base" for row in payload["data"]["rows"])


def test_the_registry_tool_forwards_to_the_live_provider(history, plugin_package):
    _buffer, provider, module, bound = history
    registered: dict[str, dict] = {}

    class Ctx:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs

    plugin_package.plugin_tools.register(Ctx(), provider)
    entry = registered["minir_aiagent_search"]
    assert entry["toolset"] == "minir"
    with bound("base"):
        payload = json.loads(entry["handler"]({"mode": "messages", "limit": 1}))
    assert payload["success"] is True
    assert payload["data"]["layer"] == "base"
    assert payload["data"]["rows"][0]["seq"] == 1


# -- a watched run's trace ----------------------------------------------------

_CHILD = "session-test:minir:researcher:job-1"


def test_a_child_trace_reads_the_runs_own_stream(history):
    """The child id names its layer, so the reader does not have to be run inside that layer."""
    buffer, provider, module, bound = history
    buffer.append_event("researcher", _CHILD, "user", text="scan the repo",
                        profile="default", run_id="job-1")
    buffer.append_event("researcher", _CHILD, "tool", kind="msg", name="search_files",
                        result="42 files", profile="default", run_id="job-1")

    with bound("base"):  # the caller's own layer is a different one
        payload = _call(module, provider, {"mode": "messages", "session_id": _CHILD})

    rows = payload["data"]["rows"]
    assert payload["data"]["layer"] == "researcher"
    assert [row["role"] for row in rows] == ["user", "tool"]
    assert {row["run_id"] for row in rows} == {"job-1"}
    assert "a dialectic secret" not in json.dumps(payload["data"])


def test_a_finished_run_falls_back_to_the_final_stream_by_run_id(history):
    """No realtime stream (or it was pruned): the parent's stream holds the run, under its id."""
    buffer, provider, module, bound = history
    child = "session-test:minir:researcher:job-2"
    buffer.record_aux("researcher", "session-test", [
        {"role": "user", "content": "the run prompt"},
        {"role": "assistant", "content": "the run answer"}],
        system_message="sys", run_id="job-2", profile="default")
    buffer.record_aux("researcher", "session-test", [
        {"role": "user", "content": "another run's prompt"}],
        system_message="sys", run_id="job-1", profile="default")

    with bound("base"):
        payload = _call(module, provider, {"mode": "messages", "session_id": child})

    texts = [row.get("text") for row in payload["data"]["rows"] if row.get("role") == "user"]
    assert texts == ["the run prompt"], "run_id separates this run from the agent's other runs"
    assert all(row["run_id"] == "job-2" for row in payload["data"]["rows"])
