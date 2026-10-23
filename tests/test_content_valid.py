"""``minir_content_valid``: one local count, two modes, no store needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _report(payload: str) -> dict:
    report = json.loads(payload)
    assert "error" not in report, report
    return report


# -- counts -------------------------------------------------------------------

def test_string_mode_counts_unicode_code_points(plugin_package):
    valid = plugin_package.content_valid
    assert _report(valid.handle({"content": "abcdefghij", "limit": 10}))["measured"] == 10
    assert _report(valid.handle({"content": "记忆存储插", "limit": 5}))["measured"] == 5


def test_the_limit_is_inclusive(plugin_package):
    report = _report(plugin_package.content_valid.handle({"content": "abc", "limit": 3}))
    assert report["ok"] is True and report["over"] == 0 and report["remaining"] == 0


def test_over_and_remaining_are_reported(plugin_package):
    report = _report(plugin_package.content_valid.handle({"content": "abcdef", "limit": 4}))
    assert report["ok"] is False and report["over"] == 2 and report["remaining"] == 0


def test_token_mode_uses_the_plugin_estimator(plugin_package):
    valid = plugin_package.content_valid
    assert _report(valid.handle(
        {"content": "a" * 100, "limit": 100, "mode": "token"}))["measured"] == 28
    report = _report(valid.handle({"content": "记" * 10, "limit": 100, "mode": "token"}))
    assert report["measured"] == 11
    assert report["unit"] == "tokens"
    assert report["estimator"] == "rules.estimate_tokens"
    assert report["measured"] == plugin_package.rules.estimate_tokens("记" * 10)


def test_empty_content_is_a_valid_zero(plugin_package):
    report = _report(plugin_package.content_valid.handle({"content": "", "limit": 5}))
    assert report["ok"] is True and report["measured"] == 0


# -- errors -------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    {"content": "x", "limit": 5, "mode": "chars"},
    {"content": "x", "limit": 0},
    {"limit": 5},
    {"content": 5, "limit": 5},
    {"content": "x"},
])
def test_bad_arguments_are_tool_errors(plugin_package, args):
    assert "error" in json.loads(plugin_package.content_valid.handle(args))


# -- surfaces -----------------------------------------------------------------

def test_the_tool_answers_without_a_store(make_provider, plugin_package):
    provider = make_provider()
    provider._store = None
    payload = json.loads(provider.handle_tool_call(
        "minir_content_valid", {"content": "abc", "limit": 2}))
    assert payload["measured"] == 3 and payload["over"] == 1


def test_the_registry_handler_answers_without_a_provider(plugin_package):
    class _Ctx:
        def __init__(self):
            self.tools = {}

        def register_tool(self, name, toolset, schema, handler, **kwargs):
            self.tools[name] = handler

    ctx = _Ctx()
    plugin_package.plugin_tools.register(ctx, None)
    payload = json.loads(ctx.tools["minir_content_valid"]({"content": "abcd", "limit": 4}))
    assert payload["ok"] is True


def test_the_tool_is_declared_on_every_surface(plugin_package):
    name = plugin_package.content_valid.TOOL_NAME
    assert name in plugin_package.plugin_tools.TOOL_NAMES
    assert name in [schema["name"] for schema in plugin_package.plugin_tools.SCHEMAS]
    manifest = Path(plugin_package.__file__).resolve().parent / "plugin.yaml"
    assert f"  - {name}" in manifest.read_text(encoding="utf-8")
