"""Test bootstrap for the Minir memory plugin.

The suite prefers the REAL Hermes host modules and falls back per module:

* ``agent.memory_provider`` — the ``MemoryProvider`` ABC, ``RecallStatus``,
  ``is_trivial_prompt``, ``spawn_context_thread``.
* ``tools.registry`` — ``tool_error``.

When both resolve (running under the host venv, or with ``HERMES_WORKSPACE``
pointing at a Hermes source checkout) the suite checks the actual contract —
that is the authoritative mode. Anything that fails to import is replaced by a
minimal shim so the suite stays runnable and hermetic on any machine. If the
shim and the host ever disagree, the host wins and the shim is what to fix;
``CONTRACT_MODE`` records which mode the run used.

The plugin package is loaded the way the Hermes loader loads it: a synthetic
package whose root is this directory (``__init__.py`` + relative imports).
"""

from __future__ import annotations

import contextvars
import importlib.util
import json
import os
import re
import sys
import threading
import types
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

_workspace = os.environ.get("HERMES_WORKSPACE", "").strip()
if _workspace and _workspace not in sys.path:
    sys.path.insert(0, _workspace)


def _try_import(name: str) -> Optional[types.ModuleType]:
    try:
        return __import__(name, fromlist=["_"])
    except BaseException as exc:  # missing, or a host module needing more runtime than the test env has
        _IMPORT_ERRORS[name] = f"{type(exc).__name__}: {exc}"
        return None


_IMPORT_ERRORS: dict[str, str] = {}

_host_memory_provider = _try_import("agent.memory_provider")
_host_tool_registry = _try_import("tools.registry")
CONTRACT_MODE = "host" if _host_memory_provider and _host_tool_registry else (
    "shim" if not _host_memory_provider and not _host_tool_registry else "partial")

if _workspace and CONTRACT_MODE != "host":
    # Pointing at a workspace and quietly testing against the shim is a lie: the shim cannot
    # fail, so a contract break would show up as a pass. Fail loudly instead, with the reason.
    _reasons = "; ".join(f"{name} -> {error}" for name, error in _IMPORT_ERRORS.items()) or "unknown import failure"
    raise RuntimeError(
        f"HERMES_WORKSPACE={_workspace!r} is set but the host modules did not import "
        f"(contract mode = {CONTRACT_MODE}): {_reasons}. "
        f"Install the workspace's dependencies, or unset HERMES_WORKSPACE to run the hermetic shim suite."
    )


def _namespace(name: str) -> types.ModuleType:
    """Get-or-create a module; created parents get an empty ``__path__`` so they act as packages."""
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        if "." not in name:
            module.__path__ = []  # a bare module cannot host submodule imports
        sys.modules[name] = module
    return module


def _install_memory_provider_shim() -> None:
    """Minimal stand-in for ``agent.memory_provider`` (only the surface this plugin uses)."""
    provider_module = _namespace("agent.memory_provider")

    class MemoryProvider:  # noqa: D101 - the real docstrings live in agent/memory_provider.py
        pre_compress_checkpoint_api_version = 1

        @property
        def name(self) -> str:
            raise NotImplementedError

        def is_available(self) -> bool:
            raise NotImplementedError

        def initialize(self, session_id: str, **kwargs) -> None:
            raise NotImplementedError

        def unavailable_reason(self) -> str:
            return ""

        def system_prompt_block(self) -> str:
            return ""

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
            return None

        def recall_status(self):
            return None

        def sync_turn(self, user_content: str, assistant_content: str, *,
                      session_id: str = "", messages=None, turn_author=None) -> None:
            return None

        def get_tool_schemas(self) -> list:
            raise NotImplementedError

        def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
            raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

        def shutdown(self) -> None:
            return None

        def on_session_end(self, messages) -> None:
            return None

        def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                              reset: bool = False, rewound: bool = False, **kwargs) -> None:
            return None

        def on_pre_compress(self, messages) -> str:
            return ""

        def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
            return None

        def get_config_schema(self) -> list:
            return []

        def save_config(self, values: dict, hermes_home: str) -> None:
            return None

        def backup_paths(self) -> list:
            return []

    def spawn_context_thread(target, *, name: str, daemon: bool = True,
                             args: tuple = (), kwargs: Optional[dict] = None) -> threading.Thread:
        context = contextvars.copy_context()
        return threading.Thread(target=lambda: context.run(target, *args, **(kwargs or {})),
                                name=name, daemon=daemon)

    _trivial = re.compile(r"^(yes|no|ok|okay|sure|thanks|thank you|hi|hey|hello|"
                          r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)$",
                          re.IGNORECASE)

    def is_trivial_prompt(text) -> bool:
        stripped = (text or "").strip()
        return not stripped or stripped.startswith("/") or bool(_trivial.match(stripped.strip("!.?:;, ")))

    class RecallStatus:  # dataclass stand-in
        def __init__(self, provider_label: str, count: int, glyph: str = "🧠"):
            self.provider_label, self.count, self.glyph = provider_label, count, glyph

    provider_module.MemoryProvider = MemoryProvider
    provider_module.RecallStatus = RecallStatus
    provider_module.is_trivial_prompt = is_trivial_prompt
    provider_module.spawn_context_thread = spawn_context_thread
    provider_module.INDICATOR_GLYPH = "🧠"
    provider_module.CORE_MEMORY_PROVIDER_SENTINELS = frozenset({"", "default", "builtin", "built-in", "none"})
    _namespace("agent").memory_provider = provider_module


def _install_tool_registry_shim() -> None:
    """Minimal stand-in for ``tools.registry`` (only the surface this plugin uses)."""
    registry = _namespace("tools.registry")

    def tool_error(message, **extra) -> str:
        return json.dumps({"error": str(message)[:2000], **extra}, ensure_ascii=False)

    def tool_result(data: Optional[Any] = None, **kwargs) -> str:
        return json.dumps(data if data is not None else kwargs, ensure_ascii=False)

    registry.tool_error = tool_error
    registry.tool_result = tool_result
    _namespace("tools").registry = registry


if _host_memory_provider is None:
    _install_memory_provider_shim()
if _host_tool_registry is None:
    _install_tool_registry_shim()


def pytest_report_header(config) -> str:
    return f"minir: contract mode = {CONTRACT_MODE}"


@pytest.fixture(scope="session")
def contract_mode() -> str:
    """``"host"`` | ``"partial"`` | ``"shim"`` — which bootstrap mode this run used."""
    return CONTRACT_MODE


@pytest.fixture(scope="session")
def plugin_package():
    """Load this plugin directory as a package, mirroring the Hermes loader."""
    name = f"minir_pkg_{os.getpid()}"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def make_provider(plugin_package, tmp_path):
    """Factory: ``make_provider(config=None, **initialize_kwargs)`` -> initialized provider.

    Every provider is bound to a throwaway ``hermes_home`` (tmp_path) and shut down
    when the test ends.
    """
    created = []

    def _make(config=None, **init_kwargs):
        provider = plugin_package.MinirMemoryProvider(config)
        init_kwargs.setdefault("hermes_home", str(tmp_path))
        init_kwargs.setdefault("platform", "cli")
        provider.initialize(init_kwargs.pop("session_id", "session-test"), **init_kwargs)
        created.append(provider)
        return provider

    yield _make
    for provider in created:
        provider.shutdown()


@pytest.fixture(autouse=True)
def _clean_minir_env(monkeypatch):
    """Config written by tests must not be shadowed by the developer's environment."""
    for key in ("MINIR_CAPTURE_TURNS", "MINIR_PREFETCH_LIMIT", "MINIR_BASE_INTERVAL",
                "MINIR_DIALECTIC_INTERVAL", "MINIR_SESSION_MODE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _clean_tracker(plugin_package):
    """The counting registry is process-wide (see turn_counter.TRACKER), so isolate tests:
    drop every session and restore the default cadence a previous test may have configured."""
    counter = plugin_package.turn_counter

    def _reset():
        counter.TRACKER.reset()
        counter.TRACKER.configure(base_interval=counter.DEFAULT_BASE_INTERVAL,
                                  dialectic_interval=counter.DEFAULT_DIALECTIC_INTERVAL,
                                  default_mode=counter.DEFAULT_SESSION_MODE)

    _reset()
    yield
    _reset()


@pytest.fixture()
def provider(make_provider):
    """Default provider with turn capture enabled."""
    return make_provider({"capture_turns": True, "prefetch_limit": 5})
