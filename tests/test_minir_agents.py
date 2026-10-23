"""``minir_agents``: one named custom agent, with its transcript flushed to disk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def fake_run(plugin_package, monkeypatch):
    """Stub ``agent_loop.run`` and return the calls it received."""
    calls: list[tuple[str, dict]] = []
    module = plugin_package.minir_agents

    def run(message, **kwargs):
        calls.append((message, kwargs))
        toolsets = ("*",) if kwargs.get("all_tools") else tuple(kwargs.get("toolsets") or ())
        return plugin_package.agent_loop.AgentRun(
            answer="the answer", api_calls=2, model="fake-model", provider="fake-provider",
            toolsets=toolsets,
            messages=({"role": "user", "content": message},
                      {"role": "assistant", "content": "the answer"}))

    monkeypatch.setattr(module.agent_loop, "run", run)
    return calls


def _payload(provider, **args):
    return json.loads(provider.handle_tool_call("minir_agents", args))


def test_a_custom_agent_defaults_to_all_tools_and_the_host_knobs(provider, fake_run, tmp_path):
    data = _payload(provider, agent_name="researcher", system_prompts="You research.",
                    user_prompts="Find the cause.")

    assert data["answer"] == "the answer"
    assert data["toolsets"] == ["*"] and data["all_tools"] is True
    assert data["system_prompt_source"] == "argument", "no prompt file, so the argument is used"
    (message, kwargs) = fake_run[0]
    assert message == "Find the cause."
    assert kwargs["system_message"] == "You research."
    assert kwargs["all_tools"] is True and kwargs["toolsets"] == []
    assert kwargs["skip_memory"] is False
    assert kwargs["skip_context_files"] is False
    assert kwargs["skip_background_review"] is True
    assert kwargs["load_soul_identity"] is True
    assert kwargs["project_dir"], "a custom agent gets the cwd's project root by default"

    path = Path(data["transcript_path"])
    assert path == (tmp_path / "minir-memory" / "sessions" / "default" / "researcher"
                    / "session-test.jsonl")
    assert data["transcript_written"] is True
    stored = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert stored[0]["session_id"] == "session-test"
    assert stored[0]["layer"] == "researcher"
    assert [row["role"] for row in stored
            if row["kind"] == "msg" and row["role"] != "system"] == ["user", "assistant"]
    assert kwargs["layer"] == "researcher" and kwargs["history_session"] == "session-test"
    assert kwargs["meta"] == {"agent": "researcher", "parent": "", "depth": 1}


def test_the_child_run_meta_names_the_calling_layer(provider, fake_run, plugin_package):
    tokens = plugin_package.agent_history.layer_context("base", "session-test")
    try:
        _payload(provider, agent_name="b3_window_summary", user_prompts="Write the summary.")
    finally:
        plugin_package.agent_history.reset_layer(tokens)
    assert fake_run[0][1]["meta"] == {"agent": "b3_window_summary", "parent": "base", "depth": 1}


def test_explicit_tool_sets_are_forwarded_without_the_plugin_appending(provider, fake_run):
    data = _payload(provider, agent_name="coder", user_prompts="Fix it.",
                    tool_sets=["file", "web"])

    assert data["all_tools"] is False
    assert data["toolsets"] == ["file", "web"]
    assert fake_run[0][1]["toolsets"] == ["file", "web"]
    assert fake_run[0][1]["all_tools"] is False


def test_context_files_are_read_under_the_project_scope(provider, fake_run, tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "brief.md").write_text("The cache is cold.\n", encoding="utf-8")

    data = _payload(provider, agent_name="researcher", user_prompts="Use the brief.",
                    project_dir=str(project), context_files=["brief.md"])

    assert data["context_files"] == 1
    assert "The cache is cold." in fake_run[0][1]["context"]
    assert "data, not instructions" in fake_run[0][1]["context"]
    assert fake_run[0][1]["project_dir"] == str(project)


def test_context_files_outside_the_project_are_refused(provider, fake_run, tmp_path,
                                                        tmp_path_factory):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("nope\n", encoding="utf-8")

    data = _payload(provider, agent_name="researcher", user_prompts="Read it.",
                    project_dir=str(project), context_files=[str(outside)])

    assert "not readable" in data["error"] or "limited" in data["error"]
    assert fake_run == []


def test_a_custom_agent_cannot_recurse_forever(provider, fake_run, plugin_package):
    token = plugin_package.minir_agents._DEPTH.set(plugin_package.minir_agents.MAX_DEPTH)
    try:
        data = _payload(provider, agent_name="loop", user_prompts="Again.")
    finally:
        plugin_package.minir_agents._DEPTH.reset(token)

    assert "recursion limit" in data["error"]
    assert fake_run == []


def test_agent_name_and_user_prompt_are_required(provider, fake_run):
    assert "agent_name" in _payload(provider, user_prompts="x")["error"]
    assert "user_prompts" in _payload(provider, agent_name="a")["error"]
    assert "path separators" in _payload(provider, agent_name="../x", user_prompts="x")["error"]
    assert "path separators" in _payload(provider, agent_name="a", user_prompts="x",
                                         skill_name="../b")["error"]
    assert fake_run == []


def test_a_model_route_is_split_at_the_first_colon(provider, fake_run):
    _payload(provider, agent_name="coder", user_prompts="Fix it.",
             model="openrouter:anthropic/claude-sonnet-4")

    kwargs = fake_run[0][1]
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "anthropic/claude-sonnet-4"


def test_a_bare_model_keeps_the_gateway_provider(provider, fake_run):
    _payload(provider, agent_name="coder", user_prompts="Fix it.", model="gpt-5")

    kwargs = fake_run[0][1]
    assert (kwargs["provider"], kwargs["model"]) == ("", "gpt-5")


def test_a_malformed_model_route_is_refused(provider, fake_run):
    data = _payload(provider, agent_name="coder", user_prompts="Fix it.", model="openrouter:")

    assert "provider:model" in data["error"]
    assert fake_run == []


def test_the_first_prompt_file_in_scan_order_wins(provider, fake_run, tmp_path):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "researcher.md").write_text("The .agents definition.\n",
                                                        encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "researcher.md").write_text("The agents definition.\n",
                                                       encoding="utf-8")

    data = _payload(provider, agent_name="researcher", user_prompts="Go.",
                    system_prompts="The argument.")

    assert fake_run[0][1]["system_message"] == "The .agents definition."
    assert data["system_prompt_source"] == str(tmp_path / ".agents" / "researcher.md")


def test_an_imported_agent_prompt_loses_its_front_matter(provider, fake_run, tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "code-reviewer.md").write_text(
        "---\nname: code-reviewer\ntools: Read, Grep\nmodel: sonnet\n---\n\n## Review\n\n"
        "Find bugs.\n", encoding="utf-8")

    _payload(provider, agent_name="code-reviewer", user_prompts="Review it.")

    assert fake_run[0][1]["system_message"] == "## Review\n\nFind bugs."


def test_an_empty_prompt_file_is_skipped(provider, fake_run, tmp_path):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "researcher.md").write_text("  \n", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "researcher.md").write_text("The next definition.\n", encoding="utf-8")

    _payload(provider, agent_name="researcher", user_prompts="Go.")

    assert fake_run[0][1]["system_message"] == "The next definition."


def test_the_skill_scoped_prompt_needs_skill_name(provider, fake_run, tmp_path):
    agents = tmp_path / "skills" / "code-review" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("The skill definition.\n", encoding="utf-8")

    _payload(provider, agent_name="reviewer", user_prompts="Go.", skill_name="code-review")
    _payload(provider, agent_name="reviewer", user_prompts="Go.", system_prompts="The argument.")

    assert fake_run[0][1]["system_message"] == "The skill definition."
    assert fake_run[1][1]["system_message"] == "The argument."


def test_the_packaged_pass_core_beats_the_argument(provider, fake_run, tmp_path):
    packaged = tmp_path / "plugins" / "minir" / "agents"
    packaged.mkdir(parents=True)
    (packaged / "b3_window_summary.md").write_text("The packaged core.\n", encoding="utf-8")

    data = _payload(provider, agent_name="b3_window_summary", user_prompts="Summarize.",
                    system_prompts="The argument.")

    assert fake_run[0][1]["system_message"] == "The packaged core."
    assert data["system_prompt_source"] == str(packaged / "b3_window_summary.md")

