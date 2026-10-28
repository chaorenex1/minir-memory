"""``/adviser``: the mode switch, its per-turn directive, and its wiring into ``register()``.

The command turn itself runs no model call: the host prints the handler's return value. What the
mode has to prove is the *next* turn, so the tests check the ``pre_llm_call`` injection — the only
channel that reaches the model.
"""

from __future__ import annotations

import types

import pytest


@pytest.fixture()
def switch(plugin_package):
    """A private registry: the tests must not depend on, or leak into, the process singleton."""
    modes = plugin_package.adviser_command.ModeSwitch()
    yield modes
    modes.clear_all()


class FakeCtx:
    """The plugin-context surface ``register()`` uses, with the commands recorded."""

    def __init__(self):
        self.commands: dict[str, types.SimpleNamespace] = {}
        self.hooks: dict[str, object] = {}

    def register_memory_provider(self, provider):
        self.provider = provider

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, **kwargs):
        pass

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = types.SimpleNamespace(handler=handler, **kwargs)


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


# -- the wiring ---------------------------------------------------------------


def test_register_exposes_the_command(plugin_package):
    ctx = FakeCtx()

    plugin_package.register(ctx)

    command = ctx.commands["adviser"]
    assert "multagents" in command.args_hint
    assert command.handler("multagents", session_id="s9").startswith("adviser: multagents armed")
    assert ctx.provider._modes.mode("s9") == "multagents"


def test_register_binds_the_handler_to_the_command_context(plugin_package):
    """``invoke_plugin_command`` passes only the keywords a handler declares; both must land."""
    ctx = FakeCtx()
    plugin_package.register(ctx)
    command = ctx.commands["adviser"]

    command.handler("multagents", session_key="gateway-session")

    assert ctx.provider._modes.mode("gateway-session") == "multagents"


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
