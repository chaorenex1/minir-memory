"""Adviser surface: the auxiliary route and the read-only host session search.

The model-visible consult tool was deleted with its schema, handler, registration and prompt
section; the worker's internal ``_adviser_consult`` path is covered by ``test_worker.py``. These
tests cover what remains: the route's config resolution and the tool registration split.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ADVISER = "adviser"


@pytest.fixture()
def host(monkeypatch):
    state = {
        "config": {"model": {"provider": "deepseek", "default": "deepseek-v4-flash"},
                   "auxiliary": {ADVISER: {}}},
    }
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config_readonly = lambda: state["config"]
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    return state


@pytest.fixture()
def adviser(plugin_package, host):
    return plugin_package.adviser


class FakeCtx:
    """The plugin-context surface ``adviser.register`` uses, with the calls recorded."""

    def __init__(self, fail_aux: bool = False, refuse: str = ""):
        self.calls: list[tuple[str, dict]] = []
        self.memory_providers: list[object] = []
        self.hooks: list[str] = []
        self.fail_aux = fail_aux
        self.refuse = refuse

    def register_memory_provider(self, provider):
        self.memory_providers.append(provider)

    def register_hook(self, name, handler):
        self.hooks.append(name)

    def register_auxiliary_task(self, key, **kwargs):
        if self.fail_aux or key == self.refuse:
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


def test_each_layer_route_reads_its_own_block(adviser, host):
    """Base, the review and extraction pin independently; the adviser block is not a fallback."""
    host["config"]["auxiliary"][adviser.DIALECTIC_TASK] = {"model": "strong"}
    host["config"]["auxiliary"][adviser.KNOWLEDGE_TASK] = {"model": "cheap"}

    assert adviser.load_config(adviser.DIALECTIC_TASK) == {"toolsets": [], "model": "strong"}
    assert adviser.load_config(adviser.KNOWLEDGE_TASK) == {"toolsets": [], "model": "cheap"}
    assert adviser.load_config(adviser.CARD_VIEW_TASK) == {"toolsets": []}
    assert adviser.load_config() == {"toolsets": []}


def test_missing_config_keeps_the_plugin_defaults(adviser, monkeypatch):
    broken = types.ModuleType("hermes_cli.config")

    def explode():
        raise RuntimeError("no config")

    broken.load_config_readonly = explode
    monkeypatch.setitem(sys.modules, "hermes_cli.config", broken)

    assert adviser.load_config() == {"toolsets": []}


# -- deleted consult surface ---------------------------------------------------


def test_the_consult_tool_is_gone(adviser):
    """A half-deleted tool (schema without handler, or a name in the toolset) fails loudly."""
    for symbol in ("TOOL_NAME", "SCHEMA", "handle", "handle_async", "run", "run_async"):
        assert not hasattr(adviser, symbol), symbol


def test_register_declares_the_routes_and_the_session_search(adviser):
    ctx = FakeCtx()

    adviser.register(ctx)

    tools = [entry for kind, entry in ctx.calls if kind == "tool"]
    routes = [entry for kind, entry in ctx.calls if kind == "auxiliary_task"]
    assert [route["key"] for route in routes] == [
        "adviser", "minir_dialectic", "minir_card_view", "minir_knowledge"]
    assert [route["display_name"] for route in routes] == [
        "Adviser", "Minir dialectic", "Minir card view", "Minir knowledge"]
    assert all(route["defaults"] == {"toolsets": []} for route in routes)
    assert len(tools) == 1
    tool = tools[0]
    assert (tool["name"], tool["toolset"]) == ("minir_hermes_session_search", "minir")
    assert tool["schema"]["name"] == "minir_hermes_session_search"
    assert tool["is_async"] is False
    assert tool["schema"]["parameters"]["properties"]["mode"]["enum"] == [
        "search", "sessions", "messages", "around", "stats"]
    # The handler injects the caller class and platform; it is not the bare module function.
    assert callable(tool["handler"])


def test_one_refused_key_costs_only_that_route(adviser):
    """A key another plugin owns loses its route, not the other three or the search."""
    ctx = FakeCtx(refuse="minir_card_view")

    adviser.register(ctx)

    assert [entry["key"] for kind, entry in ctx.calls if kind == "auxiliary_task"] == [
        "adviser", "minir_dialectic", "minir_knowledge"]
    assert [entry["name"] for kind, entry in ctx.calls if kind == "tool"] == [
        "minir_hermes_session_search"]


def test_manifest_declares_every_registered_adviser_tool(adviser, plugin_package):
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

    assert registered == {"minir_hermes_session_search"}
    assert registered <= set(declared)
    assert set(plugin_package.plugin_tools.TOOL_NAMES) <= set(declared)
    assert "adviser_consult" not in declared
    assert "adviser_session_search" not in declared


def test_provider_registration_survives_a_refused_auxiliary_key(plugin_package):
    """The memory provider is the reason the directory is loaded: never lose it to the adviser."""
    ctx = FakeCtx(fail_aux=True)

    plugin_package.register(ctx)

    assert len(ctx.memory_providers) == 1
    assert ctx.hooks == ["on_session_start", "on_session_reset", "on_session_finalize",
                         "pre_llm_call", "post_llm_call", "post_tool_call", "pre_tool_call",
                         "pre_verify", "transform_llm_output", "on_interim_message",
                         "on_stream_delta", "post_api_request", "api_request_error"]
    # The refused auxiliary key costs the adviser route only; the plugin's own minir toolset and
    # the read-only session search are registered before that call and must survive it.
    assert [call[1]["name"] for call in ctx.calls if call[0] == "tool"] == [
        "minir_search", "minir_remember", "minir_forget", "minir_todo", "minir_stats",
        "minir_agents", "minir_aiagent_search", "minir_hermes_session_search"]
