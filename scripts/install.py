"""One-command minir install: mirror the plugin directories into HERMES_HOME.

`hermes plugins install` only lays files down, and this repo ships two plugin
directories that must land side by side. This script mirrors both, keeps a
pre-install copy of the previous ones, and removes files a newer build no longer
ships. It never touches the host checkout: the control surface the adviser steers
through (`hermes_cli.plugin_runtime`) is part of the host itself.

    python scripts/install.py            # copy + verify
    python scripts/install.py --check    # report only
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path
from typing import Optional

#: Deployed side by side. The provider directory holds the memory system; ``minir-adviser`` is an
#: ordinary plugin because the host refuses an exclusive (provider-config) plugin at discovery, and
#: a command registered from the provider path does not exist before the first turn.
PLUGIN_DIRS = ("minir", "minir-adviser")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
EXIT_OK = 0
EXIT_BLOCKED = 2


class InstallerError(RuntimeError):
    """A blocked install; the message is shown to the operator."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _copy_plugin(source: Path, target: Path, *, check: bool) -> Optional[Path]:
    """Mirror the plugin directory, keeping a pre-install copy of the old one."""
    if not (source / "plugin.yaml").is_file():
        raise InstallerError(f"not a plugin directory (no plugin.yaml): {source}")
    if check:
        print(f"would install: {source} -> {target}")
        return None
    backup = None
    if target.exists():
        backup = (target.parent.parent / "minir-memory" / "plugin-backup"
                  / f"{target.name}-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target, backup, ignore=_IGNORE)
        print(f"backup: {backup}")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=_IGNORE)
    # Clean cutover: files the new build no longer ships must not linger from an older one.
    shipped = {
        path.relative_to(source) for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    for path in target.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.relative_to(target) not in shipped:
            path.unlink()
    # Directories a newer build dropped leave an empty tree behind, which still shows in the
    # deployed layout. Remove the ones the source no longer has, deepest first.
    shipped_dirs = {p.relative_to(source) for p in source.rglob("*") if p.is_dir()}
    for path in sorted((p for p in target.rglob("*") if p.is_dir()),
                       key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(target)
        if rel not in shipped_dirs and not any(path.iterdir()):
            path.rmdir()
    print(f"installed: {target}")
    return backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install minir into a Hermes profile.")
    parser.add_argument("--hermes-home", type=Path,
                        help="profile home (default: HERMES_HOME or %%LOCALAPPDATA%%/hermes)")
    parser.add_argument("--source", type=Path, help="plugin source directory (default: <repo>/plugin/minir)")
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    hermes_home = args.hermes_home or default_hermes_home()
    sources = [args.source] if args.source else [
        repo_root() / "plugin" / name for name in PLUGIN_DIRS
    ]
    try:
        for source in sources:
            _copy_plugin(source, Path(hermes_home) / "plugins" / source.name, check=args.check)
    except InstallerError as exc:
        print(f"error: {exc}")
        return EXIT_BLOCKED
    except OSError as exc:
        print(f"error: {exc}")
        return EXIT_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
