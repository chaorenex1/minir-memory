"""``agent_loop`` — one AIAgent run, two entry points (sync + async).

The caller only passes message / context / model / provider / toolsets; the rest is resolved
internally the way the gateway resolves a message (``_resolve_model_and_runtime`` in
``plugins/platforms/feishu/feishu_comment.py``). These tests assert exactly that split:
which overrides reach the agent, and which values come from the host resolver.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types

import pytest


class FakeAgent:
    """Records how the loop constructs its AIAgent and whether it closed it."""

    instances: list["FakeAgent"] = []
    fail_with: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.prompt = ""
        self.system_message = "<unset>"
        self.closed = False
        FakeAgent.instances.append(self)

    def run_conversation(self, prompt, system_message=None, **_kwargs):
        self.prompt = prompt
        self.system_message = system_message
        if FakeAgent.fail_with is not None:
            raise FakeAgent.fail_with
        return {"final_response": "  do the simple thing\n", "api_calls": 3}

    def close(self):
        self.closed = True


@pytest.fixture()
def host(monkeypatch):
    """Stand-ins for every host module the loop resolves through."""
    FakeAgent.instances = []
    FakeAgent.fail_with = None

    state = {
        "gateway_model": "gateway-model",
        "config": {"model": {"provider": "deepseek", "default": "gateway-model"}},
        "runtime": {"api_key": "gateway-key", "base_url": "https://gateway.example/v1",
                    "provider": "deepseek", "api_mode": "chat", "credential_pool": None,
                    "request_overrides": {"temperature": 0}, "requested_provider": "deepseek",
                    "capabilities": {"vision": False}, "command": None, "args": None},
        "pinned": {"api_key": "pinned-key", "base_url": "https://pinned.example/v1",
                   "provider": "openrouter", "api_mode": "responses",
                   "credential_pool": {"pool": True}, "request_overrides": None,
                   "requested_provider": "openrouter", "capabilities": {},
                   "command": None, "args": []},
        "pinned_calls": [],
        "default_model_calls": [],
        "reasoning_calls": [],
        "config_loads": 0,
    }

    def load_gateway_config():
        state["config_loads"] += 1
        return state["config"]

    def resolve_gateway_model(config):
        return state["gateway_model"]

    def resolve_runtime_agent_kwargs():
        return dict(state["runtime"])

    def get_default_model_for_provider(provider):
        state["default_model_calls"].append(provider)
        return "provider-default-model"

    def resolve_runtime_provider(**kwargs):
        state["pinned_calls"].append(kwargs)
        return dict(state["pinned"])

    def resolve_reasoning_config(config, model):
        state["reasoning_calls"].append(model)
        return {"effort": "medium"}

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    gateway_run = types.ModuleType("gateway.run")
    gateway_run._load_gateway_config = load_gateway_config
    gateway_run._resolve_gateway_model = resolve_gateway_model
    gateway_run._resolve_runtime_agent_kwargs = resolve_runtime_agent_kwargs

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.get_default_model_for_provider = get_default_model_for_provider
    runtime_provider = types.ModuleType("hermes_cli.runtime_provider")
    runtime_provider.resolve_runtime_provider = resolve_runtime_provider

    constants = types.ModuleType("hermes_constants")
    constants.resolve_reasoning_config = resolve_reasoning_config

    run_agent = types.ModuleType("run_agent")
    run_agent.AIAgent = FakeAgent

    for name, module in (("gateway", gateway), ("gateway.run", gateway_run),
                         ("hermes_cli", hermes_cli), ("hermes_cli.models", models),
                         ("hermes_cli.runtime_provider", runtime_provider),
                         ("hermes_constants", constants), ("run_agent", run_agent)):
        monkeypatch.setitem(sys.modules, name, module)
    return state


@pytest.fixture()
def loop(plugin_package):
    return plugin_package.agent_loop


# -- what the gateway resolves ------------------------------------------------

def test_defaults_come_from_the_gateway_resolution(loop, host):
    result = loop.run("Should we ship?")

    assert result.model == "gateway-model"
    assert result.provider == "deepseek"
    (agent,) = FakeAgent.instances
    assert agent.kwargs["api_key"] == "gateway-key"
    assert agent.kwargs["base_url"] == "https://gateway.example/v1"
    assert agent.kwargs["api_mode"] == "chat"
    assert agent.kwargs["provider"] == "deepseek"
    assert agent.kwargs["request_overrides"] == {"temperature": 0}
    assert agent.kwargs["requested_provider"] == "deepseek"
    assert agent.kwargs["capabilities"] == {"vision": False}
    assert agent.kwargs["reasoning_config"] == {"effort": "medium"}
    assert host["reasoning_calls"] == ["gateway-model"]


def test_an_explicit_api_mode_wins_over_the_resolved_runtime(loop, host):
    """``model.openai_runtime: codex_app_server`` rewrites a named custom provider's api_mode;
    the adviser route can pin its own wire protocol instead."""
    loop.run("q", provider="custom:siliconflow", model="Qwen/Qwen3.5-4B",
             api_mode="chat_completions")

    (agent,) = FakeAgent.instances
    assert agent.kwargs["api_mode"] == "chat_completions"


def test_the_agent_is_built_like_the_feishu_comment_agent(loop, host):
    loop.run("q")

    (agent,) = FakeAgent.instances
    assert agent.kwargs["quiet_mode"] is True
    assert agent.kwargs["skip_context_files"] is True
    assert agent.kwargs["skip_memory"] is True
    assert agent.kwargs["skip_background_review"] is True
    assert agent.kwargs["load_soul_identity"] is True
    assert agent.kwargs["max_iterations"] == loop.MAX_ITERATIONS
    assert agent.kwargs["enabled_toolsets"] is None
    assert agent.closed is True


def test_system_message_is_forwarded_to_the_turn(loop, host):
    loop.run("q", system_message="  be blunt  ")

    (agent,) = FakeAgent.instances
    assert agent.system_message == "be blunt"


def test_no_system_message_means_the_host_default(loop, host):
    loop.run("q")

    (agent,) = FakeAgent.instances
    assert agent.system_message is None


def test_only_the_intended_constructor_kwargs_are_passed(loop, host):
    """A peer run must not inherit session identity, callbacks, checkpoints or budgets."""
    loop.run("q")

    (agent,) = FakeAgent.instances
    assert set(agent.kwargs) == {
        "model", "provider", "reasoning_config",
        "api_key", "base_url", "api_mode", "credential_pool", "request_overrides",
        "requested_provider", "capabilities", "command", "args",
        "enabled_toolsets", "max_iterations", "quiet_mode", "skip_context_files", "skip_memory",
        "skip_background_review", "load_soul_identity",
    }


def test_provider_default_model_is_the_fallback(loop, host):
    host["gateway_model"] = ""

    result = loop.run("q")

    assert result.model == "provider-default-model"
    assert host["default_model_calls"] == ["deepseek"]


# -- what the caller overrides ------------------------------------------------

def test_external_model_and_provider_become_the_route(loop, host):
    result = loop.run("q", model="pinned-model", provider="openrouter")

    assert (result.model, result.provider) == ("pinned-model", "openrouter")
    (call,) = host["pinned_calls"]
    assert call == {"requested": "openrouter", "target_model": "pinned-model"}
    (agent,) = FakeAgent.instances
    assert agent.kwargs["api_key"] == "pinned-key"
    assert agent.kwargs["base_url"] == "https://pinned.example/v1"
    assert agent.kwargs["api_mode"] == "responses"
    assert agent.kwargs["credential_pool"] == {"pool": True}
    assert agent.kwargs["provider"] == "openrouter"
    assert host["reasoning_calls"] == ["pinned-model"]


def test_model_override_without_a_provider_pin_keeps_the_gateway_route(loop, host):
    result = loop.run("q", model="another-model")

    assert (result.model, result.provider) == ("another-model", "deepseek")
    assert host["pinned_calls"] == []
    (agent,) = FakeAgent.instances
    assert agent.kwargs["api_key"] == "gateway-key"


def test_provider_equal_to_the_resolved_one_is_not_re_resolved(loop, host):
    loop.run("q", provider="deepseek")

    assert host["pinned_calls"] == []


def test_context_is_appended_to_the_message(loop, host):
    loop.run("Ship it?", context="tests pass")

    (agent,) = FakeAgent.instances
    assert agent.prompt == "Ship it?\n\n--- context ---\ntests pass"


def test_toolsets_are_forwarded(loop, host):
    result = loop.run("q", toolsets=["file", "web"])

    (agent,) = FakeAgent.instances
    assert agent.kwargs["enabled_toolsets"] == ["file", "web"]
    assert result.toolsets == ("file", "web")
    assert result.as_dict()["toolsets"] == ["file", "web"]


# -- lifecycle ----------------------------------------------------------------

def test_run_closes_the_agent_even_when_the_model_fails(loop, host):
    FakeAgent.fail_with = RuntimeError("upstream 500")

    with pytest.raises(RuntimeError):
        loop.run("q")

    (agent,) = FakeAgent.instances
    assert agent.closed is True


def test_run_async_returns_the_same_result_and_closes_the_agent(loop, host):
    result = asyncio.run(loop.run_async("q", toolsets=["file"], model="pinned-model",
                                        provider="openrouter"))

    assert result.answer == "do the simple thing"
    assert (result.model, result.provider) == ("pinned-model", "openrouter")
    (agent,) = FakeAgent.instances
    assert agent.kwargs["enabled_toolsets"] == ["file"]
    assert agent.closed is True


def test_run_async_propagates_failures(loop, host):
    FakeAgent.fail_with = RuntimeError("upstream 500")

    with pytest.raises(RuntimeError):
        asyncio.run(loop.run_async("q"))

    (agent,) = FakeAgent.instances
    assert agent.closed is True


def test_async_runs_on_a_worker_thread_not_the_event_loop(loop, host):
    """The whole point of the async entry point: a blocking loop must not stall the loop."""
    seen: dict[str, str] = {}
    original = FakeAgent.run_conversation

    def record_thread(self, prompt, **kwargs):
        seen["worker"] = threading.current_thread().name
        return original(self, prompt, **kwargs)

    FakeAgent.run_conversation = record_thread
    try:
        asyncio.run(loop.run_async("q"))
    finally:
        FakeAgent.run_conversation = original

    assert seen["worker"] != threading.main_thread().name
