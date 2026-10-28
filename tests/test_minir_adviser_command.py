"""``minir-adviser``: the discovery-time ``/adviser`` command and its forwarding to the provider.

The mode state stays on the memory provider, so these tests cover what this directory owns:
registration, forwarding (raw args, session, injector), the queued path used before a provider
exists (queue + one injected ready-check turn), and the ``pre_llm_call`` flush that applies the
queued request once the provider is up.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

COMPANION = Path(__file__).resolve().parents[1] / "plugin" / "minir-adviser"


@pytest.fixture(scope="session")
def companion():
    """Load the companion directory as its own package, the way the host loads a plugin dir."""
    name = f"minir_adviser_pkg_{id(COMPANION)}"
    spec = importlib.util.spec_from_file_location(
        name, COMPANION / "__init__.py", submodule_search_locations=[str(COMPANION)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clean_pending(companion):
    companion.PENDING.clear()
    yield
    companion.PENDING.clear()


@pytest.fixture
def active_provider(plugin_package, make_provider):
    """A provider initialized in ``plugin_package``, restored afterwards."""
    previous = plugin_package._ACTIVE_PROVIDER
    provider = make_provider()
    plugin_package._ACTIVE_PROVIDER = provider
    yield provider
    plugin_package._ACTIVE_PROVIDER = previous


@pytest.fixture
def stub_modules():
    """Register throwaway modules as loaded ``minir`` copies; they are removed at teardown."""
    added: list[types.ModuleType] = []

    def add(**attrs) -> types.ModuleType:
        module = types.ModuleType(f"minir_stub_{len(added)}")
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[module.__name__] = module
        added.append(module)
        return module

    yield add
    for module in added:
        sys.modules.pop(module.__name__, None)


class FakeCtx:
    """The plugin-context surface ``register()`` uses, recording what it was handed."""

    def __init__(self, *, inject: bool = True):
        self.commands: dict[str, types.SimpleNamespace] = {}
        self.hooks: dict[str, object] = {}
        self.injected: list[str] = []
        if inject:
            self.inject_message = self._inject

    def _inject(self, content, role="user", **kwargs):
        self.injected.append(content)
        return True

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = types.SimpleNamespace(handler=handler, **kwargs)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class BareCtx:
    """A host with no command surface."""


def _handler(ctx: FakeCtx):
    return ctx.commands["adviser"].handler


def test_register_publishes_the_command_and_the_flush_hook(companion):
    ctx = FakeCtx()
    companion.register(ctx)

    assert list(ctx.commands) == ["adviser"]
    command = ctx.commands["adviser"]
    assert command.args_hint == "multagents | dispatch | status"
    assert "hermes-dispatch" in command.description
    assert ctx.hooks == {"pre_llm_call": companion.flush_pending}


def test_register_on_a_host_without_a_command_surface(companion):
    """No command surface is a warning, never a raise: the plugin must stay loadable anywhere."""
    companion.register(BareCtx())


def test_the_loaded_minir_package_publishes_all_three_seams(companion, plugin_package):
    """A rename on either side breaks /adviser silently; this is the contract's only guard."""
    for name in (companion.SEAM_COMMAND, companion.SEAM_VERB, companion.SEAM_KICKOFF):
        assert callable(getattr(plugin_package, name, None)), name


def test_a_live_seam_answer_is_returned(companion, stub_modules):
    stub_modules(**{companion.SEAM_COMMAND: lambda raw, *, session_id="", deliver=None: "seam line"})
    ctx = FakeCtx()
    companion.register(ctx)

    assert _handler(ctx)("status") == "seam line"


def test_a_failing_seam_falls_through_to_the_next(companion, stub_modules):
    def broken(raw, *, session_id="", deliver=None):
        raise RuntimeError("seam exploded")

    stub_modules(**{companion.SEAM_COMMAND: broken})
    stub_modules(**{companion.SEAM_COMMAND: lambda *a, **k: "second"})

    assert companion.run_adviser("status") == "second"


@pytest.fixture
def cold(companion, monkeypatch):
    """The first keystroke of a process: nothing publishes the seams and nothing resolves.

    The seam names are renamed so the minir package this test session already imported cannot
    answer, and the package resolver is stubbed so the host loader is never reached.
    """
    for attr in ("SEAM_COMMAND", "SEAM_VERB", "SEAM_KICKOFF"):
        monkeypatch.setattr(companion, attr, f"_cold_{attr.lower()}")
    monkeypatch.setattr(companion, "_provider_package", lambda: None)
    monkeypatch.setattr(companion, "_configured_provider", lambda: "minir")
    return companion


def test_without_any_seam_the_command_says_so(cold):
    ctx = FakeCtx()
    cold.register(ctx)

    assert _handler(ctx)("multagents") == cold.NOT_READY


def test_a_cold_command_names_another_memory_provider(cold, monkeypatch):
    """Arming under a different provider would queue a turn that can never apply."""
    monkeypatch.setattr(cold, "_configured_provider", lambda: "lancedb")
    ctx = FakeCtx()
    cold.register(ctx)

    assert _handler(ctx)("multagents") == cold.NOT_MINIR.format(name="lancedb")


def test_a_cold_command_resolves_the_provider_package_and_defers(cold, monkeypatch):
    """The provider loads on the first turn, so the command asks the installed package itself."""
    package = types.SimpleNamespace()
    setattr(package, cold.SEAM_VERB, lambda raw: "arm")
    setattr(package, cold.SEAM_KICKOFF, lambda: "[adviser] kickoff")
    asked: list[int] = []
    monkeypatch.setattr(cold, "_provider_package", lambda: (asked.append(1), package)[1])
    ctx = FakeCtx()
    cold.register(ctx)

    line = _handler(ctx)("multagents", session_id="s-cold")

    assert asked, "the cold path must resolve the provider package"
    assert cold.PENDING == {"s-cold": "multagents"}
    assert ctx.injected == ["[adviser] kickoff"]
    assert line == cold.PENDING_QUEUED


# -- with a provider -------------------------------------------------------------

def test_an_initialized_provider_answers_status(companion, active_provider):
    assert companion.run_adviser("status") == "adviser: mode=dispatch (hermes-dispatch governs)."


def test_arming_arms_the_switch_and_queues_the_ready_check(companion, plugin_package, active_provider):
    ctx = FakeCtx()
    companion.register(ctx)

    line = _handler(ctx)("multagents", session_id=active_provider._session_id)

    assert active_provider._modes.mode(active_provider._session_id) == "multagents"
    assert ctx.injected == [plugin_package.adviser_command.KICKOFF_TEXT]
    assert "ready-check" in line


def test_the_gateway_session_key_reaches_the_provider_switch(companion, active_provider):
    """``invoke_plugin_command`` passes only the keywords a handler declares; both must land."""
    ctx = FakeCtx(inject=False)
    companion.register(ctx)

    _handler(ctx)("multagents", session_key="gateway-session")

    assert active_provider._modes.mode("gateway-session") == "multagents"


# -- before a provider exists ----------------------------------------------------

def test_arm_is_queued_and_starts_the_ready_check_turn(
        companion, plugin_package, monkeypatch):
    monkeypatch.setattr(plugin_package, "_ACTIVE_PROVIDER", None)
    ctx = FakeCtx()
    companion.register(ctx)

    line = _handler(ctx)("multagents", session_id="s-9")

    assert companion.PENDING == {"s-9": "multagents"}
    assert ctx.injected == [plugin_package.adviser_command.KICKOFF_TEXT]
    assert line == companion.PENDING_QUEUED


def test_without_an_injector_the_arm_stays_queued(companion, plugin_package, monkeypatch):
    monkeypatch.setattr(plugin_package, "_ACTIVE_PROVIDER", None)
    ctx = FakeCtx(inject=False)
    companion.register(ctx)

    line = _handler(ctx)("multagents", session_id="s-9")

    assert companion.PENDING == {"s-9": "multagents"}
    assert line == companion.PENDING_NO_TURN


def test_status_before_a_provider_is_honest(companion, plugin_package, monkeypatch):
    monkeypatch.setattr(plugin_package, "_ACTIVE_PROVIDER", None)
    ctx = FakeCtx()
    companion.register(ctx)

    assert _handler(ctx)("status", session_id="s-9") == companion.STATUS_NOT_LOADED
    assert companion.PENDING == {}
    assert ctx.injected == []


def test_status_reports_a_queued_arm(companion, plugin_package, monkeypatch):
    monkeypatch.setattr(plugin_package, "_ACTIVE_PROVIDER", None)
    ctx = FakeCtx()
    companion.register(ctx)
    _handler(ctx)("multagents", session_id="s-9")

    assert _handler(ctx)("status", session_id="s-9") == companion.STATUS_PENDING


def test_dispatch_drops_a_queued_arm(companion, plugin_package, monkeypatch):
    monkeypatch.setattr(plugin_package, "_ACTIVE_PROVIDER", None)
    ctx = FakeCtx()
    companion.register(ctx)
    _handler(ctx)("multagents", session_id="s-9")

    assert _handler(ctx)("dispatch", session_id="s-9") == companion.CLEARED_PENDING
    assert companion.PENDING == {}


def test_an_unknown_verb_prints_usage(companion, plugin_package, monkeypatch):
    """The vocabulary is ``minir``'s: an unknown word must not be queued as an arm."""
    monkeypatch.setattr(plugin_package, "_ACTIVE_PROVIDER", None)
    ctx = FakeCtx()
    companion.register(ctx)

    assert _handler(ctx)("sideways", session_id="s-9") == companion.USAGE
    assert companion.PENDING == {}


# -- the flush hook --------------------------------------------------------------

def test_flush_applies_the_queued_arm_once_the_provider_exists(companion, plugin_package):
    previous = plugin_package._ACTIVE_PROVIDER
    companion.PENDING["s-9"] = "multagents"
    provider = plugin_package.MinirMemoryProvider()

    class _Store:
        """``initialize`` needs a store; the hook only needs the switch."""

        def close(self):
            pass

    provider._store = _Store()
    plugin_package._ACTIVE_PROVIDER = provider
    try:
        assert companion.flush_pending(session_id="s-9") is None
        assert provider._modes.mode("s-9") == "multagents"
        assert companion.PENDING == {}
    finally:
        plugin_package._ACTIVE_PROVIDER = previous


def test_flush_matches_a_composite_session_key(companion, plugin_package):
    """A command before the first turn knows the host's key; the hook gets the agent's id."""
    previous = plugin_package._ACTIVE_PROVIDER
    provider = plugin_package.MinirMemoryProvider()

    class _Store:
        def close(self):
            pass

    provider._store = _Store()
    plugin_package._ACTIVE_PROVIDER = provider
    companion.PENDING["agent:main:local:dm:local-abc123"] = "multagents"
    try:
        assert companion.flush_pending(session_id="local-abc123") is None
        assert provider._modes.mode("local-abc123") == "multagents"
        assert companion.PENDING == {}
    finally:
        plugin_package._ACTIVE_PROVIDER = previous


def test_flush_leaves_another_sessions_arm_queued(companion, plugin_package):
    previous = plugin_package._ACTIVE_PROVIDER
    provider = plugin_package.MinirMemoryProvider()

    class _Store:
        def close(self):
            pass

    provider._store = _Store()
    plugin_package._ACTIVE_PROVIDER = provider
    companion.PENDING["agent:main:local:dm:local-zzz999"] = "multagents"
    try:
        assert companion.flush_pending(session_id="local-abc123") is None
        assert provider._modes.mode("local-abc123") == "dispatch"
        assert companion.PENDING == {"agent:main:local:dm:local-zzz999": "multagents"}
    finally:
        plugin_package._ACTIVE_PROVIDER = previous


def test_flush_keeps_the_arm_queued_without_a_provider(companion, plugin_package, monkeypatch):
    monkeypatch.setattr(plugin_package, "_ACTIVE_PROVIDER", None)
    companion.PENDING["s-9"] = "multagents"

    assert companion.flush_pending(session_id="s-9") is None
    assert companion.PENDING == {"s-9": "multagents"}
