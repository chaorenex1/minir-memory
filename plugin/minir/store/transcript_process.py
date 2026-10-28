"""One lock per host process: who is first after a shutdown, who is last at exit.

The transcript directory is shared by every process that opens the profile (CLI, TUI, desktop,
gateway). The disk is the only shared truth; each process caches in memory. Two transitions need
process-wide coordination:

* the first process after a clean shutdown replays each stream's ``last_cache`` window into its
  cache;
* the last process to close finalises every stream once more.

Both are decided with try-acquire probes over ``run/<pid>-<rand>.lock`` files, never by reading a
pid: a crashed process releases its OS lock at teardown, a stale file stays on disk. The file is
one byte because Windows cannot lock past EOF. A lock this process cannot acquire belongs to a
live process; one it can acquire is stale and is deleted.

Both transitions can race (two starters both see "alone"). The callers make replay and finalise
idempotent; no distributed lock is added.
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:  # Windows
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
except ImportError:  # POSIX
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open_locked(path: Path) -> Optional[int]:
    """Create/lock *path*; ``None`` when another process holds it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        logger.debug("transcript: cannot open lock %s", path, exc_info=True)
        return None
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        _lock(fd)
        return fd
    except OSError:
        os.close(fd)
        return None


class ProcessLock:
    """This process's own lock, plus the first/last verdicts over its siblings."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self._path = self.run_dir / f"{os.getpid()}-{random.randrange(1 << 24):06x}.lock"
        self._fd: Optional[int] = None
        self.first = False
        self.last = False

    def start(self) -> bool:
        """Create and hold this process's lock; True when no other process is live."""
        fd = _open_locked(self._path)
        if fd is None:
            # Two starts in one pid share a name; retry once with a fresh suffix.
            self._path = self.run_dir / f"{os.getpid()}-{random.randrange(1 << 24):06x}.lock"
            fd = _open_locked(self._path)
        self._fd = fd
        self.first = self._alone()
        return self.first

    def _alone(self) -> bool:
        """Probe every other lock: all stale/absent means this process is alone."""
        alone = True
        try:
            paths = sorted(self.run_dir.glob("*.lock"))
        except OSError:
            return False
        for path in paths:
            if path == self._path or path.name.startswith("stream-"):
                continue  # stream locks are short-lived write locks, not process liveness
            fd = _open_locked(path)
            if fd is None:
                alone = False
                continue
            try:  # stale: nobody holds it, so the owner died
                os.close(fd)
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("transcript: could not remove stale lock %s", path, exc_info=True)
        return alone

    def finish(self) -> bool:
        """Release this lock and report whether no other process remains."""
        self._release()
        self.last = self._alone()
        return self.last

    def _release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            logger.debug("transcript: unlock failed", exc_info=True)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            logger.debug("transcript: could not remove own lock", exc_info=True)

    def release(self) -> None:
        self._release()


def new_stream_lock(path: Path) -> Path:
    """Return the lock path used to serialise ``seq`` allocation for one stream."""
    return path.with_suffix(path.suffix + ".lock")


#: A StreamLock is held for the length of one append (timeout 5s), so a file nobody touched
#: for this long belongs to a stream that will never be locked again.
LOCK_MAX_AGE = 24 * 3600.0


def prune_stale_locks(directory: Optional[Path], max_age: float = LOCK_MAX_AGE) -> int:
    """Remove lock files no writer can still hold. Returns how many were deleted.

    Deleting on release was rejected: on POSIX a waiting writer can already hold the inode, and
    a third writer would then create a fresh file and lock it in parallel. Age bounds the pile-up
    without touching a live lock.
    """
    if directory is None:
        return 0
    cutoff = time.time() - max(60.0, float(max_age))
    removed = 0
    try:
        candidates = list(directory.rglob("*.lock"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class StreamLock:
    """Best-effort exclusive lock over one stream file's seq/append region."""

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = max(0.0, float(timeout))
        self._fd: Optional[int] = None

    def __enter__(self) -> "StreamLock":
        deadline = time.time() + self.timeout
        while True:
            fd = _open_locked(self.path)
            if fd is not None:
                self._fd = fd
                return self
            if time.time() >= deadline:
                logger.debug("transcript: stream lock timeout on %s; proceeding", self.path)
                return self
            time.sleep(0.02)

    def __exit__(self, *_exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)
