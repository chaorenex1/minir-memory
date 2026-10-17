"""The session's messages, in memory only — never persisted.

This provider deliberately stores no conversation content (see ``store.py``): the completed
turns live here for the length of the process, and the only things that reach disk are what the
layers *derived* from them. Three consequences drive the shape of this module:

* **Bounded.** A long-lived gateway session would otherwise grow without limit, so each session
  keeps its newest messages under both a count and a character budget, and only the newest
  sessions are kept at all. The oldest messages of an over-long window are dropped by the
  evidence formatter (``worker._evidence``) as well — losing the *old* end of a window is the
  deliberate trade for not keeping a transcript.
* **Per session.** ``sync_turn``/``on_turn_start`` carry no session id on every host path, so
  the provider binds one session at a time; the buffer still keys by id because a switch can
  arrive between a turn ending and its job being drained.
* **Content-free after a restart.** Nothing here survives the process, which is exactly why the
  worker closes the jobs it inherits instead of reading them (``LayerWorker.expire_orphans``).

Only completed turns are added: ``sync_turn`` already gates trivial prompts and non-primary
contexts, so a window holds what the layers should actually model.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Iterable, Optional

_MAX_MESSAGES = 400          # per session
_MAX_CHARS = 200_000         # per session, across kept messages
_MAX_MESSAGE_CHARS = 4_000   # one message (long pastes must not own the whole budget)
_MAX_SESSIONS = 8            # long-lived processes open many sessions; only recent ones matter
_EPSILON = 1e-6              # keeps a message strictly inside the window that starts at the edge


def _flatten(text: str) -> str:
    return " ".join((text or "").split())


class TranscriptBuffer:
    """Thread-safe, bounded, in-memory window of each session's completed turns."""

    def __init__(self, *, max_messages: int = _MAX_MESSAGES, max_chars: int = _MAX_CHARS,
                 max_sessions: int = _MAX_SESSIONS):
        self._lock = threading.Lock()
        self._sessions: dict[str, deque[dict[str, Any]]] = {}
        self._chars: dict[str, int] = {}
        self._edges: dict[str, float] = {}   # newest window edge dispatched per session
        self._max_messages = max(1, int(max_messages))
        self._max_chars = max(1, int(max_chars))
        self._max_sessions = max(1, int(max_sessions))

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
        ever see a bounded window anyway.
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

    def add_turn(self, session_id: str, user_content: str, assistant_content: str, *,
                 author: str = "", author_id: str = "", created_at: Optional[float] = None) -> None:
        """Append one completed turn (the pair the host hands ``sync_turn``)."""
        self.add(session_id, "user", user_content, author=author, author_id=author_id,
                 created_at=created_at)
        self.add(session_id, "assistant", assistant_content, created_at=created_at)

    def clear(self, session_id: Optional[str] = None) -> None:
        """Drop one session's messages, or every session's.

        Deliberately not called on ``/new``: the session that just ended may still have a queued
        boundary job to drain, and emptying its bucket would hand that job an empty window. The
        caps bound what is kept until the process ends.
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
