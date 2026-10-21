"""The layer file guard: reads under HERMES_HOME, writes only under minir-memory/scratch."""

from __future__ import annotations

import os

import pytest


@pytest.fixture()
def guard(plugin_package):
    return plugin_package.file_guard


def _home(tmp_path):
    return str(tmp_path)


def test_reads_inside_the_home_are_allowed(guard, tmp_path):
    home = _home(tmp_path)
    target = tmp_path / "minir-memory" / "agents" / "default" / "identity.md"
    target.parent.mkdir(parents=True)
    target.write_text("operator identity", encoding="utf-8")
    assert guard.check("read_file", {"path": str(target)}, home) is None
    assert guard.check("search_files", {"path": str(tmp_path)}, home) is None


def test_reads_outside_the_home_are_refused(guard, tmp_path):
    home = _home(tmp_path / "home")
    outside = str(tmp_path / "elsewhere" / "secret.txt")
    assert "HERMES_HOME" in guard.check("read_file", {"path": outside}, home)


def test_secret_bearing_files_are_not_readable(guard, tmp_path):
    home = _home(tmp_path)
    for name in ("config.yaml", ".env", "credentials.json"):
        target = tmp_path / name
        target.write_text("token", encoding="utf-8")
        assert "not readable" in guard.check("read_file", {"path": str(target)}, home)


def test_path_escapes_are_refused(guard, tmp_path):
    home = _home(tmp_path)
    for raw in ("../../etc/passwd", "\\\\server\\share\\x", "C:/x/file.txt:stream"):
        assert guard.check("read_file", {"path": raw}, home) is not None


def test_writes_are_confined_to_scratch(guard, tmp_path):
    home = _home(tmp_path)
    scratch = tmp_path / "minir-memory" / "scratch" / "draft.md"
    assert guard.check("write_file", {"path": str(scratch)}, home) is None
    assert guard.check("write_file", {"path": str(tmp_path / "minir-memory")}, home) is not None
    for relative in ("minir-memory.db", "minir-memory.db-wal", "agents/default/identity.md",
                     "user/identity.md", "knowledge/tools/K-x.md", "rules/user-self.md",
                     "sessions/s.json"):
        target = tmp_path / relative
        assert guard.check("patch", {"path": str(target)}, home) is not None


def test_a_sibling_directory_is_not_the_store(guard, tmp_path):
    """String prefix comparison would let ``minir-memory-2`` pass; commonpath must not."""
    home = _home(tmp_path)
    target = tmp_path / "minir-memory-2" / "scratch" / "x.md"
    assert guard.check("write_file", {"path": str(target)}, home) is not None


def test_the_scope_marks_only_a_layer_pass(guard):
    assert guard.aux_active() is False
    with guard.aux_scope():
        assert guard.aux_active() is True
    assert guard.aux_active() is False


def test_the_provider_hook_blocks_only_inside_the_scope(plugin_package, make_provider, tmp_path):
    provider = make_provider({})
    outside = str(tmp_path.parent / "outside.txt")
    hook = plugin_package._on_pre_tool_call
    assert hook(tool_name="read_file", provider=provider, args={"path": outside}) is None
    with plugin_package.file_guard.aux_scope():
        blocked = hook(tool_name="read_file", provider=provider, args={"path": outside})
    assert blocked and blocked["action"] == "block"
    # The path is resolved against the real home, not the fixture's tmp_path root.
    allowed = hook(tool_name="read_file", provider=provider,
                   args={"path": os.path.join(provider._hermes_home, "state.db")})
    assert allowed is None
