"""Transcript digest: one plain-text projection per pass, one renderer.

Each layer decides *which* events it needs and how much of a tool result it can afford; the
rendering is shared so the prompt side never parses JSON and the budget is one number. Lines are
data, never instructions:

    [user t3] change the deployment tag
    [assistant t3 s1] reading the manifest first
    [tool t3 s1 exec_command ok 1.2s chars=18432] head…tail (elided, 18200 chars)
    [reasoning t3] … (elided, 3120 chars)
    [coverage] seq 1204..1892 of this session; 12 of 14 event(s) shown; source=buffer

Rules (doc/transcript-jsonl-draft.md §9):

* every elision carries ``elided`` and the original character count;
* the block ends with a coverage line and never claims to be the whole session;
* ``max_chars`` is a hard budget: whole oldest events are dropped, never half a line;
* a pass with no rows gets an empty string, and its builder omits the block entirely.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

#: One budget for the prompt text and the renderer. A pass that declares a different number here
#: and in its prompt asset has two contracts.
DIGEST_MAX_CHARS = 4_000
DIGEST_MAX_EVENTS = 200

#: Per-pass projection defaults (§9.2 of the draft). Tool results are the expensive role: a card
#: pass takes tool names only, an evidence pass takes them head/tail trimmed.
PRESETS: dict[str, dict[str, Any]] = {
    "base_user": {"roles": ("user", "assistant"), "include_tool_results": False},
    "base_ai": {"roles": ("user", "assistant"), "include_tool_results": False},
    "window_summary": {"roles": ("user", "system", "assistant", "tool"),
                       "include_tool_results": True, "tool_result_chars": 400},
    "value_markers": {"roles": ("user", "assistant"), "include_tool_results": False},
    "dialectic": {"roles": ("user", "assistant", "tool"), "include_tool_results": True},
    "card_review": {"roles": ("user", "assistant"), "include_tool_results": False},
    "extraction": {"roles": ("user",), "include_tool_results": False},
    "adviser": {"roles": ("user", "system", "assistant", "tool", "reasoning"),
                "include_tool_results": True, "include_reasoning": True},
    "minir_agents": {"roles": ("user", "assistant", "tool"), "include_tool_results": True},
}

#: One unique block name per pass; tests assert on these, not on prose inside the block.
BLOCK_NAMES: dict[str, str] = {
    "base_user": "base_user_transcript",
    "base_ai": "base_ai_transcript",
    "window_summary": "window_summary_transcript",
    "value_markers": "value_markers_transcript",
    "dialectic": "dialectic_transcript",
    "card_review": "card_review_transcript",
    "extraction": "extraction_transcript",
    "adviser": "session_history",
    "minir_agents": "agent_transcript",
}


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def _head_tail(text: str, limit: int) -> str:
    """Keep a tool result's head and tail inside *limit* characters, marked with the original."""
    body = str(text or "")
    if len(body) <= limit:
        return _flat(body)
    if limit <= 24:
        return f"(elided, {len(body)} chars)"
    head = max(1, (limit - 24) * 2 // 3)
    tail = max(1, limit - 24 - head)
    return f"{_flat(body[:head])}…{_flat(body[-tail:])} (elided, {len(body)} chars)"


def _cut(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    keep = max(0, limit - 28)
    return f"{line[:keep]}… (elided, {len(line)} chars)"


def _label(row: dict[str, Any]) -> str:
    turn = row.get("turn")
    step = row.get("step")
    role = str(row.get("role") or "")
    if row.get("kind") == "gap":
        return f"[gap dropped={row.get('dropped', 0)} reason={row.get('reason') or 'unknown'}]"
    if role in {"user", "assistant"}:
        suffix = f" t{turn}" if turn not in (None, 0) else ""
        if role == "assistant" and step:
            suffix += f" s{step}"
        return f"[{role}{suffix}]"
    if role == "tool":
        name = str(row.get("name") or "tool")
        status = "ok" if row.get("ok", True) else "error"
        duration = row.get("duration_ms")
        timing = f" {float(duration) / 1000.0:.1f}s" if isinstance(duration, (int, float)) else ""
        size = len(str(row.get("result") or ""))
        return f"[tool t{turn}{'' if not step else f' s{step}'} {name} {status}{timing} chars={size}]"
    if role == "reasoning":
        return f"[reasoning t{turn}]" if turn not in (None, 0) else "[reasoning]"
    if role == "system":
        return "[system]"
    return f"[{role or 'event'}]"


def format_row(row: dict[str, Any], *, include_tool_results: bool = False,
               tool_result_chars: int = 400, include_reasoning: bool = False) -> str:
    """One event as one prompt line. Callers have already filtered by role."""
    label = _label(row)
    role = str(row.get("role") or "")
    if row.get("kind") == "gap":
        return label
    if role == "tool":
        result = str(row.get("result") or "")
        body = (_head_tail(result, tool_result_chars) if include_tool_results and result
                else "(result elided)")
        return f"{label} {body}".rstrip()
    if role == "reasoning":
        text = _flat(row.get("text"))
        if not include_reasoning:
            return f"{label} (elided, {len(text)} chars)"
        return f"{label} {_head_tail(text, 200)}"
    text = _flat(row.get("text"))
    calls = row.get("tool_calls")
    if role == "assistant" and isinstance(calls, list) and calls:
        names = ",".join(str(call.get("name") or "") for call in calls if isinstance(call, dict))
        if names:
            text = f"{text} calls={names}".strip()
    return f"{label} {text}".rstrip()


def render_digest(buffer: Any, profile: str, layer: str, session_id: str, *,
                  roles: Iterable[str] = ("user", "assistant"),
                  include_tool_results: bool = False, tool_result_chars: int = 400,
                  include_reasoning: bool = False,
                  max_chars: int = DIGEST_MAX_CHARS, max_events: int = DIGEST_MAX_EVENTS,
                  turn: Any = None, step: Any = None,
                  since: Optional[float] = None, until: Optional[float] = None) -> str:
    """Render one stream's digest, oldest first, within a hard character/event budget."""
    wanted = {str(role) for role in roles}
    if include_reasoning:
        wanted.add("reasoning")
    result = buffer.read_events(profile, layer, session_id, since=since, until=until,
                                turn=turn, step=step, kinds=("msg", "gap"))
    rows = [row for row in result.get("rows") or ()
            if str(row.get("role") or "") in wanted or row.get("kind") == "gap"]
    if not rows:
        return ""
    lines = [format_row(row, include_tool_results=include_tool_results,
                        tool_result_chars=tool_result_chars,
                        include_reasoning=include_reasoning)
             for row in rows]
    kept: list[str] = []
    used = 0
    budget = max(1, int(max_chars))
    events = max(1, int(max_events))
    for line in reversed(lines):
        cost = len(line) + 1
        if kept and (used + cost > budget or len(kept) >= events):
            break
        if not kept and cost > budget:
            line = _cut(line, budget)
            cost = len(line) + 1
        kept.append(line)
        used += cost
    kept.reverse()
    dropped = len(lines) - len(kept)
    seqs = [int(row.get("seq") or 0) for row in rows]
    coverage = result.get("coverage") or (0, 0)
    source = result.get("source") or "disk"
    parts = [
        f"[coverage] seq {min(seqs)}..{max(seqs)} of this session",
        f"buffer holds {coverage[0]}..{coverage[1]}",
        f"{len(kept)} of {len(lines)} event(s) shown",
        f"source={source}",
    ]
    if dropped:
        parts.append(f"{dropped} earlier event(s) elided")
    if result.get("dropped_before"):
        parts.append(f"dropped_before={result['dropped_before']}")
    return "\n".join(kept + [" · ".join(parts)])


def digest_block(pass_name: str, buffer: Any, profile: str, layer: str,
                 session_id: str, **overrides: Any) -> str:
    """The pass's block body, budget line included; ``""`` when the projection is empty."""
    settings = dict(PRESETS.get(pass_name) or {})
    settings.update({key: value for key, value in overrides.items() if value is not None})
    settings.setdefault("max_chars", DIGEST_MAX_CHARS)
    text = render_digest(buffer, profile, layer, session_id, **settings)
    if not text:
        return ""
    return f"Transcript budget: {settings.get('max_chars', DIGEST_MAX_CHARS)} characters.\n{text}"


def projection_for(pass_name: str) -> dict[str, Any]:
    return dict(PRESETS.get(pass_name) or PRESETS["base_user"])
