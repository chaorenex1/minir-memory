"""Compression as an audited summary fold.

The engine no longer maintains a progressive carrier chain. It waits for the host's compression
boundary, folds the unprotected middle of the window into one **assistant summary message**, and
returns ``head + notice + summary + tail``. The summary is produced by a real adviser agent run
that audits the base layer's prepared chunk and the base revision note against history up to the
window end; raw transcript text stays in the host's ``state.db`` and is never copied into this
provider's store.

``on_pre_compress`` (provider side) pauses the periodic layer scheduler, runs the base and
dependent dialectic jobs synchronously, resets both counters, and then calls ``compress()``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Iterable, Optional

from agent.context_engine import ContextEngine

from . import cadence, handoff
from .store import MinirStore

logger = logging.getLogger(__name__)

ENGINE_NAME = "minir"

SUMMARY_MARKER = "[minir summary"
NOTICE_MARKER = "[minir memory notice"
SUMMARY_RE = re.compile(r"\[minir summary #([0-9A-Za-z_.-]+)\]")
NOTICE_TEXT = (
    f"{NOTICE_MARKER}] Earlier turns in this session were compressed into the assistant summary "
    "below. Treat that summary as a derived record, not a live reply: it may be incomplete or "
    "stale, and the current conversation wins on conflict. Use minir_search or "
    "adviser_session_search to recover exact details.")

_MAX_FOLD_MESSAGES = 24
_MAX_SUMMARY_CHARS = 8_000
_ELIDED_TOOL_MARKER = "[tool result elided by minir · recoverable from the session store]"
_TERM_RE = re.compile(r"[\w-]{3,}", re.UNICODE)
_STOPWORDS = frozenset("""
about after again against all also and any are because been before being between both but can
could did does doing down during each even ever every few for from further had has have having
here how into its itself just like made make many more most much must not now off once only other
over own same should some such than that the their them then there these they this those through
too under until very was were what when where which while who whom why will with would you your
""".split())


def _terms(text: str, limit: int = 12) -> list[str]:
    """Lowercased search terms, order kept, deduped, stopwords dropped."""
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
        return "\n".join(part["text"] for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def foldable_slice(messages: list[dict[str, Any]], protect_first_n: int, protect_last_n: int, *,
                   focus_topic: Optional[str] = None) -> Optional[tuple[int, int]]:
    """The half-open ``[start, end)`` middle this engine may replace with a summary.

    Rules: never touch the leading system prompt (or the ``protect_first_n`` messages after it),
    never touch the ``protect_last_n`` messages at the tail, never remove the newest user-authored
    turn, and always end where the next user-role row begins so the retained tail opens on a user
    turn and no assistant/tool group is split.
    """
    if not isinstance(messages, list) or len(messages) < 3:
        return None
    head = 1 if messages[0].get("role") == "system" else 0
    floor = head + max(0, int(protect_first_n))
    ceiling = len(messages) - max(0, int(protect_last_n))
    starts = [index for index in range(head, len(messages))
              if messages[index].get("role") == "user"]
    if not starts:
        return None
    ceiling = min(ceiling, starts[-1])
    inside = [index for index in starts if floor <= index <= ceiling]
    if len(inside) < 2:  # at least one whole turn must move
        return None
    start = inside[0]
    end = start
    for index in inside[1:]:
        if index - start > _MAX_FOLD_MESSAGES:
            break
        end = index
    if end <= start:
        return None
    if focus_topic:
        terms = _terms(focus_topic)
        while terms and end > start and any(term in _text_of(messages[end - 1]).lower()
                                            for term in terms):
            previous = max((index for index in inside if index < end), default=start)
            if previous <= start:
                return None
            end = previous
    return start, end


def _is_summary_message(message: Any) -> bool:
    return isinstance(message, dict) and message.get("role") == "assistant" \
        and SUMMARY_MARKER in _text_of(message)


def _last_index(messages: list[dict[str, Any]], role: str) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], dict) and messages[index].get("role") == role:
            return index
    return -1


class MinirContextEngine(ContextEngine):
    """Audited summary compression for the host's context window."""

    emit_automatic_compaction_status = False

    def __init__(self, *, hermes_home: str = "", state_db: str = "",
                 recall: bool = True, interval: int = 0, **kwargs: Any) -> None:
        # ``recall``/``interval`` are accepted for compatibility with older engine factories;
        # this engine no longer keeps a folded-uid index or a fold beat. Details are recovered
        # through the search surfaces.
        self._hermes_home = str(hermes_home or "")
        self._state_db_override = str(state_db or "")
        self._session_id = ""
        self._lock = threading.RLock()
        self._store: Optional[MinirStore] = None
        self._store_checked = False
        self._summary_enabled = (cadence.configured_handoff("")
                                 and cadence.configured_reasoning(""))
        self.compression_count = 0

    @property
    def name(self) -> str:
        return ENGINE_NAME

    # -- lifecycle ------------------------------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        with self._lock:
            self._session_id = str(session_id or "")
        if kwargs.get("hermes_home"):
            self._hermes_home = str(kwargs["hermes_home"])
        self._summary_enabled = (cadence.configured_handoff(self._hermes_home)
                                 and cadence.configured_reasoning(self._hermes_home))

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self.on_session_reset()

    def on_session_reset(self) -> None:
        with self._lock:
            session = self._session_id
            self._session_id = ""
            self.compression_count = 0
        if session:
            store = self._memory_store()
            if store is not None:
                store.clear_compression_gate(session)
        super().on_session_reset()

    def on_turn_complete(self, messages: list[dict[str, Any]],
                         usage: Optional[dict[str, Any]] = None, **kwargs: Any) -> None:
        """Promote the summary run whose marker is present in the committed message list."""
        if not isinstance(messages, list) or not self._session_id:
            return
        store = self._memory_store()
        if store is None:
            return
        run_ids = self.summary_run_ids(messages)
        for run_id in run_ids:
            store.promote_summary_run(self._session_id, run_id)
        if run_ids:
            store.clear_compression_gate(self._session_id)

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Track token usage after every LLM call (the host reads these fields directly)."""
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

    # -- trigger --------------------------------------------------------------

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """One trigger: the window has consumed 95% of the configured threshold."""
        if not self.threshold_tokens:
            return False
        tokens = self.last_prompt_tokens if prompt_tokens is None else int(prompt_tokens)
        return tokens >= int(self.threshold_tokens * 0.95)

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        """Defer to real usage whenever the last completed request fit under the threshold."""
        return bool(self.last_prompt_tokens) and bool(self.threshold_tokens) \
            and self.last_prompt_tokens < self.threshold_tokens

    def has_content_to_compress(self, messages: list[dict[str, Any]]) -> bool:
        """False reports "nothing foldable yet" without touching the model."""
        return foldable_slice(messages, self.protect_first_n, self.protect_last_n) is not None

    # -- compression ----------------------------------------------------------

    def compress(
        self, messages: list[dict[str, Any]], current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",
    ) -> list[dict[str, Any]]:
        """Fold the middle into one audited assistant summary and return head/notice/summary/tail.

        The summary agent runs synchronously; when it fails or times out the prepared chunk and
        review are used as the summary so the window still shrinks. Never raises.
        """
        store = self._memory_store()
        session = self._session_id
        try:
            bounds = foldable_slice(messages, self.protect_first_n, self.protect_last_n,
                                    focus_topic=focus_topic)
            if bounds is None:
                return messages
            start, end = bounds
            had_summary = self.has_summary_marker(messages)
            # A previous summary is an assistant row and sits in the protected head on the next
            # fold. Remove it from head/tail so repeated compression keeps exactly one summary.
            head = [message for message in messages[:start]
                    if not self.is_summary_message(message)]
            tail = [message for message in messages[end:]
                    if not self.is_summary_message(message)]
            chunk, review, previous = self._prepared_inputs(store, session)
            previous_message = self._previous_summary(messages)
            if previous_message is not None:
                # The previous summary may still be pending (its carrier was never promoted),
                # so the store cannot be the only source for the agent's continuity.
                previous = self._summary_body(previous_message) or previous
            summary = ""
            if self._summary_enabled:
                summary = handoff.summarize_agent(
                    chunk, review, previous=previous, session_id=session,
                    before=time.time(), focus_topic=focus_topic or "",
                    memory_context=memory_context or "")
            if not summary:
                summary = self._fallback_summary(chunk, review, previous)
            summary = summary[:_MAX_SUMMARY_CHARS].rstrip()
            run_id = uuid.uuid4().hex[:12]
            if store is not None and session:
                store.expire_pending_handoffs(session, before=time.time() - 3_600)
                key = f"session_summary:{session}:{run_id}"
                store.put_conclusion(
                    "session_summary", key, summary, session_id=session, subject="session",
                    evidence=json.dumps([run_id]), status="pending")
            if not had_summary:
                head = self._insert_notice(head)
            summary_message = {
                "role": "assistant",
                "content": f"{SUMMARY_MARKER} #{run_id}]\n{summary}",
                "message_uid": uuid.uuid4().hex,
            }
            with self._lock:
                self.compression_count += 1
            result = [*head, summary_message, *tail]
            logger.info("Minir compression: %d messages -> summary run %s, %d left verbatim",
                        len(messages) - len(result) + 1, run_id, len(result))
            return result
        except Exception as exc:  # fail-open: an unusable engine leaves the window alone
            logger.warning("Minir compression failed, leaving the window unchanged: %s", exc)
            return messages
        finally:
            if store is not None and session:
                store.clear_compression_gate(session)

    def prune_tool_results_only(
        self, messages: list[dict[str, Any]], current_tokens: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Replace old tool-result payloads with a marker; the originals stay in the store."""
        try:
            last_user = _last_index(messages, "user")
            cut = max(0, len(messages) - self.protect_last_n)
            pruned = 0
            for index, message in enumerate(messages[:cut]):
                if not isinstance(message, dict) or message.get("role") != "tool" \
                        or index >= last_user:
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

    # -- summary helpers ------------------------------------------------------

    @staticmethod
    def is_summary_message(message: Any) -> bool:
        return _is_summary_message(message)

    @staticmethod
    def _previous_summary(messages: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
        previous: Optional[dict[str, Any]] = None
        for message in messages or ():
            if _is_summary_message(message):
                previous = message
        return previous

    @staticmethod
    def _summary_body(message: Any) -> str:
        text = _text_of(message)
        newline = text.find("\n")
        return text[newline + 1:].strip() if newline >= 0 else ""

    @staticmethod
    def has_summary_marker(messages: Iterable[dict[str, Any]]) -> bool:
        return any(_is_summary_message(message) for message in messages or ())

    @staticmethod
    def summary_run_ids(messages: Iterable[dict[str, Any]]) -> list[str]:
        run_ids: list[str] = []
        for message in messages or ():
            if _is_summary_message(message):
                run_ids.extend(SUMMARY_RE.findall(_text_of(message)))
        return list(dict.fromkeys(run_ids))

    @staticmethod
    def _insert_notice(head: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if any(NOTICE_MARKER in _text_of(message) for message in head):
            return head
        index = 0
        while index < len(head) and head[index].get("role") == "system":
            index += 1
        return [*head[:index], {"role": "system", "content": NOTICE_TEXT}, *head[index:]]

    @staticmethod
    def _prepared_inputs(store: Optional[MinirStore], session: str) -> tuple[str, str, str]:
        if store is None or not session:
            return "", "", ""
        chunks = store.chunk_summaries(session)
        chunk = str(chunks[-1].get("content") or "").strip() if chunks else ""
        review_row = store.newest_conclusion("dialectic.review", session_id=session)
        previous_row = store.newest_conclusion("session_summary", session_id=session)
        return (chunk,
                str((review_row or {}).get("content") or "").strip(),
                str((previous_row or {}).get("content") or "").strip())

    @staticmethod
    def _fallback_summary(chunk: str, review: str, previous: str) -> str:
        for candidate in (chunk, review, previous):
            if candidate.strip():
                return candidate.strip()
        return ("## Goal\n- N/A\n\n## Current State\n- N/A\n\n"
                "## Next Steps\n- N/A\n\n## Critical Context\n- No summary was generated.")

    # -- status ---------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        return {**super().get_status(), "engine": ENGINE_NAME,
                "compressions": self.compression_count}

    def clone_for_agent(self) -> "MinirContextEngine":
        """Fresh engine for another agent: same profile/config, no session state."""
        return MinirContextEngine(hermes_home=self._hermes_home,
                                  state_db=self._state_db_override)

    # -- store ----------------------------------------------------------------

    def _memory_store(self) -> Optional[MinirStore]:
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
