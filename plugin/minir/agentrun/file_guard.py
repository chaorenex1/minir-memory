"""Path guard for the plugin agents' file tools.

The base, dialectic, review, knowledge and extraction passes can read their evidence, the
durable card files and the project they are working in. The host's file tools are not scoped, so
the scope is applied here, at the ``pre_tool_call`` gate, and only while a plugin agent is
running:

* ``agent_loop.run`` marks the thread (and the worker threads it spawns) as inside a plugin
  agent; a normal turn is never marked, so the guard cannot block the user's own tools;
* reads stay under ``$HERMES_HOME`` and the project root the run passes (the cwd's git root by
  default), minus secret-bearing files and ``.git`` metadata;
* writes stay under ``$HERMES_HOME/minir-memory/scratch``. Cards, knowledge, transcripts and
  rule files are written by code paths only: if the model could write the identity file and
  then cite it, the identity grounding check would prove nothing.

Resolution is ``expanduser`` -> ``realpath`` -> ``normcase`` -> ``commonpath`` against the root,
never a string prefix (``C:\\x\\minir-memory-2`` starts with ``C:\\x\\minir-memory``).

``patch`` has two shapes. Replace mode names one ``path``; V4A mode carries the targets in
header lines, so every header path is checked too (the host's ``patch`` accepts both shapes).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

AUX_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar("minir_aux_scope",
                                                                default=False)
PROJECT_ROOTS: contextvars.ContextVar[tuple[Path, ...]] = contextvars.ContextVar(
    "minir_project_roots", default=())
# How far above the start directory the project marker is looked for.
_PROJECT_LOOKUP_DEPTH = 4
_PROJECT_MARKER = ".git"

READ_TOOLS = frozenset({"read_file", "search_files"})
WRITE_TOOLS = frozenset({"write_file", "patch"})
FILE_TOOLS = READ_TOOLS | WRITE_TOOLS

STORE_DIRNAME = "minir-memory"
SCRATCH_DIRNAME = "scratch"
DENIED_READ_NAMES = frozenset({"config.yaml", ".env"})
DENIED_READ_PREFIXES = ("credentials",)
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
     *(f"lpt{i}" for i in range(1, 10))})
_DRIVE_ADS_RE = re.compile(r"^[A-Za-z]:.*:")
# The two V4A header shapes the host's ``patch`` tool parses (tools/file_tools.py).
_V4A_SINGLE_HEADER_RE = re.compile(r"^\*\*\*\s*(Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
_V4A_MOVE_HEADER_RE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)


def aux_active() -> bool:
    """True when this call runs inside a plugin agent run."""
    return bool(AUX_SCOPE.get())


def _usable_root(root: Path) -> Optional[Path]:
    """Reject a drive root or the user's home: a project read root must be narrower."""
    try:
        if not root.is_dir() or root.parent == root or root == Path.home():
            return None
    except (OSError, RuntimeError):
        return None
    return root


def project_root(start: Any = "") -> Optional[Path]:
    """Nearest git root at or above *start* (cwd by default), else *start* itself.

    This is the one directory the adviser may read outside HERMES_HOME. It is the repository
    root when the cwd sits inside one, so a consult launched from a subdirectory still sees the
    whole project; without a marker the start directory is the best available answer.
    """
    raw = str(start or "").strip()
    if not raw:
        # One deployment-level override for every peer agent; the adviser has its own key.
        raw = str(os.environ.get("MINIR_PROJECT_DIR") or "").strip()
    try:
        base = Path(os.path.expanduser(raw)) if raw else Path.cwd()
        if not base.is_absolute():
            base = Path.cwd() / base
        base = Path(os.path.realpath(base))
        if not base.is_dir():
            return None
        candidate = base
        for _ in range(_PROJECT_LOOKUP_DEPTH):
            if (candidate / _PROJECT_MARKER).exists():
                return _usable_root(candidate)
            if candidate.parent == candidate:
                break
            candidate = candidate.parent
        return _usable_root(base)
    except (OSError, RuntimeError, ValueError):
        return None


@contextlib.contextmanager
def aux_scope(project_dir: Any = "") -> Iterator[None]:
    """Mark a plugin agent run; optionally grant reads under its project root.

    Writes stay under ``minir-memory/scratch`` in every case. Passing no *project_dir* keeps
    the outer roots, so a nested ``agent_loop.run`` cannot silently widen or narrow a scope.
    """
    roots = PROJECT_ROOTS.get()
    if str(project_dir or "").strip():
        root = project_root(project_dir)
        if root is not None and root not in roots:
            roots = (*roots, root)
            logger.debug("Minir file guard: project reads enabled under %s", root)
    token = AUX_SCOPE.set(True)
    root_token = PROJECT_ROOTS.set(roots)
    try:
        yield
    finally:
        PROJECT_ROOTS.reset(root_token)
        AUX_SCOPE.reset(token)


def _blocked_shape(raw: str) -> Optional[str]:
    """Reject path forms that can escape a root regardless of where they resolve."""
    text = str(raw or "").strip()
    if not text:
        return "path is required"
    if "\x00" in text:
        return "path contains a NUL byte"
    if text.startswith("\\\\") or text.startswith("//"):
        return "UNC paths are not allowed"
    if text.startswith("\\\\?\\") or text.startswith("\\\\.\\"):
        return "device paths are not allowed"
    if _DRIVE_ADS_RE.match(text):
        return "alternate data streams are not allowed"
    for part in re.split(r"[\\/]+", text):
        stem = part.split(".", 1)[0].lower()
        if stem in _WINDOWS_RESERVED:
            return f"reserved device name {part!r} is not allowed"
    return None


def _resolve(raw: Any) -> Path:
    """Absolute, symlink-free, case-normalised path for comparison."""
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normcase(os.path.realpath(path)))


def _within(path: Path, root: Path) -> bool:
    left = os.path.normcase(str(path))
    right = os.path.normcase(str(root))
    try:
        return os.path.commonpath([left, right]) == right
    except ValueError:  # different drives
        return False


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return ()


def _v4a_paths(patch: Any) -> list[str]:
    """Every path named in a V4A patch body; both endpoints of a move."""
    text = str(patch or "")
    if not text:
        return []
    paths = [match.group(2).strip() for match in _V4A_SINGLE_HEADER_RE.finditer(text)]
    for match in _V4A_MOVE_HEADER_RE.finditer(text):
        paths.extend((match.group(1).strip(), match.group(2).strip()))
    return [path for path in paths if path]


def _paths_for(name: str, args: dict[str, Any]) -> list[str]:
    """Every target a call can write: ``path`` plus any V4A header paths."""
    raw = str((args or {}).get("path") or (args or {}).get("file_path") or "").strip()
    paths = [raw] if raw else []
    if name == "patch":
        paths.extend(_v4a_paths(args.get("patch")))
    return paths


def check(tool_name: str, args: dict[str, Any], hermes_home: str) -> Optional[str]:
    """Return the block message for this call, or ``None`` when it is allowed."""
    name = str(tool_name or "")
    if name not in FILE_TOOLS or not str(hermes_home or "").strip():
        return None
    home = _resolve(hermes_home)
    read_roots = (home, *PROJECT_ROOTS.get())
    args = args or {}
    if name in READ_TOOLS:
        targets = _paths_for(name, args)
        if not targets:
            if name != "search_files":
                return "minir file guard: path is required"
            # ``search_files`` defaults ``path`` to the cwd; resolve and judge that default.
            targets = ["."]
        for raw in targets:
            reason = _blocked_shape(raw)
            if reason:
                return f"minir file guard: {reason}"
            path = _resolve(raw)
            if not any(_within(path, root) for root in read_roots):
                return "minir file guard: reads are limited to HERMES_HOME or the project directory"
            if any(part.lower() == ".git" for part in path.parts):
                return "minir file guard: repository metadata is not readable"
            leaf = path.name.lower()
            if _within(path, home):
                # HERMES_HOME owns the host config and credentials; project config.yaml is
                # ordinary source material and stays readable.
                denied = (leaf in DENIED_READ_NAMES or leaf.startswith(".env.")
                          or leaf.startswith(DENIED_READ_PREFIXES))
            else:
                denied = leaf.startswith(".env") or leaf.startswith(DENIED_READ_PREFIXES)
            if denied:
                return "minir file guard: that file is not readable by a plugin agent"
        return None
    targets = _paths_for(name, args)
    if not targets:
        return "minir file guard: path is required"
    store = _resolve(home / STORE_DIRNAME)
    for raw in targets:
        reason = _blocked_shape(raw)
        if reason:
            return f"minir file guard: {reason}"
        path = _resolve(raw)
        if not _within(path, store):
            return "minir file guard: writes are limited to minir-memory/scratch"
        parts = _relative_parts(path, store)
        if not parts or parts[0] != SCRATCH_DIRNAME:
            return "minir file guard: only minir-memory/scratch is writable"
        if any(part.lower().startswith("minir-memory.db") for part in parts):
            return "minir file guard: the memory database is not writable"
    return None
