"""Path guard for the layer agents' file tools.

The base, dialectic, review and knowledge passes can read their evidence and the durable card
files, and nothing else. The host's file tools are not scoped, so the scope is applied here, at
the ``pre_tool_call`` gate, and only while a layer pass is running:

* a ContextVar marks the thread (and the worker threads it spawns) as inside a layer pass; a
  normal turn is never marked, so the guard cannot block the user's own tools;
* reads stay under ``$HERMES_HOME``, minus the secret-bearing files;
* writes stay under ``$HERMES_HOME/minir-memory/scratch``. Cards, knowledge, transcripts and
  rule files are written by code paths only: if the model could write the identity file and
  then cite it, the identity grounding check would prove nothing.

Resolution is ``expanduser`` -> ``realpath`` -> ``normcase`` -> ``commonpath`` against the root,
never a string prefix (``C:\\x\\minir-memory-2`` starts with ``C:\\x\\minir-memory``).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional

AUX_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar("minir_aux_scope",
                                                                default=False)

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


def aux_active() -> bool:
    """True when this call runs inside a layer pass."""
    return bool(AUX_SCOPE.get())


@contextlib.contextmanager
def aux_scope() -> Iterator[None]:
    """Mark the current context (and copies made by tool threads) as a layer pass."""
    token = AUX_SCOPE.set(True)
    try:
        yield
    finally:
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
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:  # different drives
        return False


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return ()


def check(tool_name: str, args: dict[str, Any], hermes_home: str) -> Optional[str]:
    """Return the block message for this call, or ``None`` when it is allowed."""
    name = str(tool_name or "")
    if name not in FILE_TOOLS or not str(hermes_home or "").strip():
        return None
    raw = (args or {}).get("path") or (args or {}).get("file_path") or ""
    reason = _blocked_shape(str(raw))
    if reason:
        return f"minir file guard: {reason}"
    path = _resolve(raw)
    home = _resolve(hermes_home)
    if name in READ_TOOLS:
        if not _within(path, home):
            return "minir file guard: reads are limited to HERMES_HOME"
        leaf = path.name.lower()
        if leaf in DENIED_READ_NAMES or leaf.startswith(DENIED_READ_PREFIXES):
            return "minir file guard: that file is not readable by a layer pass"
        return None
    store = _resolve(home / STORE_DIRNAME)
    if not _within(path, store):
        return "minir file guard: writes are limited to minir-memory/scratch"
    parts = _relative_parts(path, store)
    if not parts or parts[0] != SCRATCH_DIRNAME:
        return "minir file guard: only minir-memory/scratch is writable"
    if any(part.lower().startswith("minir-memory.db") for part in parts):
        return "minir file guard: the memory database is not writable"
    return None
