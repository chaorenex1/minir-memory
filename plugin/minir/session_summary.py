"""The end-of-session summary agent: hand-off first, one auditable summary out.

One session ends, the operator may or may not come back, and a day later the session is treated
as finished. This module is the summary half of that finalisation. It runs on the same
``auxiliary.adviser`` agent route as the layer passes, with this plugin's toolset attached
(``minir`` + ``adviser``), so the summarizer can read the session with
``minir_hermes_session_search`` and the store with ``minir_search``.

The **session hand-off** — the audited Task Handoff produced by :mod:`handoff` — is the first
input of the user message. The agent reads it, checks it against the session when a claim needs
evidence, and writes the durable summary that outlives the session:

    ## Session Goal / ## Work Done / ## Decisions / ## Durable Knowledge / ## Open Threads

Only a summary carrying those headings in order is accepted; anything else is rejected rather
than stored, so a malformed reply cannot become the session's final record.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import agent_loop, layers, tools
from .prompting import data_block

logger = logging.getLogger(__name__)

TASK_KEY = "minir_session_summary"
MAX_CHARS = 6_000
HEADINGS: tuple[str, ...] = (
    "## Session Goal", "## Work Done", "## Decisions", "## Durable Knowledge", "## Open Threads",
)

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")

_OUTPUT_RULE = (
    "OUTPUT\nReturn only the five-heading summary described in the system prompt: no preamble, "
    "no closing remark, no code fence.")


def system_prompt() -> str:
    return f"{layers.load_core('session_summary')}\n\n{_OUTPUT_RULE}"


def normalize(text: str) -> str:
    """Accept only a summary with all five headings in order; strip fences and thinking."""
    candidate = _THINK_RE.sub("", text or "").strip()
    candidate = _FENCE_RE.sub("", candidate).strip()
    if not candidate:
        return ""
    positions = [candidate.find(heading) for heading in HEADINGS]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        return ""
    return candidate[:MAX_CHARS].rstrip()


def build_prompt(*, session_id: str, handoff: str, markers: Any, usage: Any,
                 window_end: float = 0.0) -> str:
    """The user message: the hand-off and the derived evidence, then the instruction."""
    before = f"{float(window_end):.6f}" if window_end else "the supplied window end"
    parts = [
        f"SESSION: {session_id or '-'}",
        f"WINDOW END (never read after): {before}",
        "SESSION HANDOFF (audited compression record; derived data):\n"
        + data_block("session_handoff", (handoff or "").strip()[:8_000],
                     empty="(none — read the session itself)"),
        "VALUE MARKERS (derived data):\n"
        + data_block("value_markers",
                     json.dumps(markers if markers is not None else {}, ensure_ascii=False)[:4_000]),
        "TOKEN USAGE (derived data):\n"
        + data_block("token_usage",
                     json.dumps(usage if usage is not None else {}, ensure_ascii=False)[:2_000]),
        "Read the session with minir_hermes_session_search (session_id, before as above, "
        "include_inactive=true, include_compacted=true) when the hand-off or markers leave a "
        "claim unproven. Then write the final summary.",
    ]
    return "\n\n".join(parts)


def summarize_agent(*, session_id: str, handoff: str, markers: Any = None, usage: Any = None,
                    window_end: float = 0.0, timeout: float = 120.0) -> str:
    """Run the summary agent; returns a validated summary or ``""`` (never raises)."""
    prompt = build_prompt(session_id=session_id, handoff=handoff, markers=markers,
                          usage=usage, window_end=window_end)

    def run() -> str:
        from . import adviser

        cfg = adviser.load_config()
        result = agent_loop.run(
            prompt, model=str(cfg.get("model") or ""),
            provider=str(cfg.get("provider") or ""),
            api_mode=str(cfg.get("api_mode") or ""),
            toolsets=tools.agent_toolsets(cfg.get("toolsets")),
            system_message=system_prompt(),
            layer="session_summary", history_session=session_id)
        return str(result.answer or "")

    try:
        from .handoff import run_with_timeout

        return normalize(run_with_timeout(run, timeout) or "")
    except Exception as exc:  # a summary failure must not fail the extraction
        logger.warning("Minir session summary agent failed: %s", exc)
        return ""
