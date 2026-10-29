"""Contract tests for scripts/install_host_patches.py (no live host needed)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import install_host_patches as ihp  # noqa: E402

BASE = "VALUE = 1\n" + "".join(f"LINE_{i} = {i}\n" for i in range(2, 10))
PATCHED = BASE.replace("LINE_9 = 9", "LINE_9 = 99")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), text=True, capture_output=True, check=True,
        encoding="utf-8", errors="replace",
    )


@pytest.fixture
def repo(tmp_path):
    host = tmp_path / "hermes-agent"
    host.mkdir()
    _git(host, "init", "-q")
    _git(host, "config", "user.email", "test@example.com")
    _git(host, "config", "user.name", "test")
    # A ten-line file: the patch changes the last line, so a local edit on line 1
    # stays outside the hunk context (the --force case below).
    base = BASE
    (host / "module.py").write_text(base, encoding="utf-8")
    _git(host, "add", ".")
    _git(host, "commit", "-q", "-m", "base")
    patches = tmp_path / "host-patches"
    patches.mkdir()
    (host / "module.py").write_text(PATCHED, encoding="utf-8")
    diff = _git(host, "diff").stdout
    (patches / "0001-change-value.patch").write_text(diff, encoding="utf-8")
    _git(host, "checkout", "--", "module.py")
    digest = hashlib.sha256((patches / "0001-change-value.patch").read_bytes()).hexdigest()
    (patches / "manifest.json").write_text(json.dumps({
        "series": "test-v1",
        "capability": "test",
        "base_version": "0.0.0",
        "patches": [{"name": "0001-change-value.patch", "sha256": digest}],
    }), encoding="utf-8")
    return host, patches


def _run(host: Path, patches: Path, *, check=False, force=False):
    logs: list[str] = []
    code = ihp.apply_series(
        host, patches, state_path=host.parent / "state.json",
        backup_root=host.parent / "backup", check=check, force=force, log=logs.append,
    )
    return code, logs


@pytest.fixture(autouse=True)
def _stub_verification(monkeypatch):
    monkeypatch.setattr(ihp, "verify", lambda host, files: (True, "stub"))
    monkeypatch.setattr(ihp, "verify_reverted", lambda host: (True, "stub"))


def test_apply_records_state_and_is_idempotent(repo):
    host, patches = repo
    code, logs = _run(host, patches)
    assert code == ihp.EXIT_OK
    assert (host / "module.py").read_text(encoding="utf-8") == PATCHED
    state = json.loads((host.parent / "state.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in state["applied"]] == ["0001-change-value.patch"]
    assert (host.parent / "backup").is_dir()

    code, logs = _run(host, patches)
    assert code == ihp.EXIT_OK
    assert any("already applied" in line for line in logs)


def test_check_mode_changes_nothing(repo):
    host, patches = repo
    code, logs = _run(host, patches, check=True)
    assert code == ihp.EXIT_OK
    assert (host / "module.py").read_text(encoding="utf-8") == BASE
    assert not (host.parent / "state.json").exists()
    assert any(line.startswith("would apply") for line in logs)


def test_local_edits_block_the_series_unless_forced(repo):
    host, patches = repo
    edited = BASE.replace("VALUE = 1", "VALUE = 99", 1)
    (host / "module.py").write_text(edited, encoding="utf-8")
    code, logs = _run(host, patches)
    assert code == ihp.EXIT_BLOCKED
    assert (host / "module.py").read_text(encoding="utf-8") == edited

    code, _ = _run(host, patches, force=True)
    assert code == ihp.EXIT_OK
    text = (host / "module.py").read_text(encoding="utf-8")
    assert text.startswith("VALUE = 99\n")
    assert text.endswith("LINE_9 = 99\n")


def test_revert_restores_the_pre_image(repo):
    host, patches = repo
    assert _run(host, patches)[0] == ihp.EXIT_OK
    code = ihp.revert_series(
        host, patches, state_path=host.parent / "state.json", log=lambda line: None,
    )
    assert code == ihp.EXIT_OK
    assert (host / "module.py").read_text(encoding="utf-8") == BASE
    state = json.loads((host.parent / "state.json").read_text(encoding="utf-8"))
    assert state["applied"] == []


def test_a_modified_patch_is_refused(repo):
    host, patches = repo
    patch = patches / "0001-change-value.patch"
    patch.write_text(patch.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    code, logs = _run(host, patches)
    assert code == ihp.EXIT_BLOCKED
    assert any("checksum" in line for line in logs)
    assert (host / "module.py").read_text(encoding="utf-8") == BASE


def test_failed_verification_rolls_back(repo, monkeypatch):
    host, patches = repo
    monkeypatch.setattr(ihp, "verify", lambda host, files: (False, "import failed"))
    code, logs = _run(host, patches)
    assert code == ihp.EXIT_BLOCKED
    assert (host / "module.py").read_text(encoding="utf-8") == BASE
    assert any("rolled back" in line for line in logs)


def _dependent_repo(tmp_path):
    """A two-patch series where patch 2 edits the file patch 1 creates."""
    host = tmp_path / "hermes-agent"
    host.mkdir()
    _git(host, "init", "-q")
    _git(host, "config", "user.email", "test@example.com")
    _git(host, "config", "user.name", "test")
    (host / "base.txt").write_text("base\n", encoding="utf-8")
    _git(host, "add", ".")
    _git(host, "commit", "-q", "-m", "base")
    patches = tmp_path / "host-patches"
    patches.mkdir()

    # Patch 1: create new.py at A = 1.
    (host / "new.py").write_text("A = 1\n", encoding="utf-8")
    _git(host, "add", "-N", "new.py")
    (patches / "0001-add-new.patch").write_text(_git(host, "diff").stdout, encoding="utf-8")
    (host / "new.py").unlink()
    _git(host, "reset", "-q")

    # Patch 2: commit the file, edit it, take that diff, then drop the temp commit.
    (host / "new.py").write_text("A = 1\n", encoding="utf-8")
    _git(host, "add", "new.py")
    _git(host, "commit", "-q", "-m", "temp add")
    (host / "new.py").write_text("A = 2\n", encoding="utf-8")
    (patches / "0002-touch-new.patch").write_text(_git(host, "diff").stdout, encoding="utf-8")
    _git(host, "reset", "-q", "--hard", "HEAD~1")

    entries = []
    for name in ("0001-add-new.patch", "0002-touch-new.patch"):
        digest = hashlib.sha256((patches / name).read_bytes()).hexdigest()
        entries.append({"name": name, "sha256": digest})
    (patches / "manifest.json").write_text(json.dumps({
        "series": "dependent-v1", "capability": "test", "base_version": "0.0.0",
        "patches": entries,
    }), encoding="utf-8")
    return host, patches


def test_a_series_may_build_on_its_own_new_files(tmp_path):
    host, patches = _dependent_repo(tmp_path)
    code, logs = _run(host, patches)
    assert code == ihp.EXIT_OK
    assert (host / "new.py").read_text(encoding="utf-8") == "A = 2\n"

    # --check cannot preflight patch 2 in isolation; it must not report a false conflict.
    check_logs: list[str] = []
    code = ihp.apply_series(
        host, patches, state_path=host.parent / "state2.json",
        backup_root=host.parent / "backup2", check=True, log=check_logs.append,
    )
    assert code == ihp.EXIT_OK
    assert any("already applied" in line for line in check_logs)


def _survivor_repo(tmp_path):
    """A two-patch series whose late patch only creates a file, so it survives a revert alone."""
    host = tmp_path / "hermes-agent"
    host.mkdir()
    _git(host, "init", "-q")
    _git(host, "config", "user.email", "test@example.com")
    _git(host, "config", "user.name", "test")
    (host / "module.py").write_text(BASE, encoding="utf-8")
    _git(host, "add", ".")
    _git(host, "commit", "-q", "-m", "base")
    patches = tmp_path / "host-patches"
    patches.mkdir()

    (host / "module.py").write_text(PATCHED, encoding="utf-8")
    (patches / "0001-change-value.patch").write_text(_git(host, "diff").stdout, encoding="utf-8")
    _git(host, "checkout", "--", "module.py")

    (host / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
    _git(host, "add", "-N", "extra.py")
    (patches / "0002-add-extra.patch").write_text(_git(host, "diff").stdout, encoding="utf-8")
    (host / "extra.py").unlink()
    _git(host, "reset", "-q")

    entries = []
    for name in ("0001-change-value.patch", "0002-add-extra.patch"):
        digest = hashlib.sha256((patches / name).read_bytes()).hexdigest()
        entries.append({"name": name, "sha256": digest})
    (patches / "manifest.json").write_text(json.dumps({
        "series": "survivor-v1", "capability": "test", "base_version": "0.0.0",
        "patches": entries,
    }), encoding="utf-8")
    return host, patches


def test_an_upgrade_that_keeps_only_the_created_files_is_not_read_as_applied(tmp_path):
    """The host moved after the install: the last patch still reverse-applies, the earlier one does not.

    Leaving the created file behind (an upgrade reverts tracked hunks only) used to make the prefix
    rule report the whole series as applied, so the re-run silently did nothing.
    """
    host, patches = _survivor_repo(tmp_path)
    assert _run(host, patches)[0] == ihp.EXIT_OK
    assert (host / "module.py").read_text(encoding="utf-8") == PATCHED

    _git(host, "checkout", "--", "module.py")
    _git(host, "commit", "-q", "--allow-empty", "-m", "upgrade")
    assert (host / "module.py").read_text(encoding="utf-8") == BASE

    code, logs = _run(host, patches)
    assert code == ihp.EXIT_BLOCKED
    assert any("does not prove it is still in place" in line for line in logs)
    assert not any("already applied" in line for line in logs)
    # The blocked run leaves the tree as it found it, not half patched.
    assert (host / "module.py").read_text(encoding="utf-8") == BASE

def test_a_drop_forgets_one_name_so_the_rest_can_revert(repo):
    """T5.4: the record outlives the series — a dropped name must not block `--revert`."""
    host, patches = repo
    assert _run(host, patches)[0] == ihp.EXIT_OK
    state_path = host.parent / "state.json"

    # The operator reverses the hunk by hand and removes the patch from the series.
    _git(host, "apply", "--reverse", str(patches / "0001-change-value.patch"))
    assert (host / "module.py").read_text(encoding="utf-8") == BASE
    (patches / "manifest.json").write_text(json.dumps({
        "series": "test-v1", "capability": "test", "base_version": "0.0.0", "patches": [],
    }), encoding="utf-8")
    (patches / "0001-change-value.patch").unlink()

    assert ihp.revert_series(host, patches, state_path=state_path,
                             log=lambda line: None) == ihp.EXIT_BLOCKED

    logs: list[str] = []
    assert ihp.drop_recorded(names=["0001-change-value.patch"], state_path=state_path,
                             log=logs.append) == ihp.EXIT_OK
    assert any("the checkout is untouched" in line for line in logs)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["applied"] == []

    assert ihp.revert_series(host, patches, state_path=state_path,
                             log=lambda line: None) == ihp.EXIT_OK
    assert ihp.drop_recorded(names=["nope.patch"], state_path=state_path,
                             log=logs.append) == ihp.EXIT_OK
    assert any("not recorded" in line for line in logs)

