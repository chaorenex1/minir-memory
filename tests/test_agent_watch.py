"""``agent_watch``: the table of background runs the adviser may observe."""

from __future__ import annotations


def test_the_child_id_is_stable_and_names_the_run(plugin_package):
    module = plugin_package.agent_watch
    first = module.child_session_id("session-test", "researcher", "job-1")
    second = module.child_session_id("session-test", "researcher", "job-1")

    assert first == second == "session-test:minir:researcher:job-1"
    assert module.child_session_id("", "", "") == "session:minir:agent:job"


def test_a_registered_run_is_addressable_by_its_own_session(plugin_package):
    module = plugin_package.agent_watch
    watch = module.register(parent="session-test", agent="researcher", job_id="job-1",
                            capabilities={"soul": False}, profile="default")

    assert watch is not None
    assert module.is_child(watch.session) and module.get(watch.session) is watch
    assert watch.parent == "session-test" and watch.agent == "researcher"
    assert watch.capabilities == {"soul": False}, "capabilities come back unchanged"
    assert watch.profile == "default"
    assert module.is_child("session-test") is False, "the parent is not a watched run"


def test_a_run_without_a_job_id_or_past_the_depth_limit_is_not_watched(plugin_package):
    module = plugin_package.agent_watch

    assert module.register(parent="session-test", agent="a", job_id="") is None
    assert module.register(parent="session-test", agent="a", job_id="j", depth=2) is None
    assert module.register(parent="session-test", agent="a", job_id="j", depth=1) is not None


def test_unregistering_drops_the_run(plugin_package):
    module = plugin_package.agent_watch
    watch = module.register(parent="session-test", agent="a", job_id="j")

    module.unregister(watch.session)

    assert module.get(watch.session) is None and module.is_child(watch.session) is False


def test_steps_are_bounded_and_advice_keeps_its_order(plugin_package):
    module = plugin_package.agent_watch
    watch = module.register(parent="session-test", agent="a", job_id="j")

    for index in range(module.PROGRESS_STEPS + 4):
        module.note_step(watch.session, f"step {index}")
    for text in ("first", "second"):
        module.note_advice(watch.session, text)

    assert len(watch.steps) == module.PROGRESS_STEPS
    assert watch.steps[0] == "step 4" and watch.steps[-1] == f"step {module.PROGRESS_STEPS + 3}"
    assert watch.advice == ["first", "second"]


def test_lines_inside_the_window_merge_and_an_outside_line_flushes(plugin_package):
    module = plugin_package.agent_watch
    watch = module.register(parent="session-test", agent="a", job_id="j")

    first = module.push_line(watch.session, "one", now=100.0)
    merged = module.push_line(watch.session, "two", now=100.5)
    also_merged = module.push_line(watch.session, "three", now=101.0)
    flushed = module.push_line(watch.session, "four", now=100.0 + module.PROGRESS_MIN_GAP + 1)

    assert first == "one", "the first line of an idle window goes out at once"
    assert (merged, also_merged) == ("", ""), "inside the window the lines wait"
    assert flushed == "two; three; four", "the window hands back everything it held, in order"


def test_the_brief_names_the_run_and_is_empty_for_the_primary_session(plugin_package):
    module = plugin_package.agent_watch
    watch = module.register(parent="session-test", agent="researcher", job_id="job-1")

    brief = module.child_brief(watch.session)

    assert "background agent run (researcher, job job-1)" in brief
    assert brief.startswith("[background agent review]")
    assert module.child_brief("session-test") == ""
