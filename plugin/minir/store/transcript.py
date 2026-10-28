"""Every event of a session, one JSON object per line, appended and never rewritten.

The old transcript was an audit mirror: whole-file JSON rewritten under a lock, a 4000-message
cap, 30-day ``prune``, and a docstring promise that no layer read it. This module is the
reversal of that contract — the file is the record and the layers read it:

* **One event per line.** ``append_event`` writes the body as given: no ``_flatten``, no 4000
  character cut, no whitespace collapse. Tool results and code blocks survive.
* **Append-only, permanent.** No rewrite, no compression, no deletion. Parts rotate at
  ``EVENTS_PER_PART`` lines and are never removed; a read continues across parts by ``seq``.
* **A stream is ``profile/layer/session``.** ``<persist_dir>/<profile>/<layer>/<session>.jsonl``
  and ``.partN.jsonl`` for later parts.
* **Memory is a cache of the disk, not the reverse.** ``read_events`` is the single read entry
  point: memory first, this stream's own file for anything the cache no longer holds, and the
  result reports ``source`` and ``coverage`` so a caller can tell "no such row" from "not cached".
* **The cache range and the dedupe anchor live in the file.** A clean close appends
  ``marker{marker:"last_cache", from_seq, to_seq}`` to every stream this process wrote; the first
  process after a full shutdown replays that range. Every message row carries ``fp``; the newest
  one is the next ``sync_turn`` anchor, so a resume does not replay history. There is no sidecar.

``sync_turn`` hands the plugin the run's whole message list, so ingestion is a tail append after
the ``fp`` anchor; when the anchor is gone (compaction rewrote the conversation) a ``gap`` row is
written and the slice appended anyway. What the host committed lives in ``state.db``; this stream
exists for the buffer, for layer reasoning, and for agent history.

The in-memory window (``add``/``window``) is a separate, bounded projection used by the layer
worker's timestamp windows. It is not the event table and it keeps its 400-message/200k-character
budget; only its persistence half moved to JSONL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from . import transcript_process

logger = logging.getLogger(__name__)

# -- window (what the layer worker reads) --------------------------------------
MAX_MESSAGES = 400            # per session
MAX_CHARS = 200_000           # per session, across kept messages
MAX_MESSAGE_CHARS = 4_000     # one message in the *window* (the file keeps the full body)
_EPSILON = 1e-6

# -- event store ---------------------------------------------------------------
BUFFER_STREAMS = 20           # streams kept in memory (the old _MAX_SESSIONS was 8)
BUFFER_EVENTS_PER_STREAM = 4_000
BUFFER_BYTES = 256 * 1024 * 1024
EVENTS_PER_PART = 2_000
QUEUE_MAX = 8_000

HOST_LAYER = "host"
ROLES = ("user", "system", "assistant", "tool", "reasoning")
KINDS = ("msg", "aux", "run_start", "run_end", "gap", "marker")

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_PART_RE = re.compile(r"^(?P<stem>.+)\.part(?P<part>\d+)\.jsonl$")


def _file_stem(value: str, fallback: str) -> str:
    """A safe path component: no separators, no leading dot, never empty and never ``..``."""
    stem = _UNSAFE_NAME.sub("_", str(value or "").strip()).strip("._")
    return stem or fallback


def _flatten(text: str) -> str:
    """Collapse whitespace for the *window* only; the event body is never flattened."""
    return " ".join((text or "").split())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _row_stamp(row: Mapping[str, Any]) -> Optional[float]:
    """The message's own epoch stamp, when the host slice carries one."""
    for key in ("timestamp", "created_at"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _text_of(content: Any) -> str:
    """Host message content -> text. Lists of parts are joined; nothing is flattened."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                parts.append(str(text) if text is not None
                             else json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _tool_calls(value: Any) -> list[dict[str, Any]]:
    """Normalise the two tool-call shapes (``{id,name,args}`` / ``{id,function:{...}}``)."""
    calls: list[dict[str, Any]] = []
    for item in value or ():
        if not isinstance(item, Mapping):
            continue
        function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        args = item.get("args", function.get("arguments", ""))
        calls.append({"id": str(item.get("id") or ""),
                      "name": str(item.get("name") or function.get("name") or ""),
                      "args": args if isinstance(args, str)
                      else json.dumps(args, ensure_ascii=False)})
    return calls


def fingerprint(role: str, text: str, *, tool_call_id: str = "", name: str = "") -> str:
    """Version-prefixed content identity: the dedupe anchor and the resume ``last_seen``."""
    digest = hashlib.sha256()
    for part in (role, tool_call_id, name, text):
        digest.update(str(part or "").encode("utf-8", "replace"))
        digest.update(b"\x00")
    return "v1:" + digest.hexdigest()[:32]


def _disk_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse one JSONL part; a truncated final line is counted, never raised."""
    rows: list[dict[str, Any]] = []
    broken = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    broken += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        logger.debug("transcript: cannot read %s", path, exc_info=True)
    return rows, broken


def _tail_rows(path: Path, count: int) -> list[dict[str, Any]]:
    """Last *count* parseable rows of one part, newest first, without loading the file."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    block = 64 * 1024
    data = b""
    offset = size
    try:
        with open(path, "rb") as handle:
            while offset > 0 and data.count(b"\n") <= count:
                step = min(block, offset)
                offset -= step
                handle.seek(offset)
                data = handle.read(step) + data
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(data.splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
        if len(rows) >= count:
            break
    return rows


def _count_lines(path: Path) -> int:
    total = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += chunk.count(b"\n")
    except OSError:
        return 0
    return total


class _Stream:
    """One ``profile/layer/session`` stream, as this process caches it."""

    __slots__ = ("profile", "layer", "session_id", "rows", "chars", "size", "last_seq",
                 "turn", "step", "last_seen", "evicted_before", "updated_at", "lock", "loaded")

    def __init__(self, profile: str, layer: str, session_id: str, lock: Any = None) -> None:
        self.profile = profile
        self.layer = layer
        self.session_id = session_id
        self.rows: deque[dict[str, Any]] = deque()
        self.chars = 0
        self.size = 0
        self.last_seq = 0
        self.turn = 0
        self.step = 0
        self.last_seen: Optional[dict[str, Any]] = None
        self.evicted_before = 0
        self.updated_at = time.time()
        #: One process-wide RLock: seq allocation, cache mutation and eviction share it, so a
        #: row cannot be cached while another thread evicts the stream it belongs to.
        self.lock = lock if lock is not None else threading.RLock()
        self.loaded = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.profile, self.layer, self.session_id)

    def coverage(self) -> tuple[int, int]:
        if not self.rows:
            return (0, 0)
        return (int(self.rows[0].get("seq") or 0), int(self.rows[-1].get("seq") or 0))


class TranscriptBuffer:
    """The session's event store plus its bounded in-memory window."""

    def __init__(self, *, max_messages: int = MAX_MESSAGES, max_chars: int = MAX_CHARS,
                 max_sessions: int = BUFFER_STREAMS,
                 persist_dir: Optional[os.PathLike[str] | str] = None,
                 run_dir: Optional[os.PathLike[str] | str] = None,
                 profile: str = "default",
                 buffer_events_per_stream: int = BUFFER_EVENTS_PER_STREAM,
                 buffer_bytes: int = BUFFER_BYTES,
                 events_per_part: int = EVENTS_PER_PART,
                 queue_max: int = QUEUE_MAX):
        self._lock = threading.RLock()
        self._cv = threading.Condition(threading.RLock())
        self._sessions: dict[str, deque[dict[str, Any]]] = {}
        self._chars: dict[str, int] = {}
        self._edges: dict[str, float] = {}
        self._max_messages = max(1, int(max_messages))
        self._max_chars = max(1, int(max_chars))
        self._max_sessions = max(1, int(max_sessions))
        self._profile = str(profile or "default")
        self._events_per_stream = max(1, int(buffer_events_per_stream))
        self._buffer_bytes = max(1, int(buffer_bytes))
        self._events_per_part = max(1, int(events_per_part))
        self._queue_max = max(1, int(queue_max))

        self._persist_dir = Path(persist_dir).expanduser() if persist_dir else None
        self._run_dir = (Path(run_dir).expanduser() if run_dir else
                         (self._persist_dir.parent / "run" if self._persist_dir else None))
        self._streams: dict[tuple[str, str, str], _Stream] = {}
        self._parts: dict[tuple[str, str, str], tuple[int, int]] = {}
        self._written: dict[tuple[str, str, str], list[int]] = {}
        self._pinned_session = ""
        self._pinned_layer = ""
        self._queue: deque[tuple[tuple[str, str, str], list[dict[str, Any]]]] = deque()
        self._writing = 0
        self._dropped = 0
        self._errors = 0
        self._files_written = 0
        self._total_bytes = 0
        self._closing = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[transcript_process.ProcessLock] = None

        if self._persist_dir is not None:
            try:
                self._persist_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("transcript: cannot create persist_dir %s", self._persist_dir,
                               exc_info=True)
            self._process = transcript_process.ProcessLock(self._run_dir)
            try:
                removed = transcript_process.prune_stale_locks(self._persist_dir)
                if removed:
                    logger.info("transcript: removed %d stale stream lock(s)", removed)
            except Exception:  # hygiene must never keep a writer from starting
                logger.debug("transcript: stale lock prune failed", exc_info=True)
            self._thread = threading.Thread(target=self._writer_loop, name="minir-transcript",
                                            daemon=True)
            self._thread.start()

    # -- lifecycle -------------------------------------------------------------

    def bind(self, session_id: str, *, layer: str = HOST_LAYER, profile: str = "") -> bool:
        """Pin the live session/layer and, when this is the first process, replay the cache.

        Returns True when a rebuild ran. The pin keeps the current session and the caller's layer
        out of the LRU; the replay is bounded by stream count, events and bytes.
        """
        if str(profile or "").strip():
            self._profile = str(profile).strip()
        self._pinned_session = str(session_id or "")
        self._pinned_layer = str(layer or "")
        if self._process is None:
            return False
        first = self._process.start()
        if first:
            self.rebuild()
        return first

    def rebuild(self) -> int:
        """Replay each stream's newest ``last_cache`` range into this process's cache."""
        if self._persist_dir is None or not self._persist_dir.exists():
            return 0
        newest: dict[tuple[str, str, str], tuple[float, Path]] = {}
        try:
            for path in self._persist_dir.rglob("*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                parts = path.relative_to(self._persist_dir).parts
                if len(parts) < 3:
                    continue
                profile, layer = parts[-3], parts[-2]
                stem = path.name[:-6] if path.name.endswith(".jsonl") else path.name
                match = _PART_RE.match(path.name)
                if match:
                    stem = match.group("stem")
                key = (profile, layer, stem)
                if key not in newest or mtime > newest[key][0]:
                    newest[key] = (mtime, path)
        except OSError:
            return 0
        streams = sorted(newest.items(), key=lambda item: item[1][0], reverse=True)
        pinned = (self._profile, self._pinned_layer or HOST_LAYER, self._pinned_session)
        ordered = [item for item in streams if item[0] == pinned] + \
                  [item for item in streams if item[0] != pinned]
        loaded = 0
        for (profile, layer, stem), _info in ordered:
            if loaded >= self._max_sessions or (loaded and self._total_bytes >= self._buffer_bytes):
                break
            stream = self._stream(profile, layer, stem)
            self._replay_marker(stream)
            loaded += 1
        return loaded

    def _replay_marker(self, stream: _Stream) -> None:
        """Load ``[from_seq, to_seq]`` of the newest marker into the cache, holes respected."""
        marker: Optional[dict[str, Any]] = None
        for path in reversed(self._parts_for(stream)):
            for row in _tail_rows(path, 200):  # newest first
                if row.get("kind") == "marker" and row.get("marker") == "last_cache":
                    marker = row
                    break
            if marker is not None:
                break
        if marker is None:
            return
        from_seq = int(marker.get("from_seq") or 0)
        to_seq = int(marker.get("to_seq") or 0)
        if to_seq < from_seq:
            return
        rows: list[dict[str, Any]] = []
        for path in self._parts_for(stream):
            part_rows, _broken = _disk_rows(path)
            rows.extend(row for row in part_rows
                        if isinstance(row.get("seq"), int) and from_seq <= row["seq"] <= to_seq)
        if not rows:
            return
        rows.sort(key=lambda row: int(row.get("seq") or 0))
        if [int(row["seq"]) for row in rows] != list(range(from_seq, to_seq + 1)):
            return  # a hole: the cache would lie about contiguity, so read the disk instead
        with stream.lock:
            if rows and stream.rows and stream.rows[0]["seq"] == rows[0]["seq"]:
                return  # idempotent: another starter replayed the same range
            for row in rows:
                self._cache_row(stream, row)
            stream.evicted_before = max(0, from_seq - 1)
            # ``to_seq`` is the cache range, not the file's last row: the marker itself (and any
            # row written after it) still owns higher seq numbers. Reusing one would collide.
            files = self._parts_for(stream)
            file_max = max((int(row.get("seq") or 0)
                            for row in _tail_rows(files[-1], 200)), default=0) if files else 0
            stream.last_seq = max(stream.last_seq, to_seq, file_max)
            stream.loaded = True
            self._update_seen(stream, rows)

    # -- event writes ----------------------------------------------------------

    def configure_profile(self, profile: str) -> None:
        self._profile = str(profile or "default")

    @property
    def profile(self) -> str:
        return self._profile

    def append_event(self, layer: str, session_id: str, role: str, *, kind: str = "msg",
                     text: str = "", profile: str = "", turn: Optional[int] = None,
                     step: Optional[int] = None, run_id: str = "", redacted: bool = True,
                     ts: Optional[float] = None, **fields: Any) -> dict[str, Any]:
        """Append one full-fidelity event row; returns the row as written.

        ``text`` is stored verbatim. Role-specific fields (``tool_calls``, ``result``, ``fp``,
        ...) ride through ``fields``. A ``marker``/``gap`` row has no role.
        """
        stream = self._stream(profile or self._profile, str(layer or HOST_LAYER),
                              str(session_id or ""))
        with stream.lock:
            self._ensure_loaded(stream)
            stream.last_seq += 1
            if turn is None:
                turn = stream.turn + (1 if role == "user" else 0)
            if step is None:
                step = 0 if role == "user" else stream.step + (1 if role == "assistant" else 0)
            turn, step = int(turn), int(step)
            stream.turn, stream.step = max(stream.turn, turn), step
            now = float(ts) if isinstance(ts, (int, float)) else time.time()
            row: dict[str, Any] = {
                "v": 1, "seq": stream.last_seq, "ts": now, "iso": _iso(now),
                "profile": stream.profile, "layer": stream.layer,
                "session_id": stream.session_id, "turn": turn, "step": step,
                "role": str(role or ""), "kind": str(kind or "msg"), "run_id": str(run_id or ""),
                "redacted": bool(redacted),
            }
            if text:
                row["text"] = text
            for name, value in fields.items():
                if value is not None:
                    row[name] = value
            self._cache_row(stream, row)
            low, high = self._written.setdefault(stream.key, [row["seq"], row["seq"]])
            self._written[stream.key] = [min(low, row["seq"]), max(high, row["seq"])]
            self._enqueue(stream, [row])
        return row

    def ingest(self, layer: str, session_id: str, messages: Iterable[Mapping[str, Any]], *,
               profile: str = "", redacted: bool = True, run_id: str = "",
               source: str = "host") -> int:
        """Append the tail of a whole-conversation slice after the stream's ``fp`` anchor."""
        stream = self._stream(profile or self._profile, str(layer or HOST_LAYER),
                              str(session_id or ""))
        normalized = [dict(item) for item in (messages or ()) if isinstance(item, Mapping)]
        normalized = [row for row in normalized if str(row.get("role") or "") in ROLES]
        with stream.lock:
            self._ensure_loaded(stream)
            anchor = list((stream.last_seen or {}).get("fps") or ())
            if not anchor and stream.last_seen and stream.last_seen.get("fp"):
                anchor = [stream.last_seen["fp"]]
            fingerprints = [
                fingerprint(str(row.get("role") or ""), _text_of(row.get("content")),
                            tool_call_id=str(row.get("tool_call_id") or ""),
                            name=str(row.get("name") or ""))
                for row in normalized]
            start = None
            if anchor:
                start = self._anchor_offset(fingerprints, anchor)
            if start is None:
                # The in-memory anchor can be behind a newer writer: match against the tail
                # actually on disk before calling this a rewritten conversation.
                disk_anchor = self._disk_anchor(stream)
                if disk_anchor:
                    anchor = disk_anchor
                    start = self._anchor_offset(fingerprints, disk_anchor)
            dropped = 0
            if start is None and anchor:
                # Still no anchor. A whole-history replay would duplicate every message the
                # file already carries, so rows whose fingerprint the tail still holds are
                # dropped and the gap records how many.
                normalized, fingerprints, dropped = self._drop_seen_tail(
                    stream, normalized, fingerprints)
                self.append_event(layer, session_id, "", kind="gap", dropped=dropped,
                                  reason="compaction", profile=profile,
                                  redacted=redacted)
                start = 0
            else:
                start = int(start or 0)
            appended = 0
            for row in normalized[start:]:
                appended += self._append_message(stream, row, redacted=redacted,
                                                 run_id=run_id, source=source)
        return appended

    @staticmethod
    def _anchor_offset(fingerprints: list[str], anchor: list[str]) -> Optional[int]:
        """Where the new tail starts: after the last contiguous match of the anchor sequence.

        A single fingerprint cannot tell two identical messages apart; the last *sequence* can.
        The sequence is checked first, then the last single fingerprint as a weaker fallback
        (an older writer, or a conversation whose earlier pair was edited).
        """
        size = len(anchor)
        if size and len(fingerprints) >= size:
            for index in range(len(fingerprints) - size, -1, -1):
                if fingerprints[index:index + size] == anchor:
                    return index + size
        if anchor:
            for index in range(len(fingerprints) - 1, -1, -1):
                if fingerprints[index] == anchor[-1]:
                    return index + 1
        return None

    def _disk_anchor(self, stream: "_Stream", size: int = 8, scan: int = 800) -> list[str]:
        """The newest message fingerprints on disk; the anchor to trust after a lost one."""
        files = self._parts_for(stream)
        if not files:
            return []
        # _tail_rows is newest-first; an anchor has to read oldest-to-newest.
        fps = [str(row.get("fp") or "") for row in reversed(_tail_rows(files[-1], scan))
               if str(row.get("kind") or "") == "msg" and row.get("fp")]
        return fps[-size:] if fps else []

    def _drop_seen_tail(self, stream: "_Stream",
                        rows: list[dict[str, Any]], fingerprints: list[str],
                        scan: int = 800) -> tuple[list[dict[str, Any]], list[str], int]:
        """Drop incoming rows the newest part already carries (multiset match on ``fp``)."""
        files = self._parts_for(stream)
        if not files:
            return rows, fingerprints, 0
        seen: Counter[str] = Counter(
            str(row["fp"]) for row in _tail_rows(files[-1], scan)
            if str(row.get("kind") or "") == "msg" and row.get("fp"))
        kept: list[dict[str, Any]] = []
        kept_fps: list[str] = []
        dropped = 0
        for row, fp in zip(rows, fingerprints):
            if seen[fp] > 0:
                seen[fp] -= 1
                dropped += 1
                continue
            kept.append(row)
            kept_fps.append(fp)
        return kept, kept_fps, dropped

    def _append_message(self, stream: _Stream, row: Mapping[str, Any], *, redacted: bool,
                        run_id: str, source: str) -> int:
        role = str(row.get("role") or "")
        text = _text_of(row.get("content"))
        tool_call_id = str(row.get("tool_call_id") or "")
        name = str(row.get("name") or "")
        fp = fingerprint(role, text, tool_call_id=tool_call_id, name=name)
        fields: dict[str, Any] = {"fp": fp, "source": source}
        if role == "user":
            fields["author"] = str(row.get("author") or row.get("author_name") or "")
        elif role == "system":
            fields["chars"] = len(text)
            fields["hash"] = fingerprint("system", text)[3:]
        elif role == "assistant":
            calls = _tool_calls(row.get("tool_calls"))
            if calls:
                fields["tool_calls"] = calls
            if isinstance(row.get("usage"), Mapping):
                fields["usage"] = dict(row["usage"])
            if row.get("model"):
                fields["model"] = str(row.get("model") or "")
        elif role == "tool":
            fields.update({"tool_call_id": tool_call_id, "name": name,
                           "ok": bool(row.get("ok", True)), "result": text})
            if row.get("duration_ms") is not None:
                fields["duration_ms"] = row.get("duration_ms")
            if row.get("error"):
                fields["error"] = str(row.get("error") or "")
            text = ""  # the full body is in ``result``
        self.append_event(stream.layer, stream.session_id, role, kind="msg", text=text,
                          profile=stream.profile, redacted=redacted, run_id=run_id,
                          ts=_row_stamp(row), **fields)
        previous = list((stream.last_seen or {}).get("fps") or ())
        previous.append(fp)
        stream.last_seen = {"fp": fp, "role": role, "fps": previous[-8:], "ts": time.time()}
        reasoning = row.get("reasoning_content") or row.get("reasoning")
        if role == "assistant" and str(reasoning or "").strip():
            self.append_event(stream.layer, stream.session_id, "reasoning", kind="msg",
                              text=str(reasoning), profile=stream.profile,
                              redacted=redacted, run_id=run_id, ts=_row_stamp(row),
                              fp=fingerprint("reasoning", str(reasoning)),
                              model=str(row.get("model") or ""))
            return 2
        return 1

    def append_reasoning(self, layer: str, session_id: str, text: str, *, model: str = "",
                         profile: str = "", redacted: bool = True) -> Optional[dict[str, Any]]:
        """Append one assistant message's aggregated reasoning (never per delta)."""
        body = str(text or "")
        if not body.strip():
            return None
        return self.append_event(layer, session_id, "reasoning", text=body, profile=profile,
                                 redacted=redacted, model=str(model or ""),
                                 fp=fingerprint("reasoning", body))

    def record_aux(self, layer: str, session_id: str, messages: Iterable[dict[str, Any]], *,
                   model: str = "", provider: str = "", toolsets: Iterable[str] = (),
                   system_message: str = "", usage: Optional[Mapping[str, Any]] = None,
                   profile: str = "", run_id: str = "") -> None:
        """Append one plugin-layer conversation, deduped by ``fp`` (append-only, not a snapshot).

        The adviser passes its merged history on every review; only the tail after the last
        fingerprint is appended, so the file grows by what actually happened, not by the length
        of the prompt.
        """
        if self._persist_dir is None:
            return
        use_profile = profile or self._profile
        rows = [dict(row) for row in (messages or ()) if isinstance(row, dict)]
        run = run_id or f"run-{int(time.time() * 1000)}"
        system_text = str(system_message or "")
        stream = self._stream(use_profile, layer, session_id)
        with stream.lock:
            before = int(stream.last_seq)
            digest = fingerprint("system", system_text)[3:] if system_text.strip() else ""
            known = bool(digest) and any(
                str(row.get("hash") or "") == digest for row in stream.rows
                if str(row.get("role") or "") == "system")
            if digest and not known:
                files = self._parts_for(stream)
                if files:
                    known = any(str(row.get("hash") or "") == digest
                                for row in _tail_rows(files[-1], 200)
                                if str(row.get("role") or "") == "system")
            if digest and not known:
                # The system role has no host source here either: the run's system message is
                # the text it actually ran under. One row per distinct prompt.
                self.append_event(layer, session_id, "system", text=system_text,
                                  profile=use_profile, redacted=True, chars=len(system_text),
                                  hash=digest, fp=fingerprint("system", system_text))
        appended = self.ingest(layer, session_id, rows, profile=use_profile,
                               run_id=run, source="aux")
        if not appended:
            return
        self.append_event(layer, session_id, "", kind="run_end", profile=use_profile,
                          run_id=run, status="ok", usage=dict(usage or {}),
                          model=str(model or ""), provider=str(provider or ""),
                          toolsets=list(toolsets or ()), messages=appended,
                          prompt_hash=hashlib.sha256(
                              system_text.encode("utf-8", "replace")).hexdigest()[:32],
                          prompt_chars=len(system_text), from_seq=before)

    # -- window (unchanged contract) -------------------------------------------

    def mark_edge(self, session_id: str, ts: float) -> None:
        """Record a dispatched window's edge for the session."""
        with self._lock:
            session = str(session_id or "")
            self._edges[session] = max(float(ts), self._edges.get(session, 0.0))

    def add(self, session_id: str, role: str, content: str, *, author: str = "",
            author_id: str = "", created_at: Optional[float] = None) -> None:
        """Append one message to the bounded window (the file is written by ``append_event``)."""
        body = _flatten(content)
        if not body:
            return
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + "…"
        session = str(session_id or "")
        with self._lock:
            edge = self._edges.get(session, 0.0)
            bucket = self._sessions.get(session)
            last = bucket[-1]["created_at"] if bucket else 0.0
            stamp = time.time() if created_at is None else float(created_at)
            entry = {"role": str(role or "user"), "content": body, "author": str(author or ""),
                     "author_id": str(author_id or ""),
                     "created_at": max(stamp, edge + _EPSILON, last + _EPSILON)
                     if (edge or last) else stamp}
            if bucket is None:
                if len(self._sessions) >= self._max_sessions:
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

    def add_turn(self, session_id: str, user_content: str, assistant_content: str, *,
                 author: str = "", author_id: str = "", created_at: Optional[float] = None) -> None:
        """Append one completed turn to the window (the pair the host hands ``sync_turn``)."""
        self.add(session_id, "user", user_content, author=author, author_id=author_id,
                 created_at=created_at)
        self.add(session_id, "assistant", assistant_content, created_at=created_at)

    def clear(self, session_id: Optional[str] = None) -> None:
        """Drop one session's window, or every session's. The files are untouched."""
        with self._lock:
            if session_id is None:
                self._sessions.clear()
                self._chars.clear()
                self._edges.clear()
                return
            self._sessions.pop(str(session_id or ""), None)
            self._chars.pop(str(session_id or ""), None)
            self._edges.pop(str(session_id or ""), None)

    def window(self, session_id: str, from_ts: float, to_ts: float) -> list[dict[str, Any]]:
        """Messages of one session in the half-open window ``(from_ts, to_ts]``, oldest first."""
        with self._lock:
            bucket = list(self._sessions.get(str(session_id or ""), ()))
        return [dict(entry) for entry in bucket
                if float(from_ts) < entry["created_at"] <= float(to_ts)]

    def has_after(self, session_id: str, ts: float) -> bool:
        with self._lock:
            bucket = self._sessions.get(str(session_id or ""))
            return bool(bucket) and bucket[-1]["created_at"] > float(ts)

    def latest_ts(self, session_id: str) -> float:
        with self._lock:
            bucket = self._sessions.get(str(session_id or ""))
            return bucket[-1]["created_at"] if bucket else 0.0

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        """Every message kept in the window for the session, oldest first."""
        with self._lock:
            return [dict(entry) for entry in self._sessions.get(str(session_id or ""), ())]

    def extend(self, session_id: str, entries: Iterable[dict[str, Any]]) -> None:
        for entry in entries:
            self.add(session_id, str(entry.get("role") or "user"),
                     str(entry.get("content") or ""), author=str(entry.get("author") or ""),
                     author_id=str(entry.get("author_id") or ""),
                     created_at=entry.get("created_at"))

    # -- reads ---------------------------------------------------------------

    def read_events(self, profile: str, layer: str, session_id: str, *,
                    cursor: Optional[int] = None, last_n: Optional[int] = None,
                    since: Optional[float] = None, until: Optional[float] = None,
                    turn: Any = None, step: Any = None,
                    roles: Optional[Iterable[str]] = None,
                    kinds: Optional[Iterable[str]] = None,
                    limit: Optional[int] = None,
                    page: Optional[int] = None) -> dict[str, Any]:
        """One stream's events: memory first, this stream's own file for the rest.

        Disk rows are **streamed**: a part can hold multi-megabyte tool results, so the reader
        never materializes the whole file. It scans in ``seq`` order, applies the filters, keeps
        only the requested page or the ``last_n`` ring, and stops once the page is full and one
        more match proves ``truncated``.
        """
        stream = self._stream(profile, layer, session_id)
        with stream.lock:
            memory = sorted(stream.rows, key=lambda row: int(row.get("seq") or 0))
            coverage = stream.coverage()
            evicted = int(stream.evicted_before)
        need_disk = (not memory or limit is not None or page is not None
                     or (last_n is not None and last_n > len(memory))
                     or (cursor is not None and memory and cursor < int(memory[0]["seq"]) - 1)
                     or (since is not None and memory
                         and float(memory[0].get("ts") or 0.0) > float(since)))
        mem_index = {int(row.get("seq") or 0): row for row in memory}
        role_filter = tuple(str(role) for role in roles) if roles else None
        kind_filter = tuple(str(kind) for kind in kinds) if kinds else None
        page_size = max(1, int(limit)) if limit is not None else None
        offset = (max(0, int(page or 0)) * page_size) if page_size else (max(0, int(page or 0)))
        ring: Optional[deque[dict[str, Any]]] = (deque(maxlen=max(0, int(last_n)))
                                                 if last_n is not None else None)
        selected: list[dict[str, Any]] = []
        total = 0
        emitted: set[int] = set()
        stop = False
        more = False

        def consider(row: dict[str, Any]) -> bool:
            """Count and collect one row; returns False when the scan can stop."""
            nonlocal total
            if not self._matches(row, cursor=cursor, since=since, until=until, turn=turn,
                                 step=step, roles=role_filter, kinds=kind_filter):
                return True
            total += 1
            if ring is not None:
                ring.append(row)
                return True
            if total <= offset:
                return True
            if page_size is None or len(selected) < page_size:
                selected.append(row)
                return True
            return False  # one more match than the page: truncated, stop

        def take(row: dict[str, Any]) -> None:
            nonlocal stop, more
            if stop:
                return
            if not consider(row):
                stop = True
                more = True

        if need_disk:
            for row in self._iter_disk_rows(stream):
                seq = int(row.get("seq") or 0)
                emitted.add(seq)
                better = mem_index.get(seq)
                take(better if better is not None else row)
                if stop:
                    break
        if not stop:
            for row in memory:
                seq = int(row.get("seq") or 0)
                if seq not in emitted:
                    take(row)
                if stop:
                    break
        if ring is not None:
            selected = list(ring)
        dropped_before = evicted
        if last_n is not None and total > len(selected):
            dropped_before = max(evicted, int(selected[0]["seq"]) - 1 if selected else evicted)
        elif cursor is not None:
            dropped_before = max(evicted, int(cursor))
        truncated = bool(more)
        memory_seqs = set(mem_index)
        in_cache = sum(1 for row in selected if int(row.get("seq") or 0) in memory_seqs)
        source = ("buffer" if in_cache == len(selected) and selected else
                  "disk" if in_cache == 0 else "mixed")
        next_cursor = int(selected[-1]["seq"]) if selected else int(cursor or 0)
        return {"rows": selected, "next_cursor": next_cursor, "dropped_before": dropped_before,
                "coverage": coverage, "source": source, "truncated": truncated,
                "evicted_before": evicted}

    @staticmethod
    def _matches(row: dict[str, Any], *, cursor: Optional[int], since: Optional[float],
                 until: Optional[float], turn: Any, step: Any,
                 roles: Optional[tuple[str, ...]],
                 kinds: Optional[tuple[str, ...]]) -> bool:
        seq = int(row.get("seq") or 0)
        ts = float(row.get("ts") or 0.0)
        if cursor is not None and seq <= int(cursor):
            return False
        if since is not None and ts < float(since):
            return False
        if until is not None and ts >= float(until):
            return False
        if roles is not None and str(row.get("role") or "") not in roles:
            return False
        if kinds is not None and str(row.get("kind") or "") not in kinds:
            return False
        return _range_ok(row.get("turn"), turn) and _range_ok(row.get("step"), step)

    def _iter_disk_rows(self, stream: _Stream) -> Iterable[dict[str, Any]]:
        """Stream every part's JSONL rows in ``seq`` order, skipping malformed lines."""
        for path in self._parts_for(stream):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and isinstance(row.get("seq"), int):
                            yield row
            except OSError:
                logger.debug("transcript: cannot read %s", path, exc_info=True)

    def read_all(self, profile: str, layer: str, session_id: str) -> list[dict[str, Any]]:
        return self.read_events(profile, layer, session_id, limit=10 ** 9)["rows"]

    def stats(self, profile: Optional[str] = None, layer: Optional[str] = None) -> dict[str, Any]:
        """Per-stream counts: the in-memory view plus what the files hold."""
        with self._lock:
            streams = [{"profile": stream.profile, "layer": stream.layer,
                        "session_id": stream.session_id, "events": len(stream.rows),
                        "last_seq": stream.last_seq, "bytes": stream.size,
                        "evicted_before": stream.evicted_before,
                        "updated_at": stream.updated_at}
                       for stream in self._streams.values()
                       if (profile is None or stream.profile == profile)
                       and (layer is None or stream.layer == layer)]
        disk: dict[tuple[str, str], dict[str, int]] = {}
        if self._persist_dir is not None:
            try:
                for path in self._persist_dir.rglob("*.jsonl"):
                    parts = path.relative_to(self._persist_dir).parts
                    if len(parts) < 3:
                        continue
                    key = (parts[-3], parts[-2])
                    entry = disk.setdefault(key, {"events": 0, "bytes": 0})
                    entry["events"] += _count_lines(path)
                    entry["bytes"] += path.stat().st_size
            except OSError:
                pass
        return {"streams": streams,
                "count": len(streams),
                "bytes": sum(entry["bytes"] for entry in disk.values()),
                "disk": [{"profile": key[0], "layer": key[1], **value}
                         for key, value in sorted(disk.items())],
                "coverage": {stream["session_id"]: [stream["evicted_before"] + 1,
                                                    stream["last_seq"]]
                             for stream in streams}}

    def digest(self, profile: str, layer: str, session_id: str, **kwargs: Any) -> str:
        """Plain-text transcript digest for a prompt block (see ``transcript_prompt``)."""
        from ..prompting.transcript_prompt import render_digest

        return render_digest(self, profile, layer, session_id, **kwargs)

    def read_aux(self, layer: str, session_id: str) -> list[dict[str, Any]]:
        """Read one aux run's conversation back as host-shaped messages."""
        rows = self.read_all(self._profile, layer, session_id)
        messages: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("kind") or "") != "msg":
                continue
            role = str(row.get("role") or "")
            if role == "system":
                continue  # the run's system prompt, not a conversation turn (the host filters it)
            if role in {"user", "assistant"}:
                message: dict[str, Any] = {"role": role, "content": str(row.get("text") or "")}
                calls = row.get("tool_calls")
                if role == "assistant" and isinstance(calls, list) and calls:
                    message["tool_calls"] = [
                        {"id": str(call.get("id") or ""), "type": "function",
                         "function": {"name": str(call.get("name") or ""),
                                      "arguments": str(call.get("args") or "")}}
                        for call in calls if isinstance(call, Mapping)]
                if row.get("model"):
                    message["model"] = str(row.get("model") or "")
                messages.append(message)
            elif role == "tool":
                messages.append({"role": "tool", "tool_call_id": str(row.get("tool_call_id") or ""),
                                 "name": str(row.get("name") or ""),
                                 "content": str(row.get("result") or "")})
        return messages

    def path_for_aux(self, layer: str, session_id: str) -> Optional[Path]:
        """The JSONL path a layer stream lands on, or ``None`` when memory-only."""
        return self.path_for_stream(self._profile, layer, session_id)

    def path_for_stream(self, profile: str, layer: str, session_id: str) -> Optional[Path]:
        if self._persist_dir is None:
            return None
        folder = self._persist_dir / _file_stem(profile, "default") / _file_stem(layer, "aux")
        return folder / f"{_file_stem(session_id, 'session')}.jsonl"

    def append_text(self, layer: str, session_id: str, text: str,
                    *, suffix: str = ".log") -> None:
        """Append raw text to one stream file, unbounded (the reasoning log)."""
        if self._persist_dir is None or not text:
            return
        folder = self._persist_dir / _file_stem(self._profile, "default") / _file_stem(layer, "aux")
        path = folder / f"{_file_stem(str(session_id or ''), 'session')}{suffix}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            logger.debug("transcript: append_text failed for %s", path, exc_info=True)

    # -- flush / close ---------------------------------------------------------

    def flush(self, session_id: Optional[str] = None, *, timeout: float = 30.0) -> bool:
        """Wait until everything queued so far is on disk."""
        if self._persist_dir is None:
            return True
        deadline = time.time() + max(0.0, float(timeout))
        with self._cv:
            while (self._queue or self._writing) and not self._closed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._cv.wait(min(0.05, remaining))
        return True

    def flush_aux(self, layer: str, session_id: str, *, timeout: float = 30.0) -> bool:
        """Same acknowledgement for a layer stream (the writer is shared)."""
        return self.flush(timeout=timeout)

    def close(self) -> None:
        """Flush, mark this process's cache ranges, fsync, and release the process lock."""
        if self._persist_dir is None:
            self._closed = True
            return
        with self._cv:
            if self._closed:
                return
        self.flush()
        self._finalize_markers()
        process, self._process = self._process, None
        if process is not None:
            try:
                last = process.finish()
            except Exception:
                logger.debug("transcript: process lock finish failed", exc_info=True)
                last = False
            if last:
                self._fsync_all()
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

    def _finalize_markers(self) -> None:
        """Append one ``last_cache`` row per stream this process wrote, then fsync each file."""
        if self._persist_dir is None:
            return
        with self._lock:
            written = dict(self._written)
        for key, (low, high) in written.items():
            stream = self._streams.get(key)
            if stream is None:
                continue
            with stream.lock:
                coverage = stream.coverage()
                from_seq = max(low, int(stream.evicted_before) + 1)
                to_seq = coverage[1] or high
                if to_seq < from_seq:
                    continue
                stream.last_seq += 1
                now = time.time()
                row = {"v": 1, "seq": stream.last_seq, "ts": now, "iso": _iso(now),
                       "profile": key[0], "layer": key[1], "session_id": key[2], "turn": 0,
                       "step": 0, "role": "", "kind": "marker", "run_id": "",
                       "redacted": True, "marker": "last_cache",
                       "from_seq": int(from_seq), "to_seq": int(to_seq)}
                self._append_rows(stream, [row])
            self._fsync_stream(stream)

    def _fsync_stream(self, stream: _Stream) -> None:
        for path in self._parts_for(stream):
            try:
                with open(path, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                logger.debug("transcript: fsync failed for %s", path, exc_info=True)

    def _fsync_all(self) -> None:
        if self._persist_dir is None:
            return
        try:
            paths = list(self._persist_dir.rglob("*.jsonl"))
        except OSError:
            return
        for path in paths:
            try:
                with open(path, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                logger.debug("transcript: fsync failed for %s", path, exc_info=True)

    @property
    def persist_dir(self) -> Optional[Path]:
        return self._persist_dir

    @property
    def dropped(self) -> int:
        with self._cv:
            return self._dropped

    @property
    def errors(self) -> int:
        with self._cv:
            return self._errors

    @property
    def files_written(self) -> int:
        with self._cv:
            return self._files_written

    # -- cache ---------------------------------------------------------------

    def _stream(self, profile: str, layer: str, session_id: str) -> _Stream:
        key = (str(profile or "default"), str(layer or HOST_LAYER), str(session_id or ""))
        with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                stream = self._streams[key] = _Stream(*key, lock=self._lock)
                self._evict_streams()
            stream.updated_at = time.time()
            return stream

    def _pinned(self, stream: _Stream) -> bool:
        return (stream.session_id == self._pinned_session
                and stream.profile == self._profile
                and stream.layer == (self._pinned_layer or HOST_LAYER))

    def _cache_row(self, stream: _Stream, row: dict[str, Any]) -> None:
        with self._lock:
            stream.rows.append(row)
            size = len(json.dumps(row, ensure_ascii=False, default=str))
            stream.size += size
            stream.chars += len(str(row.get("text") or "")) + len(str(row.get("result") or ""))
            self._total_bytes += size
            while len(stream.rows) > self._events_per_stream:
                dropped = stream.rows.popleft()
                dropped_size = len(json.dumps(dropped, ensure_ascii=False, default=str))
                stream.size -= dropped_size
                stream.chars -= (len(str(dropped.get("text") or ""))
                                 + len(str(dropped.get("result") or "")))
                self._total_bytes -= dropped_size
                stream.evicted_before = max(stream.evicted_before,
                                            int(dropped.get("seq") or 0))
            stream.updated_at = time.time()
            self._evict_bytes(keep=stream.key)

    def _evict_streams(self) -> None:
        """Keep at most ``max_sessions`` streams; never the pinned session/layer."""
        with self._lock:
            while len(self._streams) > self._max_sessions:
                candidates = [key for key, stream in self._streams.items()
                              if not self._pinned(stream)]
                if not candidates:
                    return
                oldest = min(candidates, key=lambda key: self._streams[key].updated_at)
                stream = self._streams.pop(oldest)
                self._total_bytes -= stream.size

    def _evict_bytes(self, keep: Optional[tuple[str, str, str]] = None) -> None:
        """Keep the cache under the byte bound: oldest whole stream first, then its rows."""
        with self._lock:
            guard = 0
            while self._total_bytes > self._buffer_bytes and self._streams and guard < 10_000:
                guard += 1
                candidates = [key for key, stream in self._streams.items()
                              if not self._pinned(stream) and stream.rows and key != keep]
                if not candidates:
                    break
                oldest = min(candidates, key=lambda key: self._streams[key].updated_at)
                stream = self._streams[oldest]
                if len(self._streams) > 1:
                    self._streams.pop(oldest, None)
                    self._total_bytes -= stream.size
                    continue
                dropped = stream.rows.popleft()
                dropped_size = len(json.dumps(dropped, ensure_ascii=False, default=str))
                stream.size -= dropped_size
                self._total_bytes -= dropped_size
                stream.evicted_before = max(stream.evicted_before,
                                            int(dropped.get("seq") or 0))

    def _stream_lock_path(self, stream: _Stream) -> Path:
        base = self.path_for_stream(*stream.key)
        folder = self._run_dir or (base.parent if base is not None else Path("."))
        return folder / ("stream-" + hashlib.sha1(
            "/".join(stream.key).encode()).hexdigest()[:16] + ".lock")

    def _ensure_loaded(self, stream: _Stream) -> None:
        """Initialise ``seq``/``last_seen``/turn from the stream's file, once.

        The tail read runs under the stream's file lock: two processes starting together must not
        both read the same ``max(seq)`` and allocate it again. Per-event allocation stays in
        memory (an append must stay off the turn's thread); this closes the startup race.
        """
        if stream.loaded:
            return
        if self._persist_dir is None:
            stream.loaded = True  # memory-only buffer: nothing to read, nothing to lock
            return
        with transcript_process.StreamLock(self._stream_lock_path(stream)):
            if stream.loaded:
                return
            files = self._parts_for(stream)
            if files:
                rows = _tail_rows(files[-1], 200)  # newest first
                for row in rows:
                    if int(row.get("seq") or 0) > stream.last_seq:
                        stream.last_seq = int(row.get("seq") or 0)
                # Keep the last few message fingerprints: two identical answers need the sequence
                # to tell a repeated tail from a genuinely new one. ``fps`` is oldest-to-newest.
                recent = [row["fp"] for row in rows
                          if str(row.get("kind") or "") == "msg" and row.get("fp")]
                if recent:
                    stream.last_seen = {"fp": recent[0], "fps": list(reversed(recent[:8])),
                                        "role": ""}
                for row in rows:
                    if row.get("turn") or row.get("step"):
                        stream.turn = int(row.get("turn") or 0)
                        stream.step = int(row.get("step") or 0)
                        break
            stream.loaded = True

    def _update_seen(self, stream: _Stream, rows: Iterable[dict[str, Any]]) -> None:
        ordered = list(rows)
        recent = [row["fp"] for row in reversed(ordered)
                  if str(row.get("kind") or "") == "msg" and row.get("fp")]
        if recent:
            stream.last_seen = {"fp": recent[0], "fps": list(reversed(recent[:8])),
                                "role": ""}
        for row in reversed(ordered):
            if row.get("turn") or row.get("step"):
                stream.turn = int(row.get("turn") or 0)
                stream.step = int(row.get("step") or 0)
                break

    # -- writer ---------------------------------------------------------------

    def _enqueue(self, stream: _Stream, rows: list[dict[str, Any]]) -> None:
        with self._cv:
            if self._closing or self._closed:
                self._dropped += len(rows)
                return
            if len(self._queue) >= self._queue_max:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append((stream.key, rows))
            self._cv.notify_all()

    def _writer_loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closing:
                    self._cv.wait()
                if self._closing and not self._queue:
                    return
                batch: list[tuple[tuple[str, str, str], list[dict[str, Any]]]] = []
                while self._queue:
                    batch.append(self._queue.popleft())
                self._writing += 1
            try:
                grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
                for key, rows in batch:
                    grouped.setdefault(key, []).extend(rows)
                for key, rows in grouped.items():
                    stream = self._stream(*key)
                    try:
                        self._append_rows(stream, rows)
                    except Exception:  # health: allow BLE001 -- one bad batch must not kill it
                        logger.exception("transcript: unexpected writer failure for %s", key)
                        with self._cv:
                            self._errors += 1
            finally:
                with self._cv:
                    self._writing -= 1
                    self._cv.notify_all()

    def _parts_for(self, stream: _Stream) -> list[Path]:
        base = self.path_for_stream(*stream.key)
        if base is None:
            return []
        files: list[tuple[int, Path]] = []
        try:
            for path in base.parent.glob(base.stem + "*.jsonl"):
                if path.name == base.name:
                    files.append((1, path))
                    continue
                match = _PART_RE.match(path.name)
                if match and match.group("stem") == base.stem:
                    files.append((int(match.group("part")), path))
        except OSError:
            return []
        return [path for _part, path in sorted(files)]

    def _part_state(self, stream: _Stream) -> tuple[int, int]:
        with self._lock:
            state = self._parts.get(stream.key)
            if state is not None:
                return state
            files = self._parts_for(stream)
            if files:
                last = files[-1]
                match = _PART_RE.match(last.name)
                part = int(match.group("part")) if match else 1
                lines = _count_lines(last)
            else:
                part, lines = 1, 0
            self._parts[stream.key] = (part, lines)
            return part, lines

    def _set_part_state(self, stream: _Stream, part: int, lines: int) -> None:
        with self._lock:
            self._parts[stream.key] = (part, lines)

    def _append_rows(self, stream: _Stream, rows: list[dict[str, Any]]) -> None:
        base = self.path_for_stream(*stream.key)
        if base is None or not rows:
            return
        with transcript_process.StreamLock(self._stream_lock_path(stream)):
            part, lines = self._part_state(stream)
            base.parent.mkdir(parents=True, exist_ok=True)
            handle = None
            opened = 0
            try:
                for row in rows:
                    if handle is None or lines >= self._events_per_part:
                        if handle is not None:
                            handle.close()
                        if lines >= self._events_per_part:
                            part += 1
                            lines = 0
                        path = (base if part == 1 else
                                base.with_name(f"{base.stem}.part{part}.jsonl"))
                        handle = open(path, "a", encoding="utf-8", newline="\n")
                        try:
                            if path.stat().st_size:
                                with open(path, "rb") as probe:
                                    probe.seek(-1, os.SEEK_END)
                                    if probe.read(1) != b"\n":
                                        handle.write("\n")
                        except OSError:
                            pass
                        opened += 1
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                    lines += 1
                if handle is not None:
                    handle.flush()
            finally:
                if handle is not None:
                    handle.close()
                self._set_part_state(stream, part, lines)
                with self._cv:
                    self._files_written += opened


def _range_ok(value: Any, wanted: Any) -> bool:
    """``None`` -> any; int -> equality; ``(a, b)`` -> half-open ``[a, b)``."""
    if wanted is None:
        return True
    actual = int(value or 0)
    if isinstance(wanted, (tuple, list)):
        low, high = int(wanted[0]), int(wanted[1])
        return low <= actual < high
    return actual == int(wanted)
