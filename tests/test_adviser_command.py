"""``/adviser``: the mode switch and the per-turn directive it drives.

The command surface lives in the sibling ``minir-adviser`` plugin: a provider-config plugin is
refused by discovery, so a command registered from this directory does not exist before the first
turn, and ``tests/test_minir_adviser_command.py`` covers registration, forwarding and the
ready-check delivery. What stays here is the state machine and the ``pre_llm_call`` directive —
the channel that carries the mode into the model.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def switch(plugin_package):
    """A private registry: the tests must not depend on, or leak into, the process singleton."""
    modes = plugin_package.adviser_command.ModeSwitch()
    yield modes
    modes.clear_all()


# -- the switch ---------------------------------------------------------------


def test_multagents_arms_the_session(plugin_package, switch):
    line = switch.handle("multagents", session_id="s1")

    assert switch.mode("s1") == plugin_package.adviser_command.MODE_MULTIAGENTS
    assert "armed" in line
    assert "hermes-lead" in line
    assert "/adviser dispatch" in line


def test_the_next_turn_carries_the_directive(plugin_package, switch):
    switch.handle("multagents", session_id="s1")

    directive = switch.directive("s1")

    assert directive.startswith("[adviser mode: multagents]")
    # The load path, the loop, the tool and the superseded contract all have to be named: the
    # model sees this block once per turn and nothing else explains the switch.
    assert 'skill_view(name="minir:hermes-lead")' in directive
    assert "minir_agents" in directive
    assert "hermes-dispatch" in directive
    assert "/adviser dispatch" in directive


def test_dispatch_releases_the_session(plugin_package, switch):
    switch.handle("multagents", session_id="s1")

    line = switch.handle("dispatch", session_id="s1")

    assert switch.mode("s1") == plugin_package.adviser_command.MODE_DISPATCH
    assert switch.directive("s1") is None
    assert "dispatch" in line


def test_an_unknown_mode_changes_nothing(plugin_package, switch):
    switch.handle("multagents", session_id="s1")

    line = switch.handle("multiagent", session_id="s1")

    assert switch.mode("s1") == plugin_package.adviser_command.MODE_MULTIAGENTS
    assert "unknown mode" in line
    assert "usage:" in line


def test_status_reports_the_mode_without_changing_it(plugin_package, switch):
    assert "mode=dispatch" in switch.handle("status", session_id="s1")
    assert "mode=status" not in switch.handle("status", session_id="s1")
    switch.handle("multagents", session_id="s1")
    assert "mode=multagents" in switch.handle("status", session_id="s1")


def test_a_mode_stays_inside_its_session(plugin_package, switch):
    switch.handle("multagents", session_id="s1")

    assert switch.directive("s2") is None


def test_an_empty_argument_is_status(plugin_package, switch):
    assert "mode=dispatch" in switch.handle("", session_id="s1")


# -- the ready-check turn -----------------------------------------------------


def test_arming_queues_the_ready_check_turn(plugin_package, switch):
    queued = []

    line = switch.handle("multagents", session_id="s1", deliver=lambda text: queued.append(text) or True)

    assert queued == [plugin_package.adviser_command.KICKOFF_TEXT]
    assert "ready-check" in line
    assert switch.mode("s1") == plugin_package.adviser_command.MODE_MULTIAGENTS


def test_a_refused_inject_leaves_the_mode_armed(plugin_package, switch):
    line = switch.handle("multagents", session_id="s1", deliver=lambda _text: False)

    assert "ready-check" not in line
    assert "armed" in line
    assert switch.mode("s1") == plugin_package.adviser_command.MODE_MULTIAGENTS


def test_a_raising_inject_leaves_the_mode_armed(plugin_package, switch):
    def boom(_text):
        raise RuntimeError("no injector")

    line = switch.handle("multagents", session_id="s1", deliver=boom)

    assert "armed" in line
    assert switch.mode("s1") == plugin_package.adviser_command.MODE_MULTIAGENTS


def test_only_arming_queues_a_turn(plugin_package, switch):
    queued = []

    def deliver(text):
        queued.append(text)
        return True

    switch.handle("status", session_id="s1", deliver=deliver)
    switch.handle("multagents", session_id="s1", deliver=deliver)
    switch.handle("dispatch", session_id="s1", deliver=deliver)

    assert len(queued) == 1


def test_a_second_arm_does_not_queue_another_turn(plugin_package, switch):
    queued = []

    def deliver(text):
        queued.append(text)
        return True

    switch.handle("multagents", session_id="s1", deliver=deliver)
    line = switch.handle("multagents", session_id="s1", deliver=deliver)

    assert len(queued) == 1
    assert "already armed" in line


# -- the hook ---------------------------------------------------------------


def test_pre_llm_call_injects_the_armed_directive(plugin_package, make_provider):
    provider = make_provider()
    session = provider._session_id
    provider._modes.arm(session)

    injected = plugin_package._on_pre_llm_call(session_id=session, turn_id="t", provider=provider)

    assert injected.startswith("[adviser mode: multagents]")
    assert "hermes-lead" in injected


def test_pre_llm_call_stays_quiet_in_dispatch(plugin_package, make_provider):
    provider = make_provider()

    injected = plugin_package._on_pre_llm_call(
        session_id=provider._session_id, turn_id="t", provider=provider)

    assert not (injected or "").startswith("[adviser mode: multagents]")


def test_session_finalize_drops_the_mode(plugin_package, make_provider):
    provider = make_provider()
    session = provider._session_id
    provider._modes.arm(session)

    plugin_package._on_session_finalize(session_id=session, provider=provider)

    assert provider._modes.directive(session) is None


def test_session_reset_drops_the_old_mode(plugin_package, make_provider):
    provider = make_provider()
    provider._modes.arm("old-session")

    plugin_package._on_session_reset(
        session_id="new-session", old_session_id="old-session", provider=provider)

    assert provider._modes.directive("old-session") is None
