"""Adviser surface — config block, message framing, handlers, registration.

The loop itself is covered by ``test_agent_loop.py``; here ``agent_loop`` is replaced so these
tests are about what the adviser adds: which knobs it reads from ``auxiliary.adviser``, how it
frames the message, the JSON contract of the two handlers, and the route/tool registration.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

ADVISER = "adviser"
RUN = {"answer": "do the simple thing", "api_calls": 3, "model": "m",
       "provider": "p", "toolsets": ["file"]}


@pytest.fixture()
def host(monkeypatch):
    """Config stand-in plus fake run functions recording what the adviser passed them."""
    state = {
        "config": {"model": {"provider": "deepseek", "default": "deepseek-v4-flash"},
                   "auxiliary": {ADVISER: {}}},
        "sync_calls": [],
        "async_calls": [],
        "fail_with": None,
    }

    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config_readonly = lambda: state["config"]
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    return state


@pytest.fixture()
def adviser(plugin_package, monkeypatch, host):
    module = plugin_package.adviser

    class FakeRun:
        def as_dict(self):
            return dict(RUN)

    def run(message, **kwargs):
        host["sync_calls"].append({"message": message, **kwargs})
        if host["fail_with"] is not None:
            raise host["fail_with"]
        return FakeRun()

    async def run_async(message, **kwargs):
        host["async_calls"].append({"message": message, **kwargs})
        if host["fail_with"] is not None:
            raise host["fail_with"]
        return FakeRun()

    monkeypatch.setattr(module.agent_loop, "run", run, raising=True)
    monkeypatch.setattr(module.agent_loop, "run_async", run_async, raising=True)
    return module


class FakeCtx:
    """The plugin-context surface ``adviser.register`` uses, with the calls recorded."""

    def __init__(self, fail_aux: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.memory_providers: list[object] = []
        self.hooks: list[str] = []
        self.fail_aux = fail_aux

    def register_memory_provider(self, provider):
        self.memory_providers.append(provider)

    def register_hook(self, name, handler):
        self.hooks.append(name)

    def register_auxiliary_task(self, key, **kwargs):
        if self.fail_aux:
            raise ValueError("another plugin's key")
        self.calls.append(("auxiliary_task", {"key": key, **kwargs}))

    def register_tool(self, **kwargs):
        self.calls.append(("tool", kwargs))


# -- config -------------------------------------------------------------------

def test_config_reads_the_adviser_block(adviser, host):
    host["config"]["auxiliary"][ADVISER] = {"provider": "openrouter", "model": "some/other",
                                            "toolsets": ["file"]}

    assert adviser.load_config() == {"toolsets": ["file"], "provider": "openrouter",
                                     "model": "some/other"}


def test_missing_config_keeps_the_plugin_defaults(adviser, monkeypatch):
    broken = types.ModuleType("hermes_cli.config")

    def explode():
        raise RuntimeError("no config")

    broken.load_config_readonly = explode
    monkeypatch.setitem(sys.modules, "hermes_cli.config", broken)

    assert adviser.load_config() == {"toolsets": []}


# -- message framing + run ----------------------------------------------------

def test_run_frames_the_message_and_forwards_the_route(adviser, host):
    host["config"]["auxiliary"][ADVISER] = {"provider": "openrouter", "model": "some/other",
                                            "toolsets": ["file"]}

    data = adviser.run("Ship it?", context="tests pass")

    assert data == RUN
    (call,) = host["sync_calls"]
    assert call == {"message": "Ship it?", "context": "tests pass", "model": "some/other",
                    "provider": "openrouter", "toolsets": ["file"],
                    "system_message": adviser._ADVISER_BRIEF}


def test_tool_toolsets_beat_the_configured_ones(adviser, host):
    host["config"]["auxiliary"][ADVISER] = {"toolsets": ["web"]}

    adviser.run("q", toolsets=["file"])

    assert host["sync_calls"][0]["toolsets"] == ["file"]


def test_empty_config_passes_empty_route_overrides(adviser, host):
    """Empty overrides mean "let agent_loop resolve it", not "pin an empty provider"."""
    adviser.run("q")

    call = host["sync_calls"][0]
    assert (call["model"], call["provider"]) == ("", "")


def test_run_async_awaits_the_async_loop(adviser, host):
    data = asyncio.run(adviser.run_async("Ship it?", context="tests pass"))

    assert data == RUN
    assert host["sync_calls"] == []
    (call,) = host["async_calls"]
    assert call["message"] == "Ship it?"
    assert call["context"] == "tests pass"
    assert call["system_message"] == adviser._ADVISER_BRIEF


# -- tool handler -------------------------------------------------------------

def test_handler_requires_a_question(adviser, host):
    payload = json.loads(adviser.handle({}))

    assert payload["success"] is False
    assert payload["error"]["type"] == "invalid_args"
    assert host["sync_calls"] == []


def test_async_handler_requires_a_question(adviser, host):
    payload = json.loads(asyncio.run(adviser.handle_async({})))

    assert payload["success"] is False
    assert payload["error"]["type"] == "invalid_args"
    assert host["async_calls"] == []


def test_handler_reports_success(adviser, host):
    payload = json.loads(adviser.handle({"question": "q", "context": "c"}))

    assert payload == {"success": True, "data": RUN}


def test_async_handler_reports_success(adviser, host):
    payload = json.loads(asyncio.run(adviser.handle_async({"question": "q", "context": "c"})))

    assert payload == {"success": True, "data": RUN}


def test_handler_turns_a_failure_into_json(adviser, host):
    host["fail_with"] = RuntimeError("no credentials")

    payload = json.loads(adviser.handle({"question": "q"}))

    assert payload["success"] is False
    assert payload["error"]["type"] == "adviser_failed"
    assert "no credentials" in payload["error"]["message"]


def test_async_handler_turns_a_failure_into_json(adviser, host):
    host["fail_with"] = RuntimeError("no credentials")

    payload = json.loads(asyncio.run(adviser.handle_async({"question": "q"})))

    assert payload["success"] is False
    assert payload["error"]["type"] == "adviser_failed"
    assert "no credentials" in payload["error"]["message"]


def test_handlers_report_an_empty_answer(adviser, monkeypatch):
    class Blank:
        def as_dict(self):
            return {**RUN, "answer": ""}

    monkeypatch.setattr(adviser.agent_loop, "run", lambda message, **kwargs: Blank())

    payload = json.loads(adviser.handle({"question": "q"}))

    assert payload["success"] is False
    assert payload["error"]["type"] == "empty_answer"
    assert payload["data"]["answer"] == ""


def test_the_schema_offers_only_caller_owned_knobs(adviser):
    """message/context/toolsets — model/provider are config, the budget is the loop's."""
    properties = adviser.SCHEMA["parameters"]["properties"]

    assert set(properties) == {"question", "context", "toolsets"}
    assert adviser.SCHEMA["parameters"]["required"] == ["question"]


# -- registration -------------------------------------------------------------

def test_register_declares_the_route_and_both_tools(adviser):
    ctx = FakeCtx()

    adviser.register(ctx)

    (kind, entry), (tool_kind, tool), (search_kind, search) = ctx.calls
    assert (kind, entry["key"]) == ("auxiliary_task", "adviser")
    assert entry["display_name"] == "Adviser"
    assert entry["defaults"] == {"toolsets": []}
    assert (tool_kind, tool["name"], tool["toolset"]) == ("tool", "adviser_consult", "adviser")
    assert tool["schema"]["name"] == "adviser_consult"
    # The registered handler must be the coroutine function, and the flag must agree with it:
    # the host bridges async handlers (tools.registry.dispatch -> _run_async).
    assert tool["handler"] is adviser.handle_async
    assert tool["is_async"] is True
    assert (search_kind, search["name"], search["toolset"]) == (
        "tool", "adviser_session_search", "adviser")
    assert search["schema"]["name"] == "adviser_session_search"
    assert search["handler"] is adviser.session_search.handle
    assert search["is_async"] is False
    assert search["schema"]["parameters"]["properties"]["mode"]["enum"] == [
        "search", "sessions", "messages", "around", "stats"]


def test_manifest_declares_every_registered_adviser_tool(adviser):
    """``hermes plugins validate`` fails the manifest for an undeclared registered tool."""
    ctx = FakeCtx()
    adviser.register(ctx)
    registered = {call[1]["name"] for call in ctx.calls if call[0] == "tool"}

    declared = []
    in_block = False
    manifest = (Path(__file__).resolve().parents[1] / "plugin" / "minir"
                / "plugin.yaml").read_text(encoding="utf-8-sig")
    for line in manifest.splitlines():
        if line.startswith("provides_tools:"):
            in_block = True
            continue
        if in_block:
            if line.startswith("  - "):
                declared.append(line[4:].strip())
            elif line.strip() and not line.startswith(" "):
                break

    assert registered == {"adviser_consult", "adviser_session_search"}
    assert set(declared) == registered


def test_provider_registration_survives_a_refused_auxiliary_key(plugin_package):
    """The memory provider is the reason the directory is loaded: never lose it to the adviser."""
    ctx = FakeCtx(fail_aux=True)

    plugin_package.register(ctx)

    assert len(ctx.memory_providers) == 1
    assert ctx.hooks == ["on_session_start", "on_session_reset", "on_session_finalize",
                         "pre_llm_call", "post_llm_call", "post_tool_call"]
    assert ctx.calls == []
