"""One-command minir install: copy the plugin, then apply the host patch series.

`hermes plugins install` only lays files down; the adviser's steer/continue
channels also need a small, version-gated host patch. This script does both in the
safe order, keeps a backup of the deployed plugin, and refuses to leave a
half-patched host behind.

    python scripts/install.py              # copy + patch + verify
    python scripts/install.py --check      # report only
    python scripts/install.py --no-copy    # patch an install made by hermes itself
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_host_patches as host_patches  # noqa: E402  (sibling script)

PLUGIN_NAME = "minir"
#: Deployed side by side. The provider directory holds the memory system; ``minir-adviser`` is an
#: ordinary plugin because the host refuses an exclusive (provider-config) plugin at discovery, and
#: a command registered from the provider path does not exist before the first turn.
PLUGIN_DIRS = ("minir", "minir-adviser")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def _copy_plugin(source: Path, target: Path, *, check: bool) -> Optional[Path]:
    """Mirror the plugin directory, keeping a pre-install copy of the old one."""
    if not (source / "plugin.yaml").is_file():
        raise host_patches.InstallerError(f"not a plugin directory (no plugin.yaml): {source}")
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
    print(f"installed: {target}")
    return backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install minir and apply its host patch series.")
    parser.add_argument("--hermes-home", type=Path,
                        help="profile home (default: HERMES_HOME or %%LOCALAPPDATA%%/hermes)")
    parser.add_argument("--source", type=Path, help="plugin source directory (default: <repo>/plugin/minir)")
    parser.add_argument("--no-copy", action="store_true", help="skip the plugin copy (already installed)")
    parser.add_argument("--no-patches", action="store_true", help="skip the host patch series")
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    parser.add_argument("--force", action="store_true", help="apply over local edits to touched host files")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    hermes_home = args.hermes_home or host_patches.default_hermes_home()
    primary = args.source or host_patches.repo_root() / "plugin" / PLUGIN_NAME
    sources = [args.source] if args.source else [
        host_patches.repo_root() / "plugin" / name for name in PLUGIN_DIRS
    ]
    host = host_patches.default_host(hermes_home)
    patches_dir = primary / "host-patches"
    try:
        if not args.no_copy:
            for source in sources:
                _copy_plugin(source, Path(hermes_home) / "plugins" / source.name, check=args.check)
        if args.no_patches:
            return host_patches.EXIT_OK
        return host_patches.apply_series(
            host, patches_dir, check=args.check, force=args.force,
            state_path=Path(hermes_home) / "minir-memory" / host_patches.STATE_FILENAME,
            backup_root=Path(hermes_home) / "minir-memory" / host_patches.BACKUP_DIRNAME,
        )
    except host_patches.InstallerError as exc:
        print(f"error: {exc}")
        return host_patches.EXIT_BLOCKED
    except OSError as exc:
        print(f"error: {exc}")
        return host_patches.EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
