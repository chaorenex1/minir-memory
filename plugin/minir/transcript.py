"""The session's messages: a bounded in-memory window, plus an optional audit trail on disk.

This provider stores no conversation content as *state* (see ``store.py``): the completed
turns live in memory for the length of the process, and the layers reason over windows of them.
Two consumers need two different guarantees out of that memory:

* **The window** (``add``/``window``) is what ``worker`` reads. It is bounded by count and by
  characters per session, and only the newest sessions are kept at all; losing the *old* end of
  an over-long window is the deliberate trade for not keeping a transcript in the database.
* **The audit trail** (``persist_dir``) is what an operator, or the 3-day review, reads
  *afterwards*: one JSON file per session, so a run can still be inspected once the process is
  gone. It is an audit trail, never a source of truth — ``state.db`` is — so a failed write is
  dropped rather than retried forever, and none of the layers ever read these files.

Why the trail is shaped the way it is:

* **Off the turn's thread.** ``add`` runs inside ``sync_turn`` on the host's turn path, so it
  only appends to a bounded queue (microseconds, no IO). One writer thread owns every file
  operation; a full queue drops its oldest entry and counts it, because an audit trail must
  never stall a conversation.
* **Debounced.** Rewriting a 2000-message file on every message would be megabytes of IO for
  nothing. The writer waits for 30 seconds or 50 new messages, whichever comes first; a
  boundary (session end, pre-compress, shutdown) calls ``flush`` and ends the wait immediately.
* **Atomic.** Every rewrite goes through a temp file and ``os.replace``, so a reader never
  sees half a session, including after a crash.
* **Sharded, not truncated.** One file holds 2000 messages; the overflow goes to
  ``<session_id>.part2.json`` in the same directory, so growing past one file never rewrites
  the content already on disk. Only past 4000 messages does the oldest end slide off.
* **Retried, then dropped.** A failed write is retried twice; a batch that still fails is
  dropped and counted. A full disk must not turn into a stuck writer.

``record_aux`` is the second source: a plugin-layer run (base / dialectic / card_review) or a
custom ``minir_agents`` agent has no host turn to attach to, so the caller hands over the finished
``AgentRun.messages`` and the route that produced them. Those files live under
``persist_dir/<layer>/<session_id>.json``; a custom agent uses its name as the layer.

Timestamps are epoch seconds — the same unit as ``messages[].created_at`` and
``history.messages.timestamp`` — so a file correlates with ``state.db`` without a conversion.
Retention is 30 days: ``prune`` deletes expired files and is the only deleter (the model's tools
never reach this directory).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 400          # per session
_MAX_CHARS = 200_000         # per session, across kept messages
_MAX_MESSAGE_CHARS = 4_000   # one message (long pastes must not own the whole budget)
_MAX_SESSIONS = 8            # long-lived processes open many sessions; only recent ones matter
_EPSILON = 1e-6              # keeps a message strictly inside the window that starts at the edge

# -- persistence (doc/base-dialectic-layers-draft.md, 4.9) ---------------------
DEBOUNCE_SECONDS = 30.0      # rewrite a session at most this often ...
DEBOUNCE_MESSAGES = 50       # ... unless this many messages arrived first
MESSAGES_PER_FILE = 2_000    # one JSON file; the overflow goes to <session>.part2.json
QUEUE_MAX = 2_000            # pending enqueues; a full queue drops its oldest entry
WRITE_RETRIES = 2            # attempts after the first failure, then the batch is dropped
DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60
HOST_LAYER = "host"          # the payload's layer for a host session (it has no layer folder)

#: Items drained per lock acquisition on the writer thread. The queue holds 2000, and a turn
#: must never wait on a full drain: enqueue takes the same lock.
_DRAIN_BATCH = 512

#: Everything a file name may keep: separators and drive colons are folded to ``_``. Session
#: ids reach this from the host, so they are untrusted input for a path.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _file_stem(value: str, fallback: str) -> str:
    """A safe file-name stem: no separators, no leading dot, never empty and never ``..``."""
    stem = _UNSAFE_NAME.sub("_", str(value or "").strip()).strip("._")
    return stem or fallback


def _flatten(text: str) -> str:
    return " ".join((text or "").split())


def _stamp(value: Any, fallback: float) -> float:
    """A numeric timestamp, or *fallback* — ``0.0`` is a stamp, so no truthiness test."""
    return float(value) if isinstance(value, (int, float)) else float(fallback)


class _Record:
    """One session's trail, as the writer thread knows it (guarded by the buffer's cvar).

    ``messages`` grows independently of the in-memory window: the files keep 4000 messages (two
    shards), the window keeps 400, and neither truncation leaks into the other.
    """

    __slots__ = ("session_id", "layer", "folder", "stem", "messages", "model", "provider",
                 "toolsets", "system_message", "usage", "started_at", "first_pending_at",
                 "pending", "last_written", "writing")

    def __init__(self, *, session_id: str, layer: str, folder: str, stem: str,
                 started_at: float) -> None:
        self.session_id = session_id
        self.layer = layer
        self.folder = folder              # "" for a host session, else the layer directory
        self.stem = stem
        self.messages: list[dict[str, Any]] = []
        self.model = ""
        self.provider = ""
        self.toolsets: tuple[str, ...] = ()
        self.system_message = ""
        #: Token/cost counters the caller reported for this snapshot (aux runs only).
        self.usage: dict[str, Any] = {}
        self.started_at = started_at
        self.first_pending_at = 0.0
        self.pending = 0                  # messages added since the last write attempt
        self.last_written = 0.0
        self.writing = False              # a write is in flight (``prune`` must not delete it)


class TranscriptBuffer:
    """Thread-safe, bounded, in-memory window of each session's completed turns.

    Passing ``persist_dir`` additionally mirrors every session to a JSON file there, written
    asynchronously by one writer thread (see the module docstring for why). Without it the
    buffer is exactly what it was: memory only, no threads, no IO.
    """

    def __init__(self, *, max_messages: int = _MAX_MESSAGES, max_chars: int = _MAX_CHARS,
                 max_sessions: int = _MAX_SESSIONS,
                 persist_dir: Optional[os.PathLike[str] | str] = None,
                 debounce_seconds: float = DEBOUNCE_SECONDS,
                 debounce_messages: int = DEBOUNCE_MESSAGES,
                 queue_max: int = QUEUE_MAX, messages_per_file: int = MESSAGES_PER_FILE,
                 retries: int = WRITE_RETRIES):
        self._lock = threading.Lock()
        self._sessions: dict[str, deque[dict[str, Any]]] = {}
        self._chars: dict[str, int] = {}
        self._edges: dict[str, float] = {}   # newest window edge dispatched per session
        self._max_messages = max(1, int(max_messages))
        self._max_chars = max(1, int(max_chars))
        self._max_sessions = max(1, int(max_sessions))

        self._persist_dir = Path(persist_dir).expanduser() if persist_dir else None
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._debounce_messages = max(1, int(debounce_messages))
        self._queue_max = max(1, int(queue_max))
        self._messages_per_file = max(1, int(messages_per_file))
        self._retries = max(0, int(retries))
        # The writer's state has its own lock: ``add`` holds it for one append, so a slow disk
        # can never sit between a turn and its completion.
        self._cv = threading.Condition(threading.Lock())
        self._queue: deque[tuple[str, str, str, Any, Any]] = deque()
        self._records: dict[tuple[str, str], _Record] = {}
        self._flushes: list[tuple[Optional[tuple[str, str]], threading.Event]] = []
        self._closing = False
        self._closed = False
        self._dropped = 0
        self._errors = 0
        self._files_written = 0
        self._thread: Optional[threading.Thread] = None
        if self._persist_dir is not None:
            try:
                self._persist_dir.mkdir(parents=True, exist_ok=True)
            except OSError:  # a bad path must not break the provider that hosts this buffer
                logger.warning("transcript: cannot create persist_dir %s", self._persist_dir,
                               exc_info=True)
            self._thread = threading.Thread(target=self._writer_loop, name="minir-transcript",
                                            daemon=True)
            self._thread.start()

    # -- writes ---------------------------------------------------------------

    def mark_edge(self, session_id: str, ts: float) -> None:
        """Record a dispatched window's edge for the session.

        A message must never be timestamped *at* an edge: windows are half-open ``(from, to]``,
        so a message stamped exactly ``from`` would fall outside the window that starts there and
        outside the one that ended there. Clocks are coarser than the gap between an
        ``on_turn_start`` tick and the turn's arrival (Windows has been measured at ~15 ms), so
        this is a routine collision, not a theoretical one: the edge is remembered here and
        ``add`` keeps every later message strictly past it.
        """
        with self._lock:
            session = str(session_id or "")
            self._edges[session] = max(float(ts), self._edges.get(session, 0.0))

    def add(self, session_id: str, role: str, content: str, *, author: str = "",
            author_id: str = "", created_at: Optional[float] = None) -> None:
        """Append one message, evicting the oldest until both budgets hold.

        A single message is truncated to ``_MAX_MESSAGE_CHARS`` first: one oversized paste must
        not be able to empty the bucket (or blow the char budget on its own), and the layers only
        ever see a bounded window anyway. The audit trail mirrors the buffered message — guard
        included — and is queued under the same lock, so its order matches the window's.
        """
        body = _flatten(content)
        if not body:
            return
        if len(body) > _MAX_MESSAGE_CHARS:
            body = body[:_MAX_MESSAGE_CHARS] + "…"
        session = str(session_id or "")
        with self._lock:
            edge = self._edges.get(session, 0.0)
            bucket = self._sessions.get(session)
            last = bucket[-1]["created_at"] if bucket else 0.0
            stamp = time.time() if created_at is None else float(created_at)
            # Strictly past both the newest window edge and the newest buffered message: a stamp
            # that ties either one would fall outside every window that could contain it.
            entry = {"role": str(role or "user"), "content": body, "author": str(author or ""),
                     "author_id": str(author_id or ""),
                     "created_at": max(stamp, edge + _EPSILON, last + _EPSILON) if (edge or last) else stamp}
            if bucket is None:
                if len(self._sessions) >= self._max_sessions:
                    # An emptied bucket (a message larger than the whole budget) sorts first: it
                    # holds nothing worth keeping, and it must never be indexed.
                    oldest = min(self._sessions, key=lambda key: (
                        self._sessions[key][-1]["created_at"] if self._sessions[key] else 0.0))
                    self._sessions.pop(oldest, None)
                    self._chars.pop(oldest, None)
                    self._edges.pop(oldest, None)
                bucket = self._sessions[session] = deque()
            bucket.append(entry)
            self._chars[session] = self._chars.get(session, 0) + len(body)
            while bucket and (len(bucket) > self._max_messages
                              or self._chars[session] > self._max_chars):
                dropped = bucket.popleft()
                self._chars[session] -= len(dropped["content"])
            self._persist_message(session, entry)

    def add_turn(self, session_id: str, user_content: str, assistant_content: str, *,
                 author: str = "", author_id: str = "", created_at: Optional[float] = None) -> None:
        """Append one completed turn (the pair the host hands ``sync_turn``)."""
        self.add(session_id, "user", user_content, author=author, author_id=author_id,
                 created_at=created_at)
        self.add(session_id, "assistant", assistant_content, created_at=created_at)

    def record_aux(self, layer: str, session_id: str, messages: Iterable[dict[str, Any]], *,
                   model: str = "", provider: str = "", toolsets: Iterable[str] = (),
                   system_message: str = "", usage: Optional[Mapping[str, Any]] = None) -> None:
        """Queue one plugin-layer session for the audit trail.

        ``layer`` is ``base``, ``dialectic`` or ``card_review`` and becomes a directory under
        ``persist_dir``; ``session_id`` is the plugin's own id (``<job_id>-<attempt>`` or a
        uuid). The call is a *snapshot*: the messages given here replace whatever this session
        held before, because one layer run produces one transcript. Ids and the layer are folded
        to safe file names, so a hostile id cannot escape the directory.
        """
        if self._persist_dir is None:
            return
        folder = _file_stem(layer, "aux")
        rows = [dict(row) for row in (messages or ()) if isinstance(row, dict)]
        meta = (str(model or ""), str(provider or ""),
                tuple(str(tool) for tool in (toolsets or ())), str(system_message or ""),
                str(layer or folder), dict(usage or {}))
        with self._cv:
            if self._enqueue_locked(("snapshot", folder, str(session_id or ""), rows, meta)):
                self._cv.notify()

    def read_aux(self, layer: str, session_id: str) -> list[dict[str, Any]]:
        """Read one aux session's audit rows back (both shards, oldest first).

        The adviser runtime uses this to resume its own conversation after a process
        restart; the transcript layer still treats these files as audit data, so a missing
        or malformed file returns an empty list rather than raising.
        """
        if self._persist_dir is None:
            return []
        folder = self._persist_dir / _file_stem(layer or "aux", "aux")
        stem = _file_stem(str(session_id or ""), "session")
        rows: list[dict[str, Any]] = []
        for name in (f"{stem}.json", f"{stem}.part2.json"):
            try:
                payload = json.loads((folder / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            messages = payload.get("messages") if isinstance(payload, dict) else None
            if isinstance(messages, list):
                rows.extend(dict(row) for row in messages if isinstance(row, dict))
        return rows

    def append_text(self, layer: str, session_id: str, text: str,
                    *, suffix: str = ".log") -> None:
        """Append raw text to one aux file, unbounded.

        Used for the full reasoning stream: the in-memory excerpt is bounded for the prompt,
        but the on-disk copy must not drop the middle of a long thought. The caller batches a
        turn's deltas and calls this once, so this is not on the token path.
        """
        if self._persist_dir is None or not text:
            return
        folder = self._persist_dir / _file_stem(layer or "aux", "aux")
        path = folder / f"{_file_stem(str(session_id or ''), 'session')}{suffix}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            logger.debug("transcript: append_text failed for %s", path, exc_info=True)

    def clear(self, session_id: Optional[str] = None) -> None:
        """Drop one session's messages, or every session's.

        Deliberately not called on ``/new``: the session that just ended may still have a queued
        boundary job to drain, and emptying its bucket would hand that job an empty window. The
        caps bound what is kept until the process ends. The audit trail is untouched — it is
        history, and only ``prune`` deletes it.
        """
        with self._lock:
            if session_id is None:
                self._sessions.clear()
                self._chars.clear()
                self._edges.clear()
                return
            self._sessions.pop(str(session_id or ""), None)
            self._chars.pop(str(session_id or ""), None)
            self._edges.pop(str(session_id or ""), None)

    # -- persistence: public surface -------------------------------------------

    def flush(self, session_id: Optional[str] = None, *, timeout: float = 30.0) -> bool:
        """Write everything queued so far, now — the boundary flush.

        ``session_id=None`` flushes every session (what ``close`` uses); an id flushes that host
        session. The call waits for the writer to acknowledge, so on return the data is on disk
        (or the retries failed and the batch was dropped), and no debounce timer is left running.
        Returns ``False`` only when the writer did not acknowledge within ``timeout``.
        """
        if self._persist_dir is None:
            return True
        with self._cv:
            if self._closed or self._closing:
                return True
            key = None if session_id is None else ("", _file_stem(str(session_id), "session"))
            event = threading.Event()
            self._flushes.append((key, event))
            self._cv.notify_all()
        return event.wait(max(0.0, float(timeout)))

    def flush_aux(self, layer: str, session_id: str, *, timeout: float = 30.0) -> bool:
        """Write one plugin-layer snapshot now; keyed by ``(layer, session)``.

        ``flush(session_id)`` is the host-session key, so it would wait on nothing for an aux
        record. This is the same acknowledgement for the ``record_aux`` namespace, and the
        minir_agents tool uses it so the transcript is on disk before the tool returns.
        """
        if self._persist_dir is None:
            return True
        with self._cv:
            if self._closed or self._closing:
                return True
            key = (_file_stem(layer, "aux"), _file_stem(session_id, "session"))
            event = threading.Event()
            self._flushes.append((key, event))
            self._cv.notify_all()
        return event.wait(max(0.0, float(timeout)))

    def path_for_aux(self, layer: str, session_id: str) -> Optional[Path]:
        """The JSON path a layer snapshot lands on, or ``None`` when memory-only.

        Uses the same folding as ``record_aux``, so the returned path is the file the writer
        will actually produce, not a guess built from the raw names.
        """
        if self._persist_dir is None:
            return None
        folder = _file_stem(layer, "aux")
        stem = _file_stem(session_id, "session")
        return self._persist_dir / folder / f"{stem}.json"

    def close(self) -> None:
        """Flush what is queued, stop the writer and join it. Idempotent."""
        if self._persist_dir is None:
            return
        with self._cv:
            if self._closed:
                return
        self.flush()
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10.0)
            if thread.is_alive():
                logger.warning("transcript: writer thread still alive after close()")
        with self._cv:
            self._closed = True
        self._thread = None

    def prune(self, older_than_seconds: float = DEFAULT_RETENTION_SECONDS,
              now: Optional[float] = None) -> int:
        """Delete session files older than the retention window; return how many were removed.

        Default retention is 30 days. The 3-day review is the intended caller; the model's tools
        never delete these files. ``now`` is injectable so a test does not have to age files.
        """
        if self._persist_dir is None or not self._persist_dir.exists():
            return 0
        cutoff = (time.time() if now is None else float(now)) - max(0.0, float(older_than_seconds))
        removed = 0
        # The scan and the unlinks run *outside* the writer's lock: a 3-day cleanup must never
        # sit between a turn and its enqueue.
        with self._cv:
            busy = {self._path_for(record, 1) for record in self._records.values()
                    if record.pending or record.writing}
        for path in sorted(self._persist_dir.rglob("*.json")):
            try:
                if path in busy or path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                logger.debug("transcript: could not prune %s", path, exc_info=True)
        with self._cv:
            # Forget what was just deleted, or the next message would resurrect an expired file.
            for key, record in list(self._records.items()):
                if record.pending or record.writing:
                    continue
                with contextlib.suppress(OSError):
                    if not self._path_for(record, 1).exists():
                        self._records.pop(key, None)
        return removed

    @property
    def persist_dir(self) -> Optional[Path]:
        """The directory the audit trail is written under, or ``None`` when memory-only."""
        return self._persist_dir

    @property
    def dropped(self) -> int:
        """Messages discarded because the queue was full (or the buffer was already closing)."""
        with self._cv:
            return self._dropped

    @property
    def errors(self) -> int:
        """Batches dropped after every write attempt failed."""
        with self._cv:
            return self._errors

    @property
    def files_written(self) -> int:
        """JSON files successfully written (a session's main file and shard counted apart)."""
        with self._cv:
            return self._files_written

    # -- reads ----------------------------------------------------------------

    def window(self, session_id: str, from_ts: float, to_ts: float) -> list[dict[str, Any]]:
        """Messages of one session in the half-open window ``(from_ts, to_ts]``, oldest first."""
        with self._lock:
            bucket = list(self._sessions.get(str(session_id or ""), ()))
        return [dict(entry) for entry in bucket
                if float(from_ts) < entry["created_at"] <= float(to_ts)]

    def has_after(self, session_id: str, ts: float) -> bool:
        """True when the session holds a message newer than *ts* (the boundary drain's question)."""
        with self._lock:
            bucket = self._sessions.get(str(session_id or ""))
            return bool(bucket) and bucket[-1]["created_at"] > float(ts)

    def latest_ts(self, session_id: str) -> float:
        """Timestamp of the session's newest buffered message (0.0 when it holds none).

        The worker bounds the newest window's read with this, so draining the newest job always
        covers everything buffered — a stamp can sit a hair past the clock reading that a window
        would otherwise close on.
        """
        with self._lock:
            bucket = self._sessions.get(str(session_id or ""))
            return bucket[-1]["created_at"] if bucket else 0.0

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        """Every message kept for the session, oldest first."""
        with self._lock:
            return [dict(entry) for entry in self._sessions.get(str(session_id or ""), ())]

    def extend(self, session_id: str, entries: Iterable[dict[str, Any]]) -> None:
        """Append several entries that already carry ``role``/``content``/``created_at``."""
        for entry in entries:
            self.add(session_id, str(entry.get("role") or "user"), str(entry.get("content") or ""),
                     author=str(entry.get("author") or ""), author_id=str(entry.get("author_id") or ""),
                     created_at=entry.get("created_at"))

    # -- persistence: enqueue (called on the host's turn path; must stay cheap) ---

    def _persist_message(self, session_id: str, entry: dict[str, Any]) -> None:
        """Queue one host message. No IO, no unbounded growth, no blocking."""
        if self._persist_dir is None:
            return
        with self._cv:
            if self._enqueue_locked(("message", "", session_id, entry, None)):
                self._cv.notify()

    def _enqueue_locked(self, task: tuple[str, str, str, Any, Any]) -> bool:
        """Append one task, dropping the oldest when the queue is full. Caller notifies."""
        if self._closing or self._closed:
            self._dropped += 1
            return False
        if len(self._queue) >= self._queue_max:
            self._queue.popleft()
            self._dropped += 1
        self._queue.append(task)
        return True

    # -- persistence: writer thread -------------------------------------------

    def _writer_loop(self) -> None:
        """Own every file operation: drain, decide, write, acknowledge flushes."""
        while True:
            plan: list[_Record] = []
            requests: list[tuple[Optional[tuple[str, str]], threading.Event]] = []
            closing = False
            with self._cv:
                self._drain_locked(time.time(), full=bool(self._flushes) or self._closing)
                requests, self._flushes = self._flushes, []
                closing = self._closing
                for key, _event in requests:
                    if key is None:
                        plan = list(self._records.values())
                        break
                    record = self._records.get(key)
                    if record is not None and record not in plan:
                        plan.append(record)
                now = time.time()
                for record in self._records.values():
                    if record in plan:
                        continue
                    if not (record.pending or (record.messages and not record.last_written)):
                        continue
                    if closing or self._due_locked(record, now):
                        plan.append(record)
                if not plan:
                    for _key, event in requests:
                        event.set()
                    if closing:
                        return
                    if self._queue:      # arrived while we were deciding
                        continue
                    self._cv.wait(self._wait_locked())
                    continue
                for record in plan:
                    record.pending = 0
                    record.first_pending_at = 0.0
                    record.writing = True
            for record in plan:
                try:
                    self._write_record(record)
                except Exception:  # health: allow BLE001 -- the writer must outlive one bad batch
                    logger.exception("transcript: unexpected writer failure for %s", record.stem)
                    with self._cv:
                        self._errors += 1
            written_at = time.time()
            with self._cv:
                for record in plan:
                    record.writing = False
                    record.last_written = written_at
                for _key, event in requests:
                    event.set()
                if closing and not self._queue and not self._flushes:
                    return

    def _drain_locked(self, now: float, *, full: bool = False) -> None:
        """Move queued tasks into their records; the only place records are mutated."""
        limit = len(self._queue) if full else min(len(self._queue), _DRAIN_BATCH)
        for _ in range(limit):
            kind, folder, session_id, payload, meta = self._queue.popleft()
            try:
                if kind == "message":
                    record = self._record_locked(HOST_LAYER, folder, session_id,
                                                 started_at=_stamp(payload.get("created_at"), now))
                    record.messages.append(payload)
                    self._trim_locked(record)
                    self._pending_locked(record, 1, now)
                else:  # snapshot: one layer run replaces that session's transcript
                    model, provider, toolsets, system_message, layer, usage = meta
                    record = self._record_locked(layer, folder, session_id, started_at=now)
                    record.layer = layer
                    record.model = model
                    record.provider = provider
                    record.toolsets = toolsets
                    record.system_message = system_message
                    record.usage = usage
                    record.messages = payload
                    self._trim_locked(record)
                    record.pending = 0
                    self._pending_locked(record, len(record.messages), now)
            except Exception:  # health: allow BLE001 -- one bad payload must not kill the writer
                logger.exception("transcript: could not queue %r for %s", kind, session_id)
                self._errors += 1

    def _record_locked(self, layer: str, folder: str, session_id: str, *,
                       started_at: float) -> _Record:
        key = (folder, _file_stem(session_id, "session"))
        record = self._records.get(key)
        if record is None:
            record = self._records[key] = _Record(session_id=str(session_id or ""), layer=layer,
                                                  folder=folder, stem=key[1],
                                                  started_at=float(started_at))
        return record

    def _trim_locked(self, record: _Record) -> None:
        """Keep at most two shards; only past 4000 messages does content leave the files."""
        limit = self._messages_per_file * 2
        if len(record.messages) > limit:
            del record.messages[:len(record.messages) - limit]

    def _pending_locked(self, record: _Record, count: int, now: float) -> None:
        if count and not record.pending:
            record.first_pending_at = now
        record.pending += count

    def _due_locked(self, record: _Record, now: float) -> bool:
        if not record.pending:
            return False
        if record.pending >= self._debounce_messages:
            return True
        return now >= max(record.first_pending_at, record.last_written) + self._debounce_seconds

    def _wait_locked(self) -> Optional[float]:
        """How long the writer may sleep: the nearest debounce deadline, or forever."""
        now = time.time()
        deadlines = [max(record.first_pending_at, record.last_written) + self._debounce_seconds - now
                     for record in self._records.values() if record.pending]
        return max(0.01, min(deadlines)) if deadlines else None

    def _path_for(self, record: _Record, part: int) -> Path:
        assert self._persist_dir is not None
        folder = self._persist_dir / record.folder if record.folder else self._persist_dir
        name = f"{record.stem}.json" if part == 1 else f"{record.stem}.part{part}.json"
        return folder / name

    def _write_record(self, record: _Record) -> None:
        """Rewrite one session: the main file plus, past the limit, the second shard."""
        messages = list(record.messages)
        if not messages:
            return
        newest = messages[-1].get("created_at")
        base = {
            "session_id": record.session_id,
            "layer": record.layer,
            "started_at": record.started_at,
            "updated_at": float(newest) if isinstance(newest, (int, float)) else time.time(),
            "model": record.model,
            "provider": record.provider,
            "toolsets": list(record.toolsets),
            "system_message": record.system_message,
            "usage": record.usage,
        }
        written = 0
        for part in (1, 2):
            chunk = messages[(part - 1) * self._messages_per_file:part * self._messages_per_file]
            path = self._path_for(record, part)
            if not chunk:
                # A snapshot can be shorter than an earlier one (a re-run of the same layer
                # session); a stale shard from the longer write must not outlive it.
                if part > 1 and path.exists():
                    with contextlib.suppress(OSError):
                        path.unlink()
                continue
            if self._write_atomic(path, {**base, "messages": chunk}):
                written += 1
        if written:
            with self._cv:
                self._files_written += written

    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> bool:
        """temp + ``os.replace``, retried ``self._retries`` times, then dropped and counted."""
        for attempt in range(self._retries + 1):
            temp_name = ""
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                                     dir=str(path.parent))
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, default=str)
                os.replace(temp_name, path)
                return True
            except (OSError, TypeError, ValueError):
                if temp_name:
                    with contextlib.suppress(OSError):
                        os.unlink(temp_name)
                if attempt < self._retries:
                    time.sleep(0.05 * (attempt + 1))  # writer thread only; never a turn
                    continue
                logger.error("transcript: could not write %s after %d attempts",
                             path, attempt + 1, exc_info=True)
        with self._cv:
            self._errors += 1
        return False
