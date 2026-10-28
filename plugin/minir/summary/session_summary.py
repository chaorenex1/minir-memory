"""The end-of-session summary agent: the session's final record.

One session ends, the operator may or may not come back, and a day later the session is treated
as finished. This module is the summary half of that finalisation. It runs on the same
``auxiliary.adviser`` agent route as the layer passes, with this plugin's toolset attached
(``minir`` + ``adviser``), so the summarizer can read the session with
``minir_hermes_session_search`` and the store with ``minir_search``.

The shape follows what a finished session can fill, not the live window's eight sections:
``templates/session_summary.md`` is the shape asset, ``TEMPLATE:`` carries it into the system
prompt, and nothing validates the reply against it. A template is a hint, not a gate: a missing
section, a reordered section or an extra one is stored as written.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..agentrun import agent_loop
from ..passes import layers
from ..runtime import tools
from ..prompting.prompting import data_block

logger = logging.getLogger(__name__)

MAX_CHARS = 6_000
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "session_summary.md"

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")

_OUTPUT_RULE = (
    "OUTPUT\nReturn only the session summary described in the system prompt: the sections from "
    "TEMPLATE in template order, no preamble, no closing remark, no code fence.")


class SessionTemplateError(RuntimeError):
    """The template asset is missing or unreadable.

    Deliberately not a malformed-reply error: a broken asset is an assembly failure, and no
    format retry can repair it.
    """


def load_template() -> str:
    """The template asset, read at call time; a missing file fails the pass."""
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise SessionTemplateError(
            f"cannot read the session summary template {TEMPLATE_PATH}: {exc}") from exc


def system_prompt() -> str:
    return (f"{layers.load_core('session_summary')}\n\nTEMPLATE:\n{load_template()}"
            f"\n\n{_OUTPUT_RULE}")


def normalize(text: str) -> str:
    """Clean the reply: thinking and fences, whitespace, length. Shape is never checked."""
    candidate = _THINK_RE.sub("", text or "").strip()
    candidate = _FENCE_RE.sub("", candidate).strip()
    if not candidate:
        return ""
    return candidate[:MAX_CHARS].rstrip()


def build_prompt(*, session_id: str, session_record: str, markers: Any, usage: Any,
                 window_end: float = 0.0) -> str:
    """The user message: the compression record and the derived evidence, then the instruction."""
    before = f"{float(window_end):.6f}" if window_end else "the supplied window end"
    parts = [
        f"SESSION: {session_id or '-'}",
        f"WINDOW END (never read after): {before}",
        "SESSION RECORD (the latest compression summary; derived data):\n"
        + data_block("session_record", (session_record or "").strip()[:8_000],
                     empty="(none — read the session itself)"),
        "VALUE MARKERS (derived data):\n"
        + data_block("value_markers",
                     json.dumps(markers if markers is not None else {}, ensure_ascii=False)[:4_000]),
        "TOKEN USAGE (derived data):\n"
        + data_block("token_usage",
                     json.dumps(usage if usage is not None else {}, ensure_ascii=False)[:2_000]),
        "Read the session with minir_hermes_session_search (session_id, before as above, "
        "include_inactive=true, include_compacted=true) when the record or markers leave a "
        "claim unproven. Then write the final summary.",
    ]
    return "\n\n".join(parts)


def summarize_agent(*, session_id: str, session_record: str, markers: Any = None,
                    usage: Any = None, window_end: float = 0.0,
                    timeout: float = 120.0) -> str:
    """Run the summary agent; returns the cleaned summary or ``""`` (never raises)."""
    prompt = build_prompt(session_id=session_id, session_record=session_record, markers=markers,
                          usage=usage, window_end=window_end)

    def run() -> str:
        from ..adviser import adviser

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
        return normalize(agent_loop.run_with_timeout(run, timeout) or "")
    except Exception as exc:  # a summary failure must not fail the extraction
        logger.warning("Minir session summary agent failed: %s", exc)
        return ""
