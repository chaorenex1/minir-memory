"""Idle-session knowledge extraction and the due-row runner the unified clock polls.

A session that ends is not necessarily finished: the operator can resume it. This module waits
``knowledge_idle_hours`` (24 by default) after the session's last activity and only then treats
it as closed. If the operator types in that session again, the pending extraction is cancelled;
nothing is extracted from a live conversation.

When the timer fires, the provider's callback runs two agents in order:

1. :mod:`session_summary` — the Task Handoff produced by :mod:`handoff` is the first input of
   its user message, and it writes the session's final summary;
2. :func:`extract_pages` here — the summary, hand-off, value markers and token usage go in, and
   the agent returns knowledge pages with a body each. The caller writes them through
   :func:`minir.knowledge.write_unit`, so the page budget and atomic-write rules still apply.

The unified scheduler polls it on a wall-clock cadence, not a turn counter: it must fire with
no live session. Rows live in SQLite, so a timer set in one process is still honoured after a
restart; a claim marks a row ``running`` and a failed run is retried once after an hour before
it is parked as ``failed``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Optional

from . import agent_loop, layers, tools
from .prompting import data_block

logger = logging.getLogger(__name__)

IDLE_SECONDS = 24 * 3600
TICK_SECONDS = 60.0
RETRY_SECONDS = 3600.0
MAX_PAGES = 12
MAX_PAGE_CHARS = 6_000
MAX_PAGE_TOPIC = 80
MAX_OBJECTS = 5
MAX_RELATIONS = 3
MAX_SUMMARY = 160

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
_H1_RE = re.compile(r"^#\s+.*?(?:\r?\n|$)")

_OUTPUT_RULE = (
    "OUTPUT\nReturn only the JSON object described in the system prompt: no preamble, no code "
    "fence, no prose.")


def system_prompt() -> str:
    return f"{layers.load_core('knowledge_extract')}"


def _flatten(text: Any) -> str:
    return " ".join(str(text or "").split())


def _clean_body(text: Any) -> str:
    """Page body without a leading H1 (the renderer adds the title itself)."""
    body = str(text or "").strip()
    return _H1_RE.sub("", body, count=1).strip() if body.lstrip().startswith("# ") else body


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for entry in value:
        text = _flatten(entry)
        if text and text not in items:
            items.append(text[:200])
        if len(items) >= limit:
            break
    return items


def _relations(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "").strip().lower()
        target = _flatten(entry.get("target"))
        if kind in layers.RELATION_TYPES and target:
            rows.append({"type": kind, "target": target[:200]})
        if len(rows) >= MAX_RELATIONS:
            break
    return rows


def validate_pages(payload: Any) -> list[dict[str, Any]]:
    """Normalize the agent's page array; invalid entries are dropped, never repaired in place."""
    if isinstance(payload, dict):
        entries = payload.get("pages")
        if entries is None:
            entries = payload.get("units")
    elif isinstance(payload, list):
        entries = payload
    else:
        return []
    if not isinstance(entries, list):
        return []
    pages: list[dict[str, Any]] = []
    for entry in entries[:MAX_PAGES]:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "").strip().upper()
        org = str(entry.get("org") or "").strip()
        topic = _flatten(entry.get("topic"))[:MAX_PAGE_TOPIC]
        body = _clean_body(entry.get("content") or entry.get("body"))
        if category not in layers.CATEGORIES or org not in layers.ORGS:
            continue
        if not topic or not body:
            continue
        epistemic = str(entry.get("epistemic") or "").strip().lower()
        pages.append({
            "category": category,
            "org": org,
            "topic": topic,
            "content": body[:MAX_PAGE_CHARS],
            "epistemic": epistemic if epistemic in layers.EPISTEMIC else "",
            "time": _flatten(entry.get("time"))[:64],
            "objects": _string_list(entry.get("objects"), limit=MAX_OBJECTS),
            "relations": _relations(entry.get("relations")),
            "summary": _flatten(entry.get("summary"))[:MAX_SUMMARY],
        })
    return pages


def normalize_pages(text: str) -> list[dict[str, Any]]:
    """Parse one agent reply into pages; ``[]`` on anything that is not the agreed object."""
    candidate = _THINK_RE.sub("", text or "").strip()
    candidate = _FENCE_RE.sub("", candidate).strip()
    if not candidate:
        return []
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError):
        return []
    return validate_pages(payload)


def build_prompt(*, session_id: str, summary: str, handoff: str, markers: Any,
                 usage: Any) -> str:
    parts = [
        f"SESSION: {session_id or '-'}",
        "SESSION SUMMARY (derived data; the session's final record):\n"
        + data_block("session_summary", (summary or "").strip()[:6_000], empty="(none)"),
        "SESSION HANDOFF (audited compression record; derived data):\n"
        + data_block("session_handoff", (handoff or "").strip()[:8_000], empty="(none)"),
        "VALUE MARKERS (derived data):\n"
        + data_block("value_markers",
                     json.dumps(markers if markers is not None else {}, ensure_ascii=False)[:4_000]),
        "TOKEN USAGE (derived data):\n"
        + data_block("token_usage",
                     json.dumps(usage if usage is not None else {}, ensure_ascii=False)[:2_000]),
        "Read the session with adviser_session_search when a page needs evidence, then extract "
        "the durable knowledge pages. Return the JSON object.",
    ]
    return "\n\n".join(parts)


def extract_pages(*, session_id: str, summary: str, handoff: str, markers: Any = None,
                  usage: Any = None, timeout: float = 120.0) -> list[dict[str, Any]]:
    """Run the extraction agent; returns validated pages (``[]`` on any failure)."""
    prompt = build_prompt(session_id=session_id, summary=summary, handoff=handoff,
                          markers=markers, usage=usage)

    def run() -> str:
        from . import adviser

        cfg = adviser.load_config()
        result = agent_loop.run(
            prompt, model=str(cfg.get("model") or ""),
            provider=str(cfg.get("provider") or ""),
            api_mode=str(cfg.get("api_mode") or ""),
            toolsets=tools.agent_toolsets(cfg.get("toolsets")),
            system_message=f"{system_prompt()}\n\n{_OUTPUT_RULE}")
        return str(result.answer or "")

    try:
        from .handoff import run_with_timeout

        return normalize_pages(run_with_timeout(run, timeout) or "")
    except Exception as exc:  # extraction failure is reported by the caller, never raised
        logger.warning("Minir knowledge extraction agent failed: %s", exc)
        return []


class ExtractionScheduler:
    """Claims and runs the sessions whose idle timer has fired (polled by the unified clock)."""

    def __init__(self, store: Any, callback: Callable[[dict[str, Any]], Any]):
        self._store = store
        self._callback = callback

    def tick(self, *, now: Optional[float] = None) -> int:
        """Claim and run every due row (bounded); returns how many completed."""
        stamp = time.time() if now is None else float(now)
        completed = 0
        for row in self._store.due_extractions(stamp, limit=3):
            session = str(row.get("session_id") or "")
            if not session or not self._store.claim_extraction(session, now=stamp):
                continue
            try:
                result = self._callback(row) or {}
            except Exception as exc:
                status = self._store.fail_extraction(
                    session, f"{type(exc).__name__}: {exc}", retry_at=stamp + RETRY_SECONDS)
                logger.warning("Minir extraction for %s %s: %s", session, status, exc)
                continue
            self._store.finish_extraction(
                session, pages=int(result.get("pages") or 0),
                summary_chars=int(result.get("summary_chars") or 0))
            completed += 1
            logger.info("Minir extraction done for %s: %s page(s), %s summary chars",
                        session, result.get("pages", 0), result.get("summary_chars", 0))
        return completed
