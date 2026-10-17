"""Contract tests — what the Hermes loader and MemoryManager rely on.

These assertions hold against the real host (authoritative) and the shim alike.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from agent.memory_provider import MemoryProvider

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin" / "minir"
REPO_ROOT = PLUGIN_ROOT.parents[1]

_VALID_TOOL_ARG_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def test_provider_subclasses_memory_provider(plugin_package):
    assert issubclass(plugin_package.MinirMemoryProvider, MemoryProvider)


def test_no_abstract_methods_left(plugin_package):
    assert not getattr(plugin_package.MinirMemoryProvider, "__abstractmethods__", frozenset()), \
        "every abstract MemoryProvider method must be implemented"


def test_instantiates_without_host_runtime(plugin_package):
    """The loader constructs providers before initialize(); construction must be side-effect free."""
    provider = plugin_package.MinirMemoryProvider({})
    assert provider.name == "minir"
    assert provider.is_available() is True
    assert provider.unavailable_reason() == ""
    assert provider.backup_paths() == []


def test_register_captures_provider(plugin_package):
    class FakeContext:
        """Stand-in for the host's register context: the provider call plus the plugin
        hooks the plugin subscribes to (``_ProviderCollector.register_hook`` forwards to a
        real ``PluginContext``, so hooks are available on the memory-provider path too)."""

        def __init__(self):
            self.provider = None
            self.hooks = {}

        def register_memory_provider(self, provider):
            self.provider = provider

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = FakeContext()
    plugin_package.register(ctx)
    assert isinstance(ctx.provider, MemoryProvider)
    assert ctx.provider.name == "minir"
    assert ctx.hooks


def test_discovery_markers_present(plugin_package):
    """``plugins/memory`` discovery heuristically scans __init__.py for these markers."""
    source = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8-sig")[:8192]
    assert "register_memory_provider" in source or "MemoryProvider" in source


def test_manifest_name_matches_provider_name(plugin_package):
    """plugin.yaml ``name`` must match the provider's ``name`` property.

    A third copy of this string lives in the *installed directory* name: it is what
    ``memory.provider`` selects (``find_provider_dir(<name>)`` looks up
    ``$HERMES_HOME/plugins/<name>``). Rename all three together — see README.md.
    """
    manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8-sig")
    declared = next(line.split(":", 1)[1].strip() for line in manifest.splitlines()
                    if line.startswith("name:"))
    assert declared == plugin_package.MinirMemoryProvider({}).name
    if PLUGIN_ROOT.name == "minir":
        assert PLUGIN_ROOT.name == declared


def test_pyproject_matches_manifest(plugin_package):
    """The version lives in both plugin.yaml (host metadata) and pyproject.toml (packaging);
    a pip install must not advertise a release the manifest disagrees with."""
    manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8-sig")
    declared = next(line.split(":", 1)[1].strip() for line in manifest.splitlines()
                    if line.startswith("version:"))
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == declared
    assert project["entry-points"]["hermes_agent.memory_providers"]["minir"] == "minir"


def test_manifest_declares_every_registered_hook(plugin_package):
    """``hermes plugins validate`` fails the manifest when ``register()`` subscribes to a hook
    that ``provides_hooks`` does not declare — keep the two lists in step."""
    class FakeContext:
        def __init__(self):
            self.hooks = []

        def register_memory_provider(self, provider):
            pass

        def register_hook(self, name, callback):
            self.hooks.append(name)

    ctx = FakeContext()
    plugin_package.register(ctx)

    declared = []
    in_block = False
    for line in (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("provides_hooks:"):
            in_block = True
            continue
        if in_block:
            if line.startswith("  - "):
                declared.append(line[4:].strip())
            elif line.strip() and not line.startswith(" "):
                break
    assert declared, "register() subscribes to hooks, so provides_hooks must not be empty"
    assert sorted(declared) == sorted(ctx.hooks)
    try:  # host mode only: the names must be real events the host will deliver
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        return
    assert set(declared) <= VALID_HOOKS, sorted(set(declared) - VALID_HOOKS)


def test_tool_schemas_are_openai_shaped(plugin_package):
    schemas = plugin_package.MinirMemoryProvider({}).get_tool_schemas()
    assert schemas, "a memory provider should expose at least one tool"
    names = [schema["name"] for schema in schemas]
    assert len(names) == len(set(names)), "tool names must be unique"
    for schema in schemas:
        assert set(schema) >= {"name", "description", "parameters"}
        assert schema["name"].startswith("minir_"), "prefix tools to avoid cross-plugin collisions"
        assert isinstance(schema["description"], str) and schema["description"]
        parameters = schema["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters.get("required", []), list)
        for prop in parameters["properties"].values():
            assert prop["type"] in _VALID_TOOL_ARG_TYPES


def test_config_schema_fields_are_valid(plugin_package):
    schema = plugin_package.MinirMemoryProvider({}).get_config_schema()
    assert schema, "the template ships two settings; adapt or return []"
    for field in schema:
        assert field["key"] and field["description"]
        assert field["type"] in {"text", "integer", "number", "boolean"}
        # This plugin uses the ABC's env-var-only path, so every field must carry env_var.
        assert field["env_var"].startswith("MINIR_")


def test_tool_responses_are_json_strings(plugin_package, provider):
    for tool_name, args in (("minir_stats", {}), ("minir_search", {"query": "anything"}),
                            ("minir_remember", {"content": "a fact"}), ("minir_forget", {"note_id": 1})):
        result = provider.handle_tool_call(tool_name, args)
        assert isinstance(result, str), f"{tool_name} must return a JSON string"
        assert isinstance(json.loads(result), dict)


def test_unknown_tool_returns_error(plugin_package, provider):
    payload = json.loads(provider.handle_tool_call("minir_nope", {}))
    assert "error" in payload


def test_tool_call_before_initialize_is_refused(plugin_package):
    provider = plugin_package.MinirMemoryProvider({})
    payload = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert "error" in payload
