"""Apply the minir host patch series to a local hermes-agent checkout.

The adviser's steer/continue channels live in the host because a plugin cannot
reach the live agent object. They ship as a `git format-patch` series under
`plugin/minir/host-patches/`, not as a fork: the installer applies them to the
installed checkout, records exactly what it changed, and can take them back off.

Design rules:

* Idempotent. A patch that is already applied (reverse-check succeeds) is
  recorded and skipped, so re-running an install after a plugin update is safe.
* Reversible. Every file is copied to a timestamped backup before the first
  patch, and `--revert` applies the series in reverse. A failed verification
  rolls the run back instead of leaving a half-patched host.
* Honest about conflict. Local edits to a file the series touches block the run
  (unless `--force`); the host checkout is never reset or stashed.
* Version-gated by outcome, not by a number. `git apply --check` decides whether
  the series matches this host; the manifest base version is printed when it
  does not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

STATE_FILENAME = "host-patches.json"
BACKUP_DIRNAME = "host-patches-backup"
MANIFEST_NAME = "manifest.json"
#: Exit codes: a conflict and a failed verification are different failures.
EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_UNVERIFIED = 3


@dataclass(frozen=True)
class PatchSpec:
    name: str
    path: Path
    sha256: str = ""

    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


class InstallerError(RuntimeError):
    """A blocked or failed install; the message is shown to the operator."""


def default_hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def default_host(hermes_home: Path) -> Path:
    return Path(hermes_home) / "hermes-agent"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_patches_dir() -> Path:
    return repo_root() / "plugin" / "minir" / "host-patches"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), text=True, capture_output=True, check=False,
        encoding="utf-8", errors="replace",
    )


def _git(host: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], host)


def _require_git_repo(host: Path) -> None:
    if not host.is_dir():
        raise InstallerError(f"host checkout not found: {host}")
    probe = _git(host, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise InstallerError(f"not a git checkout: {host}")


def host_head(host: Path) -> str:
    probe = _git(host, "rev-parse", "--short", "HEAD")
    return probe.stdout.strip() if probe.returncode == 0 else ""


def host_version(host: Path) -> str:
    probe = _git(host, "describe", "--tags", "--always")
    return probe.stdout.strip() if probe.returncode == 0 else ""


def load_manifest(patches_dir: Path) -> dict[str, Any]:
    manifest_path = Path(patches_dir) / MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallerError(f"unreadable patch manifest {manifest_path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def resolve_patches(patches_dir: Path, manifest: Optional[dict[str, Any]] = None) -> list[PatchSpec]:
    """Manifest order when declared, filename order otherwise."""
    manifest = manifest if manifest is not None else load_manifest(patches_dir)
    declared = manifest.get("patches") if isinstance(manifest, dict) else None
    specs: list[PatchSpec] = []
    if isinstance(declared, list) and declared:
        for entry in declared:
            if isinstance(entry, str):
                name, digest = entry, ""
            elif isinstance(entry, dict):
                name, digest = str(entry.get("name") or ""), str(entry.get("sha256") or "")
            else:
                continue
            if not name:
                continue
            path = Path(patches_dir) / name
            if not path.is_file():
                raise InstallerError(f"manifest names a missing patch: {path}")
            specs.append(PatchSpec(name=name, path=path, sha256=digest))
        return specs
    for path in sorted(Path(patches_dir).glob("*.patch")):
        specs.append(PatchSpec(name=path.name, path=path))
    return specs


def touched_files(host: Path, patch: Path) -> list[str]:
    """Paths a patch would change, without changing anything (`git apply --numstat`)."""
    probe = _git(host, "apply", "--numstat", str(patch))
    if probe.returncode != 0:
        return []
    files: list[str] = []
    for line in probe.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = parts[-1].strip()
        if path.startswith("b/"):
            path = path[2:]
        if path and path not in files:
            files.append(path)
    return files


def is_applied(host: Path, patch: Path) -> bool:
    probe = _git(host, "apply", "--reverse", "--check", "--whitespace=nowarn", str(patch))
    return probe.returncode == 0


def can_apply(host: Path, patch: Path) -> tuple[bool, str]:
    probe = _git(host, "apply", "--check", "--whitespace=nowarn", str(patch))
    if probe.returncode == 0:
        return True, ""
    message = (probe.stderr or probe.stdout or "git apply --check failed").strip()
    return False, message


def dirty_files(host: Path, files: list[str]) -> list[str]:
    if not files:
        return []
    probe = _git(host, "status", "--porcelain", "--", *files)
    if probe.returncode != 0:
        return []
    dirty: list[str] = []
    for line in probe.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dirty.append(path)
    return dirty


def apply_patch(host: Path, patch: Path) -> None:
    probe = _git(host, "apply", "--whitespace=nowarn", str(patch))
    if probe.returncode != 0:
        raise InstallerError(
            f"git apply failed for {patch.name}: {(probe.stderr or probe.stdout).strip()}"
        )


def revert_patch(host: Path, patch: Path) -> None:
    probe = _git(host, "apply", "--reverse", "--whitespace=nowarn", str(patch))
    if probe.returncode != 0:
        raise InstallerError(
            f"git apply --reverse failed for {patch.name}: "
            f"{(probe.stderr or probe.stdout).strip()}"
        )


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def backup_files(host: Path, files: list[str], backup_root: Path) -> Path:
    """Copy every pre-image out of the checkout; returns the backup directory."""
    target = Path(backup_root) / _stamp()
    target.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []
    for rel in files:
        source = host / rel
        if source.is_file():
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            manifest.append({"path": rel, "state": "copied"})
        else:
            manifest.append({"path": rel, "state": "absent-before"})
    (target / "backup.json").write_text(
        json.dumps({"host": str(host), "files": manifest}, indent=2), encoding="utf-8"
    )
    return target


def read_state(state_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def applied_names(state: dict[str, Any]) -> list[str]:
    rows = state.get("applied") if isinstance(state, dict) else None
    if not isinstance(rows, list):
        return []
    return [str(row.get("name") or "") for row in rows if isinstance(row, dict) and row.get("name")]


def recorded_host_head(state: dict[str, Any]) -> str:
    """The head the recorded series was applied at; '' when nothing recorded one."""
    rows = state.get("applied") if isinstance(state, dict) else None
    if not isinstance(rows, list):
        return ""
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("host_head"):
            return str(row["host_head"])
    return ""


def _record(state: dict[str, Any], spec: PatchSpec, host: Path) -> None:
    rows = state.setdefault("applied", [])
    for row in rows:
        if isinstance(row, dict) and row.get("name") == spec.name:
            row.update({"sha256": spec.digest(), "host_head": host_head(host)})
            return
    rows.append({
        "name": spec.name,
        "sha256": spec.digest(),
        "applied_at": time.time(),
        "host_head": host_head(host),
    })


def _drop(state: dict[str, Any], name: str) -> None:
    rows = state.get("applied")
    if isinstance(rows, list):
        state["applied"] = [row for row in rows if not (isinstance(row, dict) and row.get("name") == name)]


def host_python(host: Path) -> Path:
    for candidate in (
        host / "venv" / "Scripts" / "python.exe",
        host / "venv" / "bin" / "python",
        host / ".venv" / "Scripts" / "python.exe",
        host / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def verify(host: Path, files: list[str]) -> tuple[bool, str]:
    """Import the patched modules with the host interpreter and compile the touched files."""
    python = host_python(host)
    code = (
        "import hermes_cli.plugin_runtime as m; "
        "from agent import turn_stop_gates; "
        "print(m.CAPABILITY_VERSION)"
    )
    probe = _run([str(python), "-c", code], host)
    if probe.returncode != 0:
        return False, (probe.stderr or probe.stdout).strip()[:1200]
    py_files = [rel for rel in files if rel.endswith(".py")]
    if py_files:
        compile_probe = _run([str(python), "-m", "compileall", "-q", *py_files], host)
        if compile_probe.returncode != 0:
            return False, (compile_probe.stderr or compile_probe.stdout).strip()[:1200]
    return True, probe.stdout.strip()


def apply_series(
    host: Path,
    patches_dir: Path,
    *,
    state_path: Optional[Path] = None,
    backup_root: Optional[Path] = None,
    check: bool = False,
    force: bool = False,
    keep_on_failure: bool = False,
    log: Callable[[str], None] = print,
) -> int:
    """Apply (or report) the series. Returns a process exit code; never raises."""
    host = Path(host)
    patches_dir = Path(patches_dir)
    hermes_home = host.parent
    state_path = Path(state_path) if state_path else hermes_home / "minir-memory" / STATE_FILENAME
    backup_root = Path(backup_root) if backup_root else hermes_home / "minir-memory" / BACKUP_DIRNAME
    try:
        _require_git_repo(host)
        manifest = load_manifest(patches_dir)
        specs = resolve_patches(patches_dir, manifest)
        if not specs:
            log(f"no patches under {patches_dir}")
            return EXIT_OK
        state = read_state(state_path)
        state.update({
            "series": manifest.get("series", ""),
            "capability": manifest.get("capability", ""),
            "base_version": manifest.get("base_version", ""),
            "base_commit": manifest.get("base_commit", ""),
            "host": str(host),
        })
        if manifest.get("base_version") and manifest["base_version"] not in host_version(host):
            log(f"note: series targets hermes-agent {manifest['base_version']}; "
                f"host is {host_version(host) or 'unknown'}")

        planned: list[PatchSpec] = []
        first_planned = True
        applied_files: set[str] = set()
        # A series applies in order, so "already applied" is a PREFIX property: later
        # patches may edit a file an earlier one created, which makes the earlier patch
        # fail its own reverse-check even though it is applied. The last patch that
        # reverse-applies therefore marks the whole prefix.
        reverse_flags = [is_applied(host, spec.path) for spec in specs]
        applied_through = max(
            (index for index, flag in enumerate(reverse_flags) if flag), default=-1)
        # A reverse-apply match is evidence only while the checkout has not moved: an upgrade can
        # revert the tracked hunks and leave the files the series created behind, so a late patch
        # matches while every earlier one is gone. That shape must not read as "already applied".
        recorded_head = recorded_host_head(state)
        if applied_through >= 0 and recorded_head and recorded_head != host_head(host):
            log(f"note: the recorded series was applied at {recorded_head}; the host is now "
                f"{host_head(host) or 'unknown'}, so a reverse match does not prove it is still "
                "in place. Checking every patch against this tree.")
            applied_through = -1
        for index, spec in enumerate(specs):
            if spec.sha256 and spec.digest() != spec.sha256:
                raise InstallerError(f"checksum mismatch for {spec.name}; refusing a modified series")
            if index <= applied_through:
                log(f"ok: {spec.name} already applied")
                applied_files.update(touched_files(host, spec.path))
                if not check:
                    _record(state, spec, host)
                continue
            files = touched_files(host, spec.path)
            # Files our own applied prefix changed are expected to be dirty; only an overlap
            # outside the series means the operator has local work in the way.
            dirty = [name for name in dirty_files(host, files) if name not in applied_files]
            if dirty and not force:
                raise InstallerError(
                    "local changes overlap " + spec.name + ": " + ", ".join(dirty) +
                    "\nCommit or revert them, or re-run with --force to apply over them."
                )
            planned.append(spec)
            if check:
                # A series builds on itself: only the first pending patch can be checked
                # against the current tree. Checking a later one here would report the
                # predecessor's new files as missing.
                if first_planned:
                    ok, error = can_apply(host, spec.path)
                    if ok:
                        log(f"would apply: {spec.name} ({len(files)} file(s))")
                    else:
                        log(f"would conflict: {spec.name}\n{error}")
                else:
                    log(f"would apply: {spec.name} (after predecessors)")
            else:
                log(f"apply: {spec.name} ({len(files)} file(s))")
            first_planned = False

        if check or not planned:
            if not check:
                state["updated_at"] = time.time()
                write_state(state_path, state)
            return EXIT_OK

        touched: list[str] = []
        for spec in planned:
            touched.extend(touched_files(host, spec.path))
        touched = list(dict.fromkeys(touched))
        backup_dir = None
        if touched:
            backup_dir = backup_files(host, touched, backup_root)
            log(f"backup: {backup_dir}")

        applied: list[PatchSpec] = []
        try:
            for spec in planned:
                apply_patch(host, spec.path)
                applied.append(spec)
                _record(state, spec, host)
            ok, detail = verify(host, touched)
            if not ok:
                raise InstallerError(f"post-apply verification failed:\n{detail}")
        except InstallerError:
            if applied and not keep_on_failure:
                for spec in reversed(applied):
                    try:
                        revert_patch(host, spec.path)
                        _drop(state, spec.name)
                        log(f"rolled back: {spec.name}")
                    except InstallerError as exc:
                        log(f"rollback failed for {spec.name}: {exc}")
                state["updated_at"] = time.time()
                write_state(state_path, state)
            raise
        state["updated_at"] = time.time()
        state["backup"] = str(backup_dir) if backup_dir else ""
        write_state(state_path, state)
        log(f"verified: {detail}")
        log(f"state: {state_path}")
        return EXIT_OK
    except InstallerError as exc:
        log(f"error: {exc}")
        return EXIT_BLOCKED


def revert_series(
    host: Path,
    patches_dir: Path,
    *,
    state_path: Optional[Path] = None,
    log: Callable[[str], None] = print,
) -> int:
    """Undo every recorded patch, newest first. Stops at the first conflict."""
    host = Path(host)
    patches_dir = Path(patches_dir)
    state_path = Path(state_path) if state_path else host.parent / "minir-memory" / STATE_FILENAME
    try:
        _require_git_repo(host)
        manifest = load_manifest(patches_dir)
        specs = {spec.name: spec for spec in resolve_patches(patches_dir, manifest)}
        state = read_state(state_path)
        names = applied_names(state)
        if not names:
            log("nothing recorded; no patches to revert")
            return EXIT_OK
        for name in reversed(names):
            spec = specs.get(name)
            if spec is None:
                raise InstallerError(f"state names a patch that is not in the series: {name}")
            if not is_applied(host, spec.path):
                log(f"skip: {name} is not applied")
                _drop(state, name)
                continue
            revert_patch(host, spec.path)
            _drop(state, name)
            log(f"reverted: {name}")
            write_state(state_path, state)
        state["updated_at"] = time.time()
        write_state(state_path, state)
        ok, detail = verify_reverted(host)
        if not ok:
            log(f"warning: post-revert check failed: {detail}")
            return EXIT_UNVERIFIED
        log("reverted cleanly")
        return EXIT_OK
    except InstallerError as exc:
        log(f"error: {exc}")
        return EXIT_BLOCKED


def drop_recorded(*, names: list[str], state_path: Path,
                  log: Callable[[str], None] = print) -> int:
    """Forget recorded patches without touching the host checkout.

    A patch can leave the series while a host still carries its record. ``revert_series`` then
    refuses that name, because it has no file to reverse, and the rest of the series stays
    applied. The operator reverses those hunks first (``git apply --reverse``), removes the patch
    from the manifest, and drops the record here so the rest reverts normally. This never edits
    the checkout: dropping a record is bookkeeping, and the warning says so.
    """
    state_path = Path(state_path)
    state = read_state(state_path)
    recorded = applied_names(state)
    unknown = [name for name in names if name not in recorded]
    for name in unknown:
        log(f"skip: {name} is not recorded")
    dropped = [name for name in names if name in recorded]
    if not dropped:
        return EXIT_OK
    for name in dropped:
        _drop(state, name)
    state["updated_at"] = time.time()
    write_state(state_path, state)
    log("dropped from the record (the checkout is untouched): " + ", ".join(dropped))
    log(f"state: {state_path}")
    return EXIT_OK


def verify_reverted(host: Path) -> tuple[bool, str]:
    python = host_python(host)
    code = "import hermes_cli.plugin_runtime"
    probe = _run([str(python), "-c", code], host)
    if probe.returncode == 0:
        return False, "hermes_cli.plugin_runtime still imports after revert"
    return True, "module removed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply or revert the minir host patch series on a hermes-agent checkout."
    )
    parser.add_argument("--host", type=Path, help="hermes-agent checkout (default: <hermes-home>/hermes-agent)")
    parser.add_argument("--hermes-home", type=Path, help="profile home (default: HERMES_HOME or %%LOCALAPPDATA%%/hermes)")
    parser.add_argument("--patches", type=Path, help="patch directory (default: plugin/minir/host-patches)")
    parser.add_argument("--state", type=Path, help="state file (default: <hermes-home>/minir-memory/host-patches.json)")
    parser.add_argument("--check", action="store_true", help="report what would happen; change nothing")
    parser.add_argument("--revert", action="store_true", help="undo the recorded series")
    parser.add_argument("--drop", action="append", default=[], metavar="NAME",
                        help="forget one recorded patch without touching the checkout "
                             "(use after reversing its hunks by hand)")
    parser.add_argument("--force", action="store_true", help="apply over local edits to touched files")
    parser.add_argument("--keep-on-failure", action="store_true", help="do not roll back a failed verification")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    hermes_home = args.hermes_home or default_hermes_home()
    host = args.host or default_host(hermes_home)
    patches_dir = args.patches or default_patches_dir()
    state_path = args.state or Path(hermes_home) / "minir-memory" / STATE_FILENAME
    backup_root = Path(hermes_home) / "minir-memory" / BACKUP_DIRNAME
    if args.drop:
        return drop_recorded(names=args.drop, state_path=state_path)
    if args.revert:
        return revert_series(host, patches_dir, state_path=state_path)
    return apply_series(
        host, patches_dir, state_path=state_path, backup_root=backup_root,
        check=args.check, force=args.force, keep_on_failure=args.keep_on_failure,
    )


if __name__ == "__main__":
    raise SystemExit(main())
