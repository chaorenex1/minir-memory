"""Progressive, lossless context management driven by the dialectic-layer cadence.

Instead of waiting for the token threshold and folding the window in one destructive pass, this
engine moves the **oldest unprotected slice out of the window on every dialectic beat** — the same
turn interval the memory provider's dialectic layer refreshes on (see ``cadence.py``). The slice
is replaced by a compact *carrier* row that names the turns it covers, lists the message ids that
went with it and carries a one-line gist, so the window stays small while the transcript stays
complete.

**Lossless, without a second copy.** Everything folded out is still verbatim in the host's own
``$HERMES_HOME/state.db``, where every row carries ``message_uid`` and the store has FTS5. The
engine therefore stores no conversation text at all:

* ``select_context`` re-injects the folded originals into a request when the incoming message
  actually asks about them, and
* the ``minir_context_recall`` tool lets the model pull them on demand.

Nothing is duplicated, and the memory provider's "no transcripts" rule is untouched — the
transcript lives where the host already put it.

Four properties matter for the host's contract:

* **A real subclass.** The host resolves engines by ``issubclass(ContextEngine)`` (or a captured
  ``register_context_engine``) and the plugin host proxies the class by ``base_ref``, so this
  inherits the ABC rather than duck-typing it.
* **Fail-open.** Every hook returns "leave it alone" on any error: a broken engine must never be
  worse than no engine.
* **Side effects are indexed, not authoritative.** On a fold the engine may call the auxiliary
  model once with the operator's Task Handoff template, write the resulting session summary to
  the plugin's own conclusions index as ``pending``, and queue a dialectic job. The conversation
  itself is never copied: the carrier in the returned list is what the host commits, and the
  folded originals remain in ``state.db``. A summary becomes ``active`` only when
  ``on_turn_complete`` observes the carrier in the persisted messages; a discarded pass leaves a
  ``pending`` row that expires on the TTL sweep and never reaches recall.
* **Cache-stable.** ``select_context`` returns ``None`` unless the message genuinely matches folded
  content, and an injection only appends to the last user message, so every byte before it keeps
  matching the provider's prompt cache.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from agent.context_engine import ContextEngine

from . import cadence, handoff
from .store import MinirStore, format_when
from .turn_counter import BASE_LAYER
from .turn_counter import DEFAULT_DIALECTIC_INTERVAL as _DEFAULT_DIALECTIC_INTERVAL
from .turn_counter import DIALECTIC_LAYER

logger = logging.getLogger(__name__)

ENGINE_NAME = "minir"

# A carrier rides as a **user-role** row with a self-identifying content marker: SessionDB drops
# ``_``-metadata, so content is authoritative (the same reason the built-in compressor classifies
# its own handoffs by content). It deliberately does NOT imitate the host's "[CONTEXT COMPACTION …]"
# handoff — that format tells the model the compacted work is dead reference material, while a fold
# means the opposite: the work is live and only some turns moved out of the window.
CARRIER_MARKER = "[minir fold"
_CARRIER_RE = re.compile(
    r"^\[minir fold · turns (?P<first>\d+)-(?P<last>\d+) · (?P<count>\d+) msgs\]\n"
    r"recover: (?P<uids>[^\n]*)\n"
    r"gist: (?P<gist>[^\n]*)(?:\nsummary:\n(?P<summary>.*))?$",
    re.DOTALL)

_DEFAULT_INTERVAL = _DEFAULT_DIALECTIC_INTERVAL  # the provider's own default, resolved in cadence
_MAX_FOLD_MESSAGES = 24         # messages one fold may move (progressive: a slice per beat)
_MAX_CARRIER_CHARS = 2400
_MAX_GIST_CHARS = 200
_MAX_CARRIER_SUMMARY = 900      # bounded summary that rides in the returned message sequence
_MAX_RECALL_CHARS = 1600        # injected per request
_MAX_RECALL_MESSAGES = 4
_MAX_FETCHED = 400              # originals read from state.db per recall
_ELIDED_TOOL_MARKER = "[tool result elided by minir · recoverable from the session store]"
_MIN_QUERY_TERMS = 2            # a one-word or trivial prompt never triggers an injection

_TERM_RE = re.compile(r"[\w-]{3,}", re.UNICODE)

# Words that appear in almost every folded turn carry no retrieval signal. Matching on them would
# inject context for any chatty question ("what about the weather") and change the prompt's cache
# prefix for nothing, so they never count as a query term.
_STOPWORDS = frozenset("""
about after again against all also and any are because been before being between both but can
could did does doing down during each even ever every few for from further had has have having
here how into its itself just like made make many more most much must not now off once only other
over own same should some such than that the their them then there these they this those through
too under until very was were what when where which while who whom why will with would you your
""".split())


def _terms(text: str, limit: int = 12) -> list[str]:
    """Lowercased search terms, order kept, deduped, stopwords dropped — the recall scorer."""
    seen: list[str] = []
    for token in _TERM_RE.findall((text or "").lower()):
        if token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def _text_of(message: Any) -> str:
    """Flatten a message's content to text (a content-part list becomes its text parts)."""
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part["text"] for part in content
                 if isinstance(part, dict) and isinstance(part.get("text"), str)]
        return "\n".join(parts)
    return ""


def is_carrier(message: Any) -> bool:
    """True for a row this engine wrote to stand in for folded messages."""
    return isinstance(message, dict) and message.get("role") == "user" \
        and _text_of(message).lstrip().startswith(CARRIER_MARKER)


def parse_carrier(message: Any) -> Optional[dict[str, Any]]:
    """Carrier row -> ``{first, last, count, uids, gist, summary}``, or ``None``."""
    match = _CARRIER_RE.match(_text_of(message).lstrip())
    if match is None:
        return None
    return {"first": int(match.group("first")), "last": int(match.group("last")),
            "count": int(match.group("count")),
            "uids": [uid for uid in match.group("uids").split(",") if uid],
            "gist": match.group("gist").strip(),
            "summary": (match.group("summary") or "").strip()}


def carrier_turn(message: Any) -> Optional[int]:
    """The turn a carrier folded through (the cadence reads this to stay monotonic across folds)."""
    parsed = parse_carrier(message)
    return parsed["last"] if parsed else None


def build_carrier(first_turn: int, last_turn: int, messages: list[dict[str, Any]],
                  gist: str = "", summary: str = "") -> dict[str, Any]:
    """The row that replaces a folded slice.

    ``message_uid`` is minted here on purpose: the host writes an engine-set uid through the
    commit, so the carrier survives a restart as a row the engine recognises (and a later fold can
    absorb its id list instead of nesting carriers). ``summary`` is the bounded session-summary
    conclusion that must travel with the OpenAI message sequence; the full text stays in the
    conclusions store.
    """
    uids: list[str] = []
    for message in messages:
        parsed = parse_carrier(message)
        if parsed is not None:          # an earlier fold's ids survive this one
            uids.extend(parsed["uids"])
            continue
        uid = str(message.get("message_uid") or "")
        if uid:
            uids.append(uid)
    uids = list(dict.fromkeys(uids))
    gist = " ".join((gist or "").split())[:_MAX_GIST_CHARS]
    summary = summary.strip()[:_MAX_CARRIER_SUMMARY]
    head = f"[minir fold · turns {first_turn}-{last_turn} · {len(messages)} msgs]\nrecover: "
    tail = f"\ngist: {gist or 'no gist recorded'}"
    if summary:
        tail += f"\nsummary:\n{summary}"
    # Room for the elision note too, so the finished carrier is within the cap as written.
    budget = max(0, _MAX_CARRIER_CHARS - len(head) - len(tail) - 16)
    listed: list[str] = []
    size = 0
    for uid in uids:
        if size + len(uid) + 1 > budget:
            break
        listed.append(uid)
        size += len(uid) + 1
    if len(listed) < len(uids):         # pathological fold: say so rather than silently drop ids
        listed.append(f"+{len(uids) - len(listed)} more")
    return {"role": "user", "content": f"{head}{','.join(listed)}{tail}",
            "message_uid": uuid.uuid4().hex}


def foldable_slice(messages: list[dict[str, Any]], protect_first_n: int, protect_last_n: int, *,
                   focus_topic: Optional[str] = None) -> Optional[tuple[int, int]]:
    """The half-open ``[start, end)`` slice this engine may fold, or ``None``.

    Rules: never touch the leading system prompt (or the ``protect_first_n`` messages after it),
    never touch the ``protect_last_n`` messages at the tail, never remove the newest user-authored
    turn (the host relies on a real user turn being present), and always end the slice where the
    next user-role row begins — so the retained window opens on a user turn and no assistant /
    tool-call / tool-result group is ever split.
    """
    if not isinstance(messages, list) or len(messages) < 3:
        return None
    head = 1 if messages[0].get("role") == "system" else 0
    floor = head + max(0, int(protect_first_n))          # keep at least this many head messages
    ceiling = len(messages) - max(0, int(protect_last_n))
    starts = [index for index in range(head, len(messages))
              if messages[index].get("role") == "user"]
    # The newest user-authored turn never moves (the host relies on a real user turn being there).
    last_user = max((index for index in starts if not is_carrier(messages[index])), default=-1)
    if last_user >= 0:
        ceiling = min(ceiling, last_user)
    inside = [index for index in starts if floor <= index <= ceiling]
    if len(inside) < 2:                                  # at least one whole turn must move
        return None
    start = inside[0]
    end = start
    for index in inside[1:]:                             # one fold is one bounded run of turns
        if index - start > _MAX_FOLD_MESSAGES:
            break
        end = index
    if end <= start:
        return None
    if focus_topic:  # keep whole turns that a manual /compress <focus> asked to prioritise
        terms = _terms(focus_topic)
        while terms and end > start and any(term in _text_of(messages[end - 1]).lower()
                                            for term in terms):
            previous = max((index for index in inside if index < end), default=start)
            if previous <= start:
                return None  # nothing can move without losing what the user asked to keep
            end = previous
    return start, end


def folded_uids(messages: Iterable[dict[str, Any]]) -> list[str]:
    """Every message uid named by the carriers in *messages*, in carrier order."""
    uids: list[str] = []
    for message in messages or ():
        parsed = parse_carrier(message)
        if parsed:
            uids.extend(parsed["uids"])
    return list(dict.fromkeys(uids))


def _last_index(messages: list[dict[str, Any]], role: str) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], dict) and messages[index].get("role") == role:
            return index
    return -1


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    index = _last_index(messages, "user")
    return _text_of(messages[index]) if index >= 0 else ""


class MinirContextEngine(ContextEngine):
    """Progressive lossless context management on the dialectic cadence."""

    # Fold passes are routine background maintenance: keep them out of the chat.
    emit_automatic_compaction_status = False

    def __init__(self, *, hermes_home: str = "", interval: int = 0, recall: bool = True,
                 state_db: str = "") -> None:
        self._hermes_home = str(hermes_home or "")
        self._state_db_override = str(state_db or "")
        # An explicit interval (tests, forks) wins; otherwise the profile's configured
        # dialectic interval is resolved as soon as the home is known (on_session_start).
        self._interval_override = int(interval) if int(interval or 0) > 0 else 0
        self._interval = self._interval_override or _DEFAULT_INTERVAL
        self._recall_enabled = bool(recall)
        self._session_id = ""
        self._beat = 0                # newest cadence beat already folded
        self._window_hint = 0         # newest beat the completed turns accounted for
        self._lock = threading.RLock()  # reentrant: due() reads state through current_beat()
        self._compress_requested_at = 0.0
        self._store: Optional[MinirStore] = None
        self._store_checked = False
        self._summary_enabled = (cadence.configured_handoff("")
                                 and cadence.configured_reasoning(""))
        self.fold_count = 0
        self.last_folded_messages = 0
        self.folded_through_turn = 0

    # -- contract -------------------------------------------------------------

    @property
    def name(self) -> str:
        return ENGINE_NAME

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Bind the session and the profile home the cadence and the store reads resolve against."""
        with self._lock:
            self._session_id = str(session_id or "")
            self._beat = 0
            self._window_hint = 0
            self._compress_requested_at = 0.0
            self.last_folded_messages = 0
            self.folded_through_turn = 0
        if kwargs.get("hermes_home"):
            self._hermes_home = str(kwargs["hermes_home"])
        if not self._interval_override:  # the profile is known now: follow its dialectic cadence
            self._interval = cadence.configured_interval(self._hermes_home)
        self._summary_enabled = (cadence.configured_handoff(self._hermes_home)
                                 and cadence.configured_reasoning(self._hermes_home))

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Forget per-session state; the carriers in the store remain the durable record."""
        self.on_session_reset()

    def on_turn_complete(self, messages: list[dict[str, Any]], usage: Optional[dict[str, Any]] = None,
                         **kwargs: Any) -> None:
        """Observe the finished turn: remember how far the window has counted (best-effort hook)."""
        if isinstance(messages, list):
            counted = cadence.window_turns(messages, is_carrier=is_carrier, carrier_end=carrier_turn)
            with self._lock:
                self._window_hint = max(self._window_hint, counted)
            if self._session_id and any(is_carrier(message) for message in messages):
                store = self._memory_store()
                if store is not None:
                    # A carrier persists across turns. Seal the chunk chain only when this
                    # observation actually promoted a pending summary; otherwise an old
                    # carrier would supersede a chain that is still being built.
                    if store.promote_pending_handoffs(self._session_id) > 0:
                        store.supersede_chunk_summaries(self._session_id)

    def on_session_reset(self) -> None:
        """``/new`` and ``/reset``: fresh session state plus the base class's counter reset."""
        with self._lock:
            self._session_id = ""
            self._beat = 0
            self._window_hint = 0
            self._compress_requested_at = 0.0
            self.last_folded_messages = 0
            self.folded_through_turn = 0
            self.fold_count = 0
        super().on_session_reset()

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Track token usage after every LLM call (the host reads these fields directly).

        Accepts the ABC's usage dict, a full response with a nested ``usage``, and the
        canonical ``input_tokens``/``output_tokens`` buckets used by older hosts.
        """
        payload: Any = usage
        if not isinstance(payload, dict):
            payload = getattr(usage, "usage", None)
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            payload = payload["usage"]
        if not isinstance(payload, dict):
            return
        prompt = int(payload.get("prompt_tokens", payload.get("input_tokens")) or 0)
        completion = int(payload.get("completion_tokens", payload.get("output_tokens")) or 0)
        total = int(payload.get("total_tokens") or (prompt + completion))
        self.last_prompt_tokens = prompt
        self.last_completion_tokens = completion
        self.last_total_tokens = total

    # -- cadence --------------------------------------------------------------

    def current_beat(self, messages: Optional[Iterable[dict[str, Any]]] = None) -> int:
        """The session's turn count for cadence purposes (shared with the memory provider).

        With a message list it is the window's own count; without one — the host's
        ``should_compress()`` path — it falls back to the stored dialectic beat and to the newest
        count a completed turn reported, so the decision does not depend on having the window.
        """
        window = cadence.window_turns(messages, is_carrier=is_carrier, carrier_end=carrier_turn) \
            if messages is not None else 0
        stored = cadence.dialectic_beat(cadence.store_path(self._hermes_home), self._session_id)
        with self._lock:
            hint = self._window_hint
        return max(stored, hint, window)

    def due(self, messages: Optional[Iterable[dict[str, Any]]] = None) -> bool:
        """True when a new dialectic beat has landed since the last fold (an interval elapsed)."""
        # Not held across the call: ``current_beat`` takes the same lock (it is reentrant, but
        # keeping the critical section to the reads is clearer).
        beat = self.current_beat(messages)
        with self._lock:
            return beat - self._beat >= self._interval

    # -- compaction -----------------------------------------------------------

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """Fold on a dialectic beat, or when the window is genuinely near the limit.

        The threshold is the hard upper bound on the refresh interval: when it is crossed, a
        final base-layer chunk refresh and a final dialectic refresh are queued before the fold.
        """
        tokens = self.last_prompt_tokens if prompt_tokens is None else int(prompt_tokens)
        if self.threshold_tokens and tokens >= self.threshold_tokens:
            with self._lock:
                self._compress_requested_at = time.time()
            self._queue_final_refresh()
            return True
        if self.current_beat() - self._beat >= self._interval:
            with self._lock:
                self._compress_requested_at = time.time()
            self._queue_final_refresh()
            return True
        return False

    def _queue_final_refresh(self) -> None:
        """Threshold is a hard cap: force one last base chunk + dialectic refresh."""
        store = self._memory_store()
        if store is None or not self._session_id:
            return
        requested = self._compress_requested_at or time.time()
        beat = self.current_beat()
        try:
            if not store.has_open_job(BASE_LAYER, self._session_id):
                start = store.latest_chunk_end(self._session_id)
                if requested > start:
                    store.record_job(BASE_LAYER, self._session_id, start, requested,
                                     turns=int(beat))
            self._enqueue_dialectic(store, requested, int(beat))
        except Exception as exc:
            logger.debug("Minir final refresh enqueue skipped: %s", exc)

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        """Defer to real usage whenever the last completed request fit under the threshold.

        A progressive engine folds ahead of the limit; without this the host would re-compact on
        top of a fold using its own noisy estimate.
        """
        return bool(self.last_prompt_tokens) and bool(self.threshold_tokens) \
            and self.last_prompt_tokens < self.threshold_tokens

    def has_content_to_compress(self, messages: list[dict[str, Any]]) -> bool:
        """False reports "nothing foldable yet" without touching the model (gateway ``/compress``)."""
        return foldable_slice(messages, self.protect_first_n, self.protect_last_n) is not None

    def compress(
        self, messages: list[dict[str, Any]], current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",
    ) -> list[dict[str, Any]]:
        """Fold the oldest unprotected slice into one carrier and queue the dialectic window.

        Optionally makes one bounded handoff/summary call when ``reasoning_enabled`` and
        ``handoff_enabled`` are on; always attempts the dialectic enqueue. Never raises.
        """
        try:
            bounds = foldable_slice(messages, self.protect_first_n, self.protect_last_n,
                                    focus_topic=focus_topic)
            if bounds is None:
                return messages
            start, end = bounds
            folded = messages[start:end]
            beat = self.current_beat(messages)
            store = self._memory_store()
            previous = ""
            historical = ""
            tail = ""
            chunks = ""
            if store is not None and self._session_id:
                row = (store.newest_conclusion("session_summary", session_id=self._session_id)
                       or store.newest_conclusion("handoff", session_id=self._session_id))
                previous = str((row or {}).get("content") or "")
                historical, tail = self._dialectic_inputs(store, self._session_id)
                chunks = self._chunk_input(store, self._session_id)
            summary = ""
            if self._summary_enabled and folded:
                summary = handoff.summarize(
                    folded, previous=previous, historical=historical, tail=tail,
                    chunks=chunks, focus_topic=focus_topic or "",
                    memory_context=memory_context or "")
            gist = handoff.gist(summary) if summary else ""
            if not gist:
                gist = self._gist_for(folded, memory_context)
            carrier = build_carrier(self._first_turn(folded, beat), beat, folded, gist,
                                    summary=summary)
            result = [*messages[:start], carrier, *messages[end:]]
            requested_at = self._compress_requested_at or time.time()
            if store is not None and self._session_id:
                try:
                    if summary:
                        store.expire_pending_handoffs(self._session_id,
                                                      before=time.time() - 3_600)
                        key = f"session_summary:{self._session_id}:{start}:{end}"
                        store.put_conclusion(
                            "session_summary", key, summary, session_id=self._session_id,
                            subject="session", evidence=json.dumps(
                                [uid for uid in folded_uids(folded)]), status="pending",
                            version=beat)
                    self._enqueue_dialectic(store, requested_at, beat)
                except Exception as exc:  # indexing must not discard a usable fold
                    logger.debug("Minir handoff indexing skipped: %s", exc)
            with self._lock:
                self._beat = beat
                self.folded_through_turn = beat
                self.last_folded_messages = len(folded)
                self._compress_requested_at = 0.0
            self.compression_count += 1
            self.fold_count += 1
            logger.info("Minir fold: %d messages (through turn %d) -> carrier, %d left verbatim",
                        len(folded), beat, len(result))
            return result
        except Exception as exc:  # fail-open: an unusable engine leaves the window alone
            logger.warning("Minir fold failed, leaving the window unchanged: %s", exc)
            return messages

    def prune_tool_results_only(
        self, messages: list[dict[str, Any]], current_tokens: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Replace old tool-result payloads with a marker; the originals stay in the store."""
        try:
            last_user = _last_index(messages, "user")
            cut = max(0, len(messages) - self.protect_last_n)
            pruned = 0
            for index, message in enumerate(messages[:cut]):
                if not isinstance(message, dict) or message.get("role") != "tool" or index >= last_user:
                    continue
                body = _text_of(message)
                if not body or body.startswith(_ELIDED_TOOL_MARKER):
                    continue
                messages[index] = {**message, "content": _ELIDED_TOOL_MARKER}
                pruned += 1
            return messages, pruned
        except Exception as exc:
            logger.debug("Minir tool pruning failed: %s", exc)
            return messages, 0

    # -- request-time selection ----------------------------------------------

    def select_context(
        self, request_messages: list[dict[str, Any]], *,
        conversation_messages: Optional[list[dict[str, Any]]] = None,
        incoming_message: Optional[dict[str, Any]] = None, budget_tokens: int = 0,
    ) -> Optional[list[dict[str, Any]]]:
        """Re-inject folded originals when this request actually asks about them.

        Returns a new list only on a real match, and the injected text is appended to the last user
        message so every byte before it keeps matching the provider's prompt cache.
        """
        if not self._recall_enabled or not isinstance(request_messages, list) or not request_messages:
            return None
        try:
            query = _text_of(incoming_message) if incoming_message else _last_user_text(request_messages)
            terms = _terms(query)
            if len(terms) < _MIN_QUERY_TERMS:
                return None
            block = self._recall_block(folded_uids(request_messages), terms)
            if not block:
                return None
            target = _last_index(request_messages, "user")
            if target < 0 or not isinstance(request_messages[target].get("content"), str):
                return None
            recalled = [dict(message) for message in request_messages]
            recalled[target] = {**recalled[target],
                                "content": f"{recalled[target]['content']}\n\n{block}"}
            return recalled
        except Exception as exc:  # fail-open: never make a request worse
            logger.debug("Minir context recall skipped: %s", exc)
            return None

    # -- tools ----------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [{
            "name": "minir_context_recall",
            "description": ("Search the conversation content that was folded out of the current "
                            "context window (older turns). Returns the original text, so nothing "
                            "folded out is lost."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for."},
                    "limit": {"type": "integer", "description": "Maximum results (default 4)."},
                },
                "required": ["query"],
            },
        }]

    def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs) -> str:
        if name != "minir_context_recall":
            return json.dumps({"error": f"Unknown context engine tool: {name}"})
        try:
            query = str((args or {}).get("query") or "").strip()
            if not query:
                return json.dumps({"error": "query is required"})
            limit = max(1, min(int((args or {}).get("limit") or _MAX_RECALL_MESSAGES), 10))
            messages = kwargs.get("messages") if isinstance(kwargs.get("messages"), list) else []
            uids = folded_uids(messages) or self._folded_uids_from_store()
            hits = self._match(uids, _terms(query), limit=limit)
            return json.dumps({"query": query, "count": len(hits),
                               "results": [{"when": hit["when"], "role": hit["role"],
                                            "text": hit["text"][:600]} for hit in hits]})
        except Exception as exc:
            return json.dumps({"error": f"recall failed: {exc}"})

    # -- status ---------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """The standard token/threshold status plus what this engine adds."""
        return {**super().get_status(), "engine": ENGINE_NAME, "folds": self.fold_count,
                "interval_turns": self._interval, "folded_through_turn": self.folded_through_turn,
                "last_folded_messages": self.last_folded_messages}

    def clone_for_agent(self) -> "MinirContextEngine":
        """Fresh engine for another agent: same profile/config, no session state, nothing shared."""
        return MinirContextEngine(hermes_home=self._hermes_home, interval=self._interval_override,
                                  recall=self._recall_enabled, state_db=self._state_db_override)

    # -- recall plumbing ------------------------------------------------------

    @staticmethod
    def _first_turn(folded: list[dict[str, Any]], beat: int) -> int:
        """The earliest turn the slice covers.

        An absorbed carrier states it exactly; otherwise the slice covers the turns that end at
        *beat*, one per user-authored message in it.
        """
        for message in folded:
            parsed = parse_carrier(message)
            if parsed is not None:
                return parsed["first"]
        user_turns = sum(1 for message in folded
                         if message.get("role") == "user" and not is_carrier(message))
        return max(1, beat - user_turns + 1)

    @staticmethod
    def _gist_for(folded: list[dict[str, Any]], memory_context: str) -> str:
        """A carrier gist without a model call: the provider's note, else the slice's first text."""
        if memory_context.strip():
            return memory_context.strip()
        for message in folded:
            text = " ".join(_text_of(message).split())
            if text and not is_carrier(message):
                return text
        return ""

    @staticmethod
    def _chunk_input(store: MinirStore, session: str) -> str:
        """The canonical local-compression chain, oldest first."""
        parts: list[str] = []
        for index, chunk in enumerate(store.chunk_summaries(session), start=1):
            body = str(chunk.get("content") or "").strip()
            if not body:
                continue
            window = ""
            if chunk.get("from_ts") is not None and chunk.get("to_ts") is not None:
                window = (f" · {format_when(float(chunk['from_ts']))}"
                          f"–{format_when(float(chunk['to_ts']))}")
            parts.append(f"### Local chunk {index}{window}\n{body}")
        return "\n\n".join(parts)

    @staticmethod
    def _dialectic_inputs(store: MinirStore, session: str) -> tuple[str, str]:
        """Historical conclusions + the tail window's conclusions.

        Historical = durable cards and constraints accumulated across sessions. Tail = the
        current session's latest review and current-focus representation. The canonical
        ``session_chunk_summary`` chain is passed separately by ``compress()``.
        """
        historical_parts: list[str] = []
        for kind, label in (
                ("base.user_peer_card", "User peer card"),
                ("base.ai_identity_card", "AI identity card"),
                ("dialectic.constraints", "Constraints"),
                ("dialectic.preferences", "Preferences"),
                ("dialectic.second_brain", "Second brain")):
            row = store.newest_conclusion(kind)
            body = str((row or {}).get("content") or "").strip()
            if body:
                historical_parts.append(f"## {label}\n{body}")
        tail_parts: list[str] = []
        for kind, label in (
                ("dialectic.review", "Tail-window adviser review"),
                ("base.user_representation", "Tail-window user representation"),
                ("base.value_markers", "Tail-window value markers")):
            row = store.newest_conclusion(kind, session_id=session)
            body = str((row or {}).get("content") or "").strip()
            if body:
                tail_parts.append(f"## {label}\n{body}")
        return "\n\n".join(historical_parts), "\n\n".join(tail_parts)

    def _memory_store(self) -> Optional[MinirStore]:
        """The plugin's own store, opened lazily (the engine can run without the provider)."""
        with self._lock:
            if self._store_checked:
                return self._store
            self._store_checked = True
            path = cadence.store_path(self._hermes_home)
            if path is None:
                return None
            try:
                self._store = MinirStore(path)
            except Exception as exc:
                logger.debug("Minir engine store unavailable: %s", exc)
                self._store = None
            return self._store

    def _enqueue_dialectic(self, store: MinirStore, to_ts: float, turns: int) -> None:
        """Queue the dialectic window that ends at the compression decision, once."""
        session = self._session_id
        if not session or store.has_open_job(DIALECTIC_LAYER, session):
            return
        start = store.last_processed_ts(DIALECTIC_LAYER, session)
        if to_ts <= start:
            return
        job_id = store.record_job(DIALECTIC_LAYER, session, start, float(to_ts), turns=int(turns))
        logger.info("Minir compression queued dialectic job #%s for session=%s", job_id, session)

    def _state_db_path(self) -> Optional[Path]:
        if self._state_db_override:
            return Path(self._state_db_override)
        return Path(self._hermes_home) / "state.db" if self._hermes_home else None

    def _read(self, sql: str, params: tuple) -> list[tuple]:
        """One read-only query against the host's store; ``[]`` on any problem."""
        path = self._state_db_path()
        if path is None or not path.exists():
            return []
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)
            try:
                return connection.execute(sql, params).fetchall()
            finally:
                connection.close()
        except Exception as exc:
            logger.debug("Minir store read failed: %s", exc)
            return []

    def _fetch(self, uids: list[str]) -> list[dict[str, Any]]:
        """Read the originals for *uids* from the host's own store (read-only)."""
        wanted = list(dict.fromkeys(uids))[:_MAX_FETCHED]
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        rows = self._read(
            f"SELECT message_uid, role, content, timestamp FROM messages "
            f"WHERE message_uid IN ({marks}) ORDER BY id", tuple(wanted))
        return [{"uid": uid, "role": role, "text": content, "when": timestamp}
                for uid, role, content, timestamp in rows
                if isinstance(content, str) and content.strip()]

    def _match(self, uids: list[str], terms: list[str], *, limit: int) -> list[dict[str, Any]]:
        """Rank folded originals against *terms*; ``[]`` when nothing scores."""
        if not uids or not terms:
            return []
        scored = []
        for row in self._fetch(uids):
            lowered = row["text"].lower()
            score = sum(1 for term in terms if term in lowered)
            if score:
                scored.append({**row, "score": score})
        scored.sort(key=lambda row: (-row["score"], row.get("when") or 0))
        return scored[:max(1, limit)]

    def _recall_block(self, uids: list[str], terms: list[str]) -> str:
        hits = self._match(uids, terms, limit=_MAX_RECALL_MESSAGES)
        if not hits:
            return ""
        lines = ["[minir context recall — earlier turns folded out of this window, restored verbatim]"]
        size = len(lines[0])
        for hit in hits:
            line = f"[{hit['role']}] {' '.join(hit['text'].split())}"
            if size + len(line) > _MAX_RECALL_CHARS:
                lines.append("… more available via minir_context_recall")
                break
            size += len(line) + 1
            lines.append(line)
        return "\n".join(lines)

    def _folded_uids_from_store(self) -> list[str]:
        """Every recoverable uid in this session's carriers, read back from the store.

        Used by the tool when the caller has no message list to hand: the carriers are the durable
        index, so no separate index has to be kept.
        """
        if not self._session_id:
            return []
        rows = self._read(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' AND content LIKE ? "
            "ORDER BY id DESC LIMIT 50", (self._session_id, f"{CARRIER_MARKER}%"))
        uids: list[str] = []
        for (content,) in rows:
            parsed = parse_carrier({"role": "user", "content": content})
            if parsed:
                uids.extend(parsed["uids"])
        return list(dict.fromkeys(uids))
