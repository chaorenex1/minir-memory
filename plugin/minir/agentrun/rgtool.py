"""Managed ripgrep: locate the binary the host installed, then run it for one query.

The knowledge index used to read every page's head itself. That cost grows with the page count,
so :func:`minir.knowledge.select_paths` now asks ripgrep which pages mention a query term and
reads only those heads. The binary is not assumed to be on ``PATH``: it is the one the host's
package manager recorded in ``<store>/facts.json`` under ``packages.ripgrep.entry``. ``PATH`` is
the fallback for a host that installed ripgrep some other way, and "nothing found there either"
is a normal answer, not an error.

Every failure here is reported as ``None`` rather than raised: a turn that cannot run ripgrep
must keep its own scan, not fail because a tool directory moved.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PACKAGE = "ripgrep"
BINARY = "rg.exe" if os.name == "nt" else "rg"
FACTS = "facts.json"
DEFAULT_TIMEOUT = 10.0


def store_root(hermes_home: Any = "") -> Optional[Path]:
    """The tool store holding ``facts.json``: ``HERMES_RUNTIME_DIR`` if pinned, else
    ``<hermes_home>/tools``.

    These are the two roots the plugin can see — the runtime pin a caller exported and the
    profile's own ``tools`` directory. ``None`` means neither was named, so discovery falls back
    to ``PATH``.
    """
    override = os.environ.get("HERMES_RUNTIME_DIR", "").strip()
    if override:
        return Path(override)
    home = str(hermes_home or "").strip() or os.environ.get("HERMES_HOME", "").strip()
    return Path(home) / "tools" if home else None


def _entry(root: Path) -> str:
    """``packages.ripgrep.entry`` from the store's facts; ``""`` when absent or unreadable."""
    try:
        data = json.loads((Path(root) / FACTS).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    packages = data.get("packages") if isinstance(data, dict) else None
    package = packages.get(PACKAGE) if isinstance(packages, dict) else None
    entry = package.get("entry") if isinstance(package, dict) else None
    return entry.strip() if isinstance(entry, str) else ""


def find(hermes_home: Any = "") -> Optional[Path]:
    """The ripgrep binary to run, or ``None`` when this machine has none.

    The recorded entry is checked on disk, so a store that still records ripgrep after its
    unpacked tree was deleted falls through to ``PATH`` instead of returning a dead path.
    """
    root = store_root(hermes_home)
    entry = _entry(root) if root is not None else ""
    if entry:
        candidate = Path(root) / entry / BINARY
        if candidate.is_file():
            return candidate
    found = shutil.which("rg")
    return Path(found) if found else None


def _run(binary: Path, args: list[str],
         timeout: float) -> Optional[subprocess.CompletedProcess]:
    """Run one rg invocation; ``None`` when it could not start, finish, or error out cleanly."""
    try:
        completed = subprocess.run(
            [str(binary), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            # A windowed host (gateway, desktop) must not flash a console for every query.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("ripgrep %s could not run: %s", binary, exc)
        return None
    if completed.returncode not in (0, 1):
        logger.debug("ripgrep exited %s: %s", completed.returncode,
                     completed.stderr.strip()[:300])
        return None
    return completed


def _common(glob: str, ignore_case: bool) -> list[str]:
    """The flags both callers share: no config, no ignore files, no stderr noise, no colour."""
    args = ["--no-messages", "--no-ignore", "--no-config", "--color", "never"]
    if glob:
        args += ["--glob", glob]
    if ignore_case:
        args.append("--ignore-case")
    return args


def _path(value: Any, base: Path) -> Optional[Path]:
    """One reported path as a path (relative output joins ``base``); ``None`` when unusable."""
    text = value.get("text") if isinstance(value, dict) else None
    if not isinstance(text, str) or not text:
        return None  # non-UTF-8 names arrive base64-encoded under "bytes"
    path = Path(text)
    return path if path.is_absolute() else base / path


def search(pattern: str, root: Any, *, glob: str = "", ignore_case: bool = False,
           hermes_home: Any = "", timeout: float = DEFAULT_TIMEOUT) -> Optional[list[Path]]:
    """Files under ``root`` with a line matching ``pattern``; ``None`` when rg could not run.

    ``[]`` is a real answer (exit 1, nothing matched) and stays distinct from ``None`` (missing
    binary, error exit, timeout): the caller keeps its own scan for ``None`` and trusts an empty
    hit list. ``-l`` stops after the first hit per file. ``ignore_case`` is off by default because
    the caller owns its matching semantics; the knowledge index turns it on to mirror its own
    casefolded scoring.
    """
    binary = find(hermes_home)
    if binary is None:
        return None
    completed = _run(binary, ["--files-with-matches", *_common(glob, ignore_case),
                              "--regexp", str(pattern), "--", str(root)], timeout)
    if completed is None:
        return None
    base = Path(root)
    paths: list[Path] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        path = Path(text)
        paths.append(path if path.is_absolute() else base / path)
    return paths


def matches(pattern: str, root: Any, *, glob: str = "", ignore_case: bool = False,
            max_per_file: int = 0, hermes_home: Any = "",
            timeout: float = DEFAULT_TIMEOUT) -> Optional[list[dict[str, Any]]]:
    """One entry per matching line, ``{"path", "line", "text"}``; ``None`` when rg could not run.

    ``--json`` is the only safe format here: a plain ``path:line:text`` row would have to be split
    on ``:``, and the answer is a Windows drive path when the plugin runs on Windows. A
    non-UTF-8 path or line is skipped rather than decoded half-way. ``max_per_file`` becomes
    ``--max-count``, so one chatty page cannot fill the caller's answer; 0 leaves it to ripgrep.
    """
    binary = find(hermes_home)
    if binary is None:
        return None
    args = ["--json", *_common(glob, ignore_case)]
    if max_per_file > 0:
        args += ["--max-count", str(int(max_per_file))]
    completed = _run(binary, [*args, "--regexp", str(pattern), "--", str(root)], timeout)
    if completed is None:
        return None
    base = Path(root)
    hits: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path = _path(data.get("path") or {}, base)
        text = (data.get("lines") or {}).get("text")
        if path is None or not isinstance(text, str):
            continue
        hits.append({"path": path, "line": int(data.get("line_number") or 0),
                     "text": text.rstrip("\r\n")})
    return hits
