#!/usr/bin/env python3
"""Load this plugin through the REAL Hermes memory-provider discovery path.

What it proves (against the host install running on this machine, not a mock):

1. Hermes discovery finds the plugin   — ``plugins.memory.discover_memory_providers()``
2. The loader can load it by name      — ``plugins.memory.load_memory_provider("minir")``
3. It satisfies the real ABC           — ``isinstance(provider, agent.memory_provider.MemoryProvider)``
4. A turn round-trips through the store — ``initialize() -> sync_turn() -> prefetch()``

It works in a throwaway ``HERMES_HOME`` (a temp dir where this repo is copied to
``plugins/minir``); your real Hermes home, config, and memory are untouched.

Usage::

    python scripts/verify_against_host.py                 # auto-detect the host
    python scripts/verify_against_host.py --host-python C:/.../python.exe --workspace C:/.../workspace
    python scripts/verify_against_host.py --keep          # keep the temp home for inspection

Exit code 0 = all checks passed. Any failure prints the host's stderr and exits 1.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "minir"  # must equal plugin.yaml name and the provider's name property

_PROBE = r"""
import json, os, sys

sys.path.insert(0, os.environ["MINIR_HOST_WORKSPACE"])

from agent.memory_provider import MemoryProvider
from plugins.memory import discover_memory_providers, load_memory_provider

names = [name for name, _description, _available in discover_memory_providers()]
assert "%(plugin)s" in names, "provider not discovered; saw: %%s" %% names

provider = load_memory_provider("%(plugin)s", register_skills=False)
assert provider is not None, "load_memory_provider returned None"
assert isinstance(provider, MemoryProvider), "loaded object is not a MemoryProvider"
assert provider.name == "%(plugin)s"

provider.initialize("verify-session", hermes_home=os.environ["HERMES_HOME"], platform="cli")
try:
    provider.sync_turn("The verification probe stores this distinctive sentence", "acknowledged")
    block = provider.prefetch("distinctive sentence")
    assert "distinctive sentence" in block, "prefetch did not recall the stored turn: %%r" %% block
    tools = [schema["name"] for schema in provider.get_tool_schemas()]
    status = provider.recall_status()
    print(json.dumps({
        "discovered": len(names),
        "provider": provider.name,
        "tools": tools,
        "recall_block_chars": len(block),
        "recall_count": getattr(status, "count", None),
    }))
finally:
    provider.shutdown()
"""


def _python_for_workspace(workspace: Path) -> Optional[str]:
    """The venv python that belongs to a workspace's environment directory."""
    environment_dir = workspace.parent
    for candidate in (environment_dir / "venv" / "Scripts" / "python.exe",
                      environment_dir / "venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return None


def detect_host() -> tuple[str, str]:
    """``(python_exe, workspace)`` of the live Hermes install, from ``hermes --version``.

    ``Install directory:`` already names the workspace; sibling environments of the
    install are tried as fallbacks.
    """
    try:
        version = subprocess.run(["hermes", "--version"], capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"`hermes --version` unavailable ({exc}); pass --host-python and --workspace.")
    match = re.search(r"Install directory:\s*(.+)", version.stdout or "")
    workspace = Path(match.group(1).strip()) if match else None

    candidates: list[Path] = []
    if workspace is not None:
        candidates.append(workspace)
        installs_root = next((parent for parent in workspace.parents if parent.name == "installs"), None)
        if installs_root is not None:
            candidates.extend(sorted(installs_root.glob("*/environments/*/workspace")))
    for candidate in candidates:
        if (candidate / "agent" / "memory_provider.py").is_file():
            python = _python_for_workspace(candidate)
            if python:
                return python, str(candidate)
    raise SystemExit("Could not locate the Hermes install. Pass --host-python and --workspace.")


def stage_plugin(home: Path) -> Path:
    """Copy this repo to ``<home>/plugins/<name>`` (the layout discovery expects), minus dev cruft."""
    target = home / "plugins" / PLUGIN_NAME
    shutil.copytree(REPO_ROOT, target, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", "*.pyc", "*.db", "*.db-wal", "*.db-shm"))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host-python", default="", help="python.exe of the Hermes install (default: detect)")
    parser.add_argument("--workspace", default="", help="Hermes source workspace (default: detect)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary HERMES_HOME")
    args = parser.parse_args()

    host_python, workspace = args.host_python, args.workspace
    if not (host_python and workspace):
        host_python, workspace = detect_host()
    print(f"host python : {host_python}")
    print(f"workspace   : {workspace}")

    home = Path(tempfile.mkdtemp(prefix="minir-verify-"))
    try:
        staged = stage_plugin(home)
        print(f"staged      : {staged}")
        env = os.environ.copy()
        env["HERMES_HOME"] = str(home)
        env["MINIR_HOST_WORKSPACE"] = workspace
        env["PYTHONPATH"] = workspace + os.pathsep + env.get("PYTHONPATH", "")
        probe = _PROBE % {"plugin": PLUGIN_NAME}
        result = subprocess.run([host_python, "-c", probe], capture_output=True, text=True, env=env, timeout=180)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            print("FAIL: the host could not load this plugin", file=sys.stderr)
            return 1
        print("result      : " + result.stdout.strip().splitlines()[-1])
        print("PASS: discovery, loading, ABC conformance and a turn round-trip all verified")
        return 0
    finally:
        if args.keep:
            print(f"kept temp home: {home}")
        else:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
