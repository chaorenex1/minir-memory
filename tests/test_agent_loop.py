"""``agent_loop`` — one AIAgent run, two entry points (sync + async).

The caller only passes message / context / model / provider / toolsets; the rest is resolved
internally the way the gateway resolves a message (``_resolve_model_and_runtime`` in
``plugins/platforms/feishu/feishu_comment.py``). These tests assert exactly that split:
which overrides reach the agent, and which values come from the host resolver.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import sys
import threading
import types

import pytest


class FakeAgent:
    """Records how the loop constructs its AIAgent and whether it closed it."""

    instances: list["FakeAgent"] = []
    fail_with: Exception | None = None
    result_messages: tuple = ()

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.prompt = ""
        self.system_message = "<unset>"
        self.closed = False
        # Enough shape for the read-only filter; the loop's tests do not run a tool.
        self.tools = [{"function": {"name": "read_file"}},
                      {"function": {"name": "write_file"}},
                      {"function": {"name": "patch"}},
                      {"function": {"name": "minir_remember"}},
                      {"function": {"name": "minir_todo"}}]
        self.valid_tool_names = {"read_file", "write_file", "patch", "minir_remember",
                                 "minir_todo"}
        FakeAgent.instances.append(self)

    def run_conversation(self, prompt, system_message=None, **_kwargs):
        self.prompt = prompt
        self.system_message = system_message
        if FakeAgent.fail_with is not None:
            raise FakeAgent.fail_with
        return {"final_response": "  do the simple thing\n", "api_calls": 3,
                "messages": list(FakeAgent.result_messages),
                "total_tokens": 42, "estimated_cost_usd": 0.001}

    def close(self):
        self.closed = True


@pytest.fixture()
def host(monkeypatch):
    """Stand-ins for every host module the loop resolves through."""
    FakeAgent.instances = []
    FakeAgent.fail_with = None
    FakeAgent.result_messages = ()

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
    assert agent.kwargs["load_soul_identity"] is False
    assert agent.kwargs["max_iterations"] == loop.MAX_ITERATIONS
    assert agent.kwargs["enabled_toolsets"] == [], "no toolset means no tools, never every tool"
    assert agent.closed is True


def test_system_message_is_forwarded_to_the_turn(loop, host):
    loop.run("q", system_message="  be blunt  ")

    (agent,) = FakeAgent.instances
    assert agent.system_message.startswith("be blunt\n\n[run-meta]")
    assert "route_source: gateway" in agent.system_message
    assert "model: gateway-model" in agent.system_message


def test_system_segments_join_and_meta_records_the_explicit_route(loop, host):
    loop.run("q", system_message=["rule one", " ", "rule two"], layer="base",
             provider="custom:siliconflow", model="Qwen/Qwen3.5-4B",
             meta={"agent": "b1", "parent": "top", "depth": 0})

    (agent,) = FakeAgent.instances
    assert agent.system_message.startswith("rule one\n\nrule two\n\n[run-meta]")
    assert "provider: openrouter" in agent.system_message  # the pinned route, not the request
    assert "model: Qwen/Qwen3.5-4B" in agent.system_message
    assert "route_source: explicit" in agent.system_message
    assert "pinned-key" not in agent.system_message and "api_key" not in agent.system_message


def test_no_system_message_means_the_host_default(loop, host):
    loop.run("q")

    (agent,) = FakeAgent.instances
    assert agent.system_message is None, "metadata alone never creates a system prompt"


def test_the_run_id_is_bound_beside_the_layer(loop, host, plugin_package, monkeypatch):
    """A tool that keeps state per run reads the run's own id, not the stream session.

    The provider serving a child's tool call is bound to the session that dispatched the run, so
    ``minir_todo`` needs an identity the run itself owns.
    """
    seen = {}
    original = FakeAgent.run_conversation

    def spy(self, prompt, system_message=None, **kwargs):
        seen["layer"] = plugin_package.agent_history.current_layer()
        seen["session"] = plugin_package.agent_history.current_session()
        seen["run"] = plugin_package.agent_history.current_run()
        return original(self, prompt, system_message=system_message, **kwargs)

    monkeypatch.setattr(FakeAgent, "run_conversation", spy)
    loop.run("q", system_message="brief", layer="explorer", history_session="host-session",
             session_id="host-session:minir:explorer:job-1")

    assert seen == {"layer": "explorer", "session": "host-session",
                    "run": "host-session:minir:explorer:job-1"}
    (agent,) = FakeAgent.instances
    assert "session: host-session:minir:explorer:job-1" in agent.system_message
    assert plugin_package.agent_history.current_run() == "", "the binding is reset afterwards"


def test_only_the_intended_constructor_kwargs_are_passed(loop, host):
    """A peer run must not inherit session identity, callbacks, checkpoints or budgets."""
    loop.run("q")

    (agent,) = FakeAgent.instances
    assert set(agent.kwargs) == {
        "model", "provider", "reasoning_config",
        "api_key", "base_url", "api_mode", "credential_pool", "request_overrides",
        "requested_provider", "capabilities", "command", "args", "session_id",
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


def test_a_custom_agent_can_opt_into_every_tool_and_the_host_knobs(loop, host):
    run = loop.run("q", all_tools=True, skip_memory=False, skip_context_files=False,
                   skip_background_review=False, load_soul_identity=True)

    agent = FakeAgent.instances[-1]
    assert agent.kwargs["enabled_toolsets"] is None, "None is the host's every-tool surface"
    assert agent.kwargs["skip_memory"] is False
    assert agent.kwargs["skip_context_files"] is False
    assert agent.kwargs["skip_background_review"] is False
    assert agent.kwargs["load_soul_identity"] is True
    assert run.toolsets == ("*",)


def test_every_peer_run_is_scoped_for_the_file_guard(loop, host, monkeypatch):
    entered: list[str] = []

    @contextlib.contextmanager
    def scope(project_dir=""):
        entered.append(str(project_dir))
        yield

    monkeypatch.setattr(loop.file_guard, "aux_scope", scope)
    monkeypatch.setattr(loop.file_guard, "project_root", lambda: "/tmp/default-project")
    loop.run("q")
    loop.run("q", project_dir="/tmp/project")
    loop.run("q", project_dir="")

    assert entered == ["/tmp/default-project", "/tmp/project", ""], \
        "every peer run must inherit the project root unless it opts out"


# -- the transcript the audit trail needs --------------------------------------

def test_the_run_carries_the_conversation_messages(loop, host):
    """Prompt plus answer loses the tool calls; the audit trail needs the whole loop."""
    FakeAgent.result_messages = (
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "content": "ok", "tool_call_id": "call-1"},
    )

    result = loop.run("q")

    assert isinstance(result.messages, tuple)
    assert [row["role"] for row in result.messages] == ["user", "assistant", "tool"]
    assert result.messages[1]["tool_calls"] == [{"id": "call-1"}]
    # The adviser tool's payload stays small: a transcript there would flood the caller.
    assert "messages" not in result.as_dict()


def test_a_run_without_host_messages_has_an_empty_transcript(loop, host):
    assert loop.run("q").messages == ()


def test_a_top_level_reasoning_content_lands_on_the_assistant_message(loop, host):
    rows = ({"role": "user", "content": "q"}, {"role": "assistant", "content": "a"})
    converted = loop._with_reasoning(rows, "  because  ")
    assert converted[1]["reasoning_content"] == "because"
    assert converted[0] == rows[0]
    only_reasoning = loop._with_reasoning((), "because")
    assert only_reasoning == ({"role": "reasoning", "content": "because"},)
    assert loop._with_reasoning(rows, "") == rows


def test_the_run_binds_the_callers_transcript_layer(loop, host, monkeypatch):
    seen: list[str] = []
    original = FakeAgent.run_conversation

    def record(self, prompt, system_message=None, **kwargs):
        seen.append(loop.agent_history.current_layer())
        return original(self, prompt, system_message=system_message, **kwargs)

    monkeypatch.setattr(FakeAgent, "run_conversation", record)
    loop.run("q", layer="base")
    assert seen == ["base"]
    assert loop.agent_history.current_layer() == ""  # reset after the run


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


def test_read_only_drops_mutating_tools_and_usage_is_captured(loop, host):
    run = loop.run("q", read_only=True)

    (agent,) = FakeAgent.instances
    assert [tool["function"]["name"] for tool in agent.tools] == ["read_file"]
    assert agent.valid_tool_names == {"read_file"}
    # The write surfaces a reviewer must not hold: the memory tools and the todo list too.
    assert "minir_todo" in loop.READ_ONLY_DROPPED_TOOLS
    assert run.usage == {"total_tokens": 42, "estimated_cost_usd": 0.001}


def test_apply_read_only_is_idempotent_without_mutating_tools(loop):
    agent = types.SimpleNamespace(tools=[{"function": {"name": "read_file"}}],
                                  valid_tool_names={"read_file"})
    assert loop.apply_read_only(agent) == []
    assert agent.tools == [{"function": {"name": "read_file"}}]


# -- the bounded run ----------------------------------------------------------

def test_the_bounded_run_keeps_the_callers_context(loop):
    scope = contextvars.ContextVar("minir_test_scope", default="")
    seen: dict = {}

    def _run() -> str:
        seen["scope"] = scope.get()
        return "ok"

    token = scope.set("profile-a")
    try:
        assert loop.run_with_timeout(_run, 2.0) == "ok"
    finally:
        scope.reset(token)
    assert seen["scope"] == "profile-a"


def test_a_failure_in_the_bounded_run_reaches_the_caller(loop):
    def _run() -> str:
        raise ValueError("route down")

    with pytest.raises(ValueError, match="route down"):
        loop.run_with_timeout(_run, 2.0)
