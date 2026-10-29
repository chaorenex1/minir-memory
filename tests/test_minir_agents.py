"""``minir_agents``: one named custom agent, with its transcript flushed to disk."""

from __future__ import annotations

import json
import queue as queue_module
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


def _call(provider, **args):
    return json.loads(provider.handle_tool_call("minir_agents", args))


def _payload(provider, **args):
    """A blocking call: the default for this file, so a test asserts what it means to assert."""
    args.setdefault("background", False)
    return _call(provider, **args)


@pytest.fixture(autouse=True)
def _clean_jobs(plugin_package):
    """The job table is process-wide; every test starts with no runs and no waiting line."""
    module = plugin_package.minir_agents

    def _clear():
        with module._LOCK:
            module._ACTIVE.clear()
            module._PER_SESSION.clear()
            module._PENDING.clear()

    _clear()
    yield
    _clear()


@pytest.fixture()
def queue(plugin_package, monkeypatch):
    """Where a finished job hands its event; the host's completion queue is replaced."""
    events: queue_module.Queue = queue_module.Queue()
    monkeypatch.setattr(plugin_package.minir_agents, "_notification_queue", lambda: events)
    return events


@pytest.fixture()
def slow_run(plugin_package, monkeypatch):
    """``agent_loop.run`` that blocks until the test releases it, so slots stay occupied."""
    module = plugin_package.minir_agents
    state = SimpleNamespace(calls=[], observed=[], started=threading.Semaphore(0),
                            release=threading.Event())

    def run(message, **kwargs):
        state.calls.append((message, kwargs))
        state.observed.append((kwargs.get("meta", {}).get("agent"), module._DEPTH.get()))
        state.started.release()
        state.release.wait(10)
        toolsets = ("*",) if kwargs.get("all_tools") else tuple(kwargs.get("toolsets") or ())
        return plugin_package.agent_loop.AgentRun(
            answer=f"answer {len(state.calls)}", api_calls=1, model="fake-model",
            provider="fake-provider", toolsets=toolsets,
            messages=({"role": "user", "content": message},
                      {"role": "assistant", "content": f"answer {len(state.calls)}"}))

    monkeypatch.setattr(module.agent_loop, "run", run)
    yield state
    state.release.set()


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


def test_the_child_run_is_bound_to_its_own_session_id(provider, fake_run):
    """A dispatched run carries the id it runs under, not the caller's.

    That id is what the child's system prompt names and what its own state (``minir_todo``) keys
    on, while ``history_session`` still selects the caller's transcript stream.
    """
    _payload(provider, agent_name="researcher", user_prompts="Find the cause.")

    kwargs = fake_run[0][1]
    assert kwargs["history_session"] == "session-test"
    assert kwargs["session_id"].startswith("session-test:minir:researcher:")
    assert kwargs["session_id"] != kwargs["history_session"]


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


def test_a_prompt_file_is_rendered_once_after_loading(provider, fake_run, tmp_path,
                                                      plugin_package):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "researcher.md").write_text(
        "Keep `topic` within $MAX_UNIT_TOPIC characters.\n"
        "Read the files under $HERMES_HOME yourself.\n", encoding="utf-8")

    _payload(provider, agent_name="researcher", user_prompts="Go.")

    system = fake_run[0][1]["system_message"]
    cap = plugin_package.layers.prompt_vars()["MAX_UNIT_TOPIC"]
    assert f"within {cap} characters" in system, "the cap the validator enforces must reach the model"
    assert "$MAX_UNIT_TOPIC" not in system
    assert "$HERMES_HOME" in system, "a variable this code does not own is left for its reader"


def test_a_packaged_pass_core_is_not_observable(plugin_package):
    """The b3/b4 cores ship in ``plugins/minir/agents`` and are never handed to the adviser."""
    observable = plugin_package.minir_agents._observable_prompt

    assert observable(
        r"C:\Users\x\AppData\Local\hermes\plugins\minir\agents\b3_window_summary.md") is False
    assert observable("plugins/minir/agents/b4_value_markers.md") is False
    assert observable(r"C:\Users\x\AppData\Local\hermes\.agents\mine.md") is True
    assert observable("skills/note-taking/agents/writer.md") is True
    assert observable("argument") is True, "an inline prompt is the caller's own agent"
    assert observable("") is True


def test_a_packaged_pass_core_registers_unobservable(plugin_package, provider, fake_run,
                                                    tmp_path, monkeypatch):
    """The b3/b4 cores under plugins/minir/agents never reach the adviser; a caller's own does."""
    module = plugin_package.minir_agents
    core = tmp_path / "plugins" / "minir" / "agents"
    core.mkdir(parents=True)
    (core / "b3_window_summary.md").write_text("Summarize the window.\n", encoding="utf-8")
    mine = tmp_path / ".agents"
    mine.mkdir()
    (mine / "researcher.md").write_text("You research.\n", encoding="utf-8")

    seen: list = []
    original = module.agent_watch.register

    def spy(**kwargs):
        seen.append(kwargs.get("observe"))
        return original(**kwargs)

    monkeypatch.setattr(module.agent_watch, "register", spy)

    _payload(provider, agent_name="b3_window_summary", user_prompts="Summarize.")
    _payload(provider, agent_name="researcher", user_prompts="Find it.")

    assert seen == [False, True], "a packaged pass core is not observable, a caller's agent is"


def test_every_packaged_prompt_renders_without_a_leftover_cap(plugin_package):
    """A packaged core states its caps as $NAME; a typo would ship a literal to the model."""
    agents = Path(plugin_package.layers.AGENTS_DIR)
    leftover = {}
    for path in sorted(agents.glob("*.md")):
        rendered = plugin_package.layers.render_prompt(path.read_text(encoding="utf-8-sig"))
        found = sorted(set(re.findall(r"\$[A-Z][A-Z0-9_]{2,}", rendered)))
        if found:
            leftover[path.name] = found

    assert leftover == {}, "a packaged prompt leaves a placeholder unrendered"


def test_the_default_run_returns_a_handle_and_notifies_later(provider, slow_run, queue):
    data = _call(provider, agent_name="researcher", user_prompts="Find the cause.")

    assert data["background"] is True
    assert (data["status"], data["queue_position"]) == ("running", 0)
    assert data["job_id"].startswith("minir-researcher-")
    assert "answer" not in data, "the call must not wait for the run"
    assert Path(data["transcript_path"]).parts[-2:] == ("researcher", "session-test.jsonl")

    assert slow_run.started.acquire(timeout=5), "the job should start immediately"
    slow_run.release.set()
    event = queue.get(timeout=10)

    assert event["type"] == "async_delegation"
    assert event["session_key"] == "session-test"
    assert (event["delegation_id"], event["session_id"]) == (data["job_id"], data["job_id"])
    assert event["status"] == "completed"
    assert event["summary"] == "answer 1"
    assert event["goal"] == "Find the cause." and event["goals"] == ["Find the cause."]
    assert (event["role"], event["api_calls"], event["model"]) == ("plugin", 1, "fake-model")
    assert event["duration_seconds"] >= 0
    assert event["completed_at"] >= event["dispatched_at"]
    assert "results" not in event and "restored" not in event
    assert Path(data["transcript_path"]).exists(), "the transcript is flushed before the event"


def test_the_event_satisfies_the_claim_gate_shape(provider, slow_run, queue, plugin_package):
    module = plugin_package.minir_agents
    _call(provider, agent_name="researcher", user_prompts="x" * 500)
    assert slow_run.started.acquire(timeout=5)
    slow_run.release.set()
    event = queue.get(timeout=10)

    assert event["delegation_id"].startswith("minir-researcher-")
    assert event["session_key"], "an event without a session key is owned by nobody"
    assert "restored" not in event, "the host treats a restored event as fail-closed"
    assert len(event["goal"]) <= module.MAX_GOAL_CHARS and event["goal"].endswith("…")


def test_a_failing_run_notifies_an_error(provider, queue, plugin_package, monkeypatch):
    def boom(message, **kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(plugin_package.minir_agents.agent_loop, "run", boom)
    data = _call(provider, agent_name="researcher", user_prompts="Go.")
    event = queue.get(timeout=10)

    assert data["status"] == "running"
    assert event["status"] == "error"
    assert "model exploded" in event["error"]
    assert event["summary"] == event["error"]
    assert event["api_calls"] == 0


def test_background_false_keeps_the_blocking_payload(provider, fake_run):
    data = _payload(provider, agent_name="researcher", user_prompts="Go.")

    assert set(data) == {"agent_name", "session_id", "answer", "api_calls", "model", "provider",
                         "toolsets", "all_tools", "system_prompt_source", "context_files",
                         "transcript_path", "transcript_written", "child_session_id"}
    assert data["answer"] == "the answer"
    assert "job_id" not in data
    assert data["child_session_id"].startswith("session-test:minir:researcher:minir-researcher-")


def test_a_run_registers_its_session_while_it_runs(provider, plugin_package, monkeypatch):
    seen: dict = {}

    def run(message, **kwargs):
        session = kwargs["session_id"]
        watch = plugin_package.agent_watch.get(session)
        seen.update(session=session, watched=watch is not None,
                    capabilities=dict(watch.capabilities) if watch is not None else {})
        return plugin_package.agent_loop.AgentRun(
            answer="done", api_calls=1, model="m", provider="p",
            toolsets=tuple(kwargs.get("toolsets") or ()),
            messages=({"role": "user", "content": message},))

    monkeypatch.setattr(plugin_package.minir_agents.agent_loop, "run", run)
    data = _payload(provider, agent_name="researcher", user_prompts="Go.")

    assert seen["watched"] is True
    assert seen["session"] == data["child_session_id"]
    assert seen["capabilities"]["file"] is True, "no tool_sets means every tool, file included"
    assert seen["capabilities"]["toolsets"] == []
    assert plugin_package.agent_watch.is_child(seen["session"]) is False, (
        "the run is unregistered once it returns")


def test_the_final_stream_carries_the_run_id_and_leaves_the_host_stream_alone(provider, fake_run):
    data = _payload(provider, agent_name="researcher", user_prompts="Go.")
    job = data["child_session_id"].rsplit(":", 1)[-1]

    final = provider._transcript.read_events("default", "researcher", "session-test")["rows"]
    assert {row["run_id"] for row in final if row.get("run_id")} == {job}
    assert [row["role"] for row in final
            if row["run_id"] == job and row.get("kind") == "msg"] == ["user", "assistant"]
    host = provider._transcript.read_events("default", "host", "session-test")["rows"]
    assert not [row for row in host if row.get("run_id") == job], (
        "a child's audit rows never enter the host stream")
    assert not [row for row in host if row.get("session_id") == data["child_session_id"]]


def test_a_full_session_queues_instead_of_failing(provider, slow_run, queue):
    handles = [_call(provider, agent_name=f"a{index}", user_prompts="Go.") for index in range(3)]
    assert [handle["status"] for handle in handles] == ["running"] * 3
    assert all(slow_run.started.acquire(timeout=5) for _ in range(3))

    fourth = _call(provider, agent_name="fourth", user_prompts="Go.")
    assert (fourth["status"], fourth["queue_position"]) == ("queued", 1)
    fifth = _call(provider, agent_name="fifth", user_prompts="Go.")
    assert fifth["queue_position"] == 2, "the position is the place in the waiting line"
    assert slow_run.started.acquire(timeout=2) is False, "a queued job must not start yet"

    slow_run.release.set()
    ids = {queue.get(timeout=10)["delegation_id"] for _ in range(5)}

    assert ids == {handle["job_id"] for handle in handles} | {fourth["job_id"], fifth["job_id"]}


def test_the_waiting_line_is_bounded(provider, slow_run, plugin_package, monkeypatch):
    module = plugin_package.minir_agents
    monkeypatch.setattr(module, "MAX_QUEUED_PER_SESSION", 1)
    for index in range(3):
        _call(provider, agent_name=f"a{index}", user_prompts="Go.")
    assert all(slow_run.started.acquire(timeout=5) for _ in range(3))

    assert _call(provider, agent_name="waiting", user_prompts="Go.")["status"] == "queued"
    assert "queue is full" in _call(provider, agent_name="overflow", user_prompts="Go.")["error"]


def test_a_saturated_session_does_not_block_another(make_provider, slow_run):
    first = make_provider({"prefetch_limit": 5})
    second = make_provider({"prefetch_limit": 5}, session_id="session-other")
    for index in range(3):
        _call(first, agent_name=f"a{index}", user_prompts="Go.")
    assert all(slow_run.started.acquire(timeout=5) for _ in range(3))

    assert _call(second, agent_name="b", user_prompts="Go.")["status"] == "running"
    assert slow_run.started.acquire(timeout=5), "the other session's job starts at once"


def test_a_queued_job_keeps_the_submitters_context(provider, slow_run, plugin_package):
    module = plugin_package.minir_agents
    for index in range(3):
        _call(provider, agent_name=f"a{index}", user_prompts="Go.")
    assert all(slow_run.started.acquire(timeout=5) for _ in range(3))

    token = module._DEPTH.set(2)
    try:
        queued = _call(provider, agent_name="deep", user_prompts="Go.")
    finally:
        module._DEPTH.reset(token)
    assert queued["status"] == "queued"

    slow_run.release.set()
    deadline = time.monotonic() + 10
    while len(slow_run.observed) < 4 and time.monotonic() < deadline:
        time.sleep(0.02)

    assert ("deep", 3) in slow_run.observed, "a queued job keeps the depth it was queued at"
    assert ("a0", 1) in slow_run.observed


def test_a_long_answer_is_truncated_and_points_at_the_transcript(provider, queue,
                                                               plugin_package, monkeypatch):
    module = plugin_package.minir_agents
    long_answer = "x" * 5000

    def run(message, **kwargs):
        return plugin_package.agent_loop.AgentRun(
            answer=long_answer, api_calls=1, model="fake-model", provider="fake-provider",
            toolsets=("*",), messages=())

    monkeypatch.setattr(module.agent_loop, "run", run)
    data = _call(provider, agent_name="researcher", user_prompts="Go.")
    event = queue.get(timeout=10)

    assert event["summary"].startswith("x" * module.MAX_NOTIFICATION_CHARS)
    assert "5000 characters total" in event["summary"]
    assert data["transcript_path"] in event["summary"]


def test_a_run_without_a_transcript_still_notifies(provider, queue, plugin_package, monkeypatch):
    def run(message, **kwargs):
        return plugin_package.agent_loop.AgentRun(
            answer="short answer", api_calls=1, model="fake-model", provider="fake-provider",
            toolsets=("*",), messages=())

    monkeypatch.setattr(plugin_package.minir_agents.agent_loop, "run", run)
    transcript = provider._transcript
    provider._transcript = None
    try:  # the provider's shutdown closes its buffer, so put it back inside the test
        data = _call(provider, agent_name="researcher", user_prompts="Go.")
        event = queue.get(timeout=10)
    finally:
        provider._transcript = transcript

    assert data["transcript_path"] == ""
    assert event["summary"] == "short answer"


def test_a_boolean_flag_holding_prose_is_refused(provider, fake_run):
    data = _call(provider, agent_name="researcher", user_prompts="Go.",
                 background="<prior report, verbatim>")

    assert "background must be a boolean" in data["error"]
    assert fake_run == [], "a rejected call must not run anything"


def test_a_string_flag_still_reads_as_a_boolean(provider, fake_run):
    data = _call(provider, agent_name="researcher", user_prompts="Go.", background="false")

    assert data["answer"] == "the answer"


def test_a_failed_thread_start_releases_the_slot(provider, plugin_package, monkeypatch):
    module = plugin_package.minir_agents

    class Unstartable:
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(module, "spawn_context_thread", lambda body, name="": Unstartable())
    data = _call(provider, agent_name="researcher", user_prompts="Go.")

    assert "could not start a worker thread" in data["error"]
    with module._LOCK:
        assert module._ACTIVE == {} and module._PER_SESSION == {}, "a start that failed leaks nothing"


def test_a_stalled_run_frees_its_slot_and_is_not_reported_twice(provider, slow_run, queue,
                                                                plugin_package):
    module = plugin_package.minir_agents
    first = _call(provider, agent_name="stuck", user_prompts="Go.")
    assert slow_run.started.acquire(timeout=5)
    with module._LOCK:  # age the job past the cap instead of waiting thirty minutes
        for entry in module._ACTIVE.values():
            entry["started_at"] -= module.MAX_RUN_SECONDS + 1

    assert module._stall_scan() == [first["job_id"]]
    event = queue.get(timeout=10)
    assert (event["delegation_id"], event["status"]) == (first["job_id"], "stalled")
    assert "slot was released" in event["error"]
    with module._LOCK:
        assert module._ACTIVE == {} and module._PER_SESSION == {}

    second = _call(provider, agent_name="next", user_prompts="Go.")
    assert second["status"] == "running", "the freed slot must be usable again"
    assert slow_run.started.acquire(timeout=5)
    slow_run.release.set()

    assert queue.get(timeout=10)["delegation_id"] == second["job_id"]
    with pytest.raises(queue_module.Empty):
        # The stalled thread is still running; when it finishes it must not notify a second time.
        queue.get(timeout=2)


def test_the_schema_offers_background(plugin_package):
    module = plugin_package.minir_agents
    schema = next(item for item in plugin_package.tools.SCHEMAS if item["name"] == "minir_agents")
    properties = schema["parameters"]["properties"]

    assert properties["background"]["type"] == "boolean"
    assert "background" not in schema["parameters"]["required"]
    assert "job handle" in schema["description"]
    # The caps in the prose come from the constants, so bumping one cannot leave the schema lying.
    assert f"{module.MAX_BACKGROUND_PER_SESSION} runs per session" in schema["description"]
    assert f"{int(module.MAX_RUN_SECONDS)}s" in schema["description"]

