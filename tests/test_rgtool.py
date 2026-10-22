"""Managed ripgrep: discovery from the tool store, then the one search call select_paths makes."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture()
def rgtool(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.rgtool")


def _store(root: Path, rgtool, *, entry: str = "ripgrep-15.2.0-win32-x64",
           binary: bool = True) -> tuple[Path, Path]:
    """A store laid out like the host's: ``facts.json`` beside the unpacked package."""
    folder = Path(root) / entry
    folder.mkdir(parents=True)
    target = folder / rgtool.BINARY
    if binary:
        target.write_bytes(b"")
    payload = {"schema": 1, "packages": {"ripgrep": {"entry": entry}}}
    (Path(root) / "facts.json").write_text(json.dumps(payload), encoding="utf-8")
    return Path(root), target


def test_find_reads_the_recorded_entry(rgtool, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    root, target = _store(tmp_path / "tools", rgtool)
    assert rgtool.find(str(tmp_path)) == target


def test_a_runtime_pin_outranks_the_profile_store(rgtool, tmp_path, monkeypatch):
    pinned = tmp_path / "pinned"
    _root, target = _store(pinned, rgtool)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(pinned))
    assert rgtool.find(str(tmp_path / "other-home")) == target


def test_a_recorded_entry_without_bytes_falls_back_to_path(rgtool, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    _root, target = _store(tmp_path / "tools", rgtool, binary=False)
    elsewhere = tmp_path / "bin" / rgtool.BINARY
    monkeypatch.setattr(rgtool.shutil, "which", lambda name: str(elsewhere))
    assert rgtool.find(str(tmp_path)) == elsewhere

    monkeypatch.setattr(rgtool.shutil, "which", lambda name: None)
    assert rgtool.find(str(tmp_path)) is None


def test_unreadable_facts_is_not_fatal(rgtool, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "facts.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(rgtool.shutil, "which", lambda name: None)
    assert rgtool.find(str(tmp_path)) is None


def test_search_without_ripgrep_is_none_not_empty(rgtool, tmp_path, monkeypatch):
    monkeypatch.setattr(rgtool, "find", lambda hermes_home="": None)
    assert rgtool.search("docker", tmp_path) is None


def test_search_splits_hits_from_misses_and_honours_the_glob(rgtool, tmp_path, monkeypatch):
    if rgtool.find(str(tmp_path)) is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    root = tmp_path / "knowledge"
    (root / "tools").mkdir(parents=True)
    page = root / "tools" / "METHOD-docker.md"
    page.write_text("docker prune\n", encoding="utf-8")
    (root / "tools" / "notes.txt").write_text("docker prune\n", encoding="utf-8")
    (root / "tools" / "other.md").write_text("postgres backup\n", encoding="utf-8")

    assert rgtool.search("docker", root, glob="*.md", hermes_home=str(tmp_path)) == [page]
    assert rgtool.search("granite", root, glob="*.md", hermes_home=str(tmp_path)) == []


def test_search_is_case_insensitive_only_when_asked(rgtool, tmp_path, monkeypatch):
    if rgtool.find(str(tmp_path)) is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    root = tmp_path / "knowledge"
    (root / "tools").mkdir(parents=True)
    page = root / "tools" / "METHOD-docker.md"
    page.write_text("Docker prune\n", encoding="utf-8")

    assert rgtool.search("docker", root, glob="*.md", hermes_home=str(tmp_path)) == []
    assert rgtool.search("docker", root, glob="*.md", ignore_case=True,
                         hermes_home=str(tmp_path)) == [page]


def test_matches_returns_lines_with_numbers(rgtool, tmp_path, monkeypatch):
    if rgtool.find(str(tmp_path)) is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    root = tmp_path / "knowledge"
    (root / "tools").mkdir(parents=True)
    page = root / "tools" / "METHOD-docker.md"
    page.write_text("first line\nDocker prune here\nthird\nDocker prune again\n", encoding="utf-8")

    hits = rgtool.matches("docker", root, glob="*.md", ignore_case=True, max_per_file=1,
                          hermes_home=str(tmp_path))

    assert hits == [{"path": page, "line": 2, "text": "Docker prune here"}]


def test_matches_is_none_for_a_bad_pattern_or_a_missing_binary(rgtool, tmp_path, monkeypatch):
    if rgtool.find(str(tmp_path)) is not None:
        monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
        assert rgtool.matches("(unclosed", tmp_path, hermes_home=str(tmp_path)) is None
    monkeypatch.setattr(rgtool, "find", lambda hermes_home="": None)
    assert rgtool.matches("docker", tmp_path) is None


def test_search_reports_an_error_exit_as_none(rgtool, tmp_path, monkeypatch):
    if rgtool.find(str(tmp_path)) is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    assert rgtool.search("docker", tmp_path / "missing", hermes_home=str(tmp_path)) is None
