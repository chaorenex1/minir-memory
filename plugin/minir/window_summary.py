"""Window summary: the eight-field task state, its JSON contract and its markdown template.

The model returns JSON only. :func:`render` writes the markdown the artifact stores, from a fixed
template, without changing content or truncating it. Values are whitespace-flattened on the way in
so one item stays one line; nothing else is rewritten. Both forms carry the same eight keys; a
summary with a missing or extra key is rejected rather than half-rendered, because the next
base pass reads this section as its starting state.

Empty lists are valid and keep their headings: an empty list is a signal, a placeholder word is
noise. Over-budget output fails and is retried; the renderer never shortens what the model wrote.
"""

from __future__ import annotations

import json
import re
from typing import Any

TEMPLATE_VERSION = 1

KEYS: tuple[str, ...] = (
    "objective", "current_state", "decisions", "constraints",
    "verified_facts", "open_issues", "next_actions", "references",
)
STATE_KEYS: tuple[str, ...] = ("done", "doing", "todo")
PRIORITIES: tuple[str, ...] = ("high", "normal", "low")
REFERENCE_KINDS: tuple[str, ...] = ("file", "log", "config", "url", "issue", "other")

MAX_OBJECTIVE_CHARS = 120
MAX_STATE_ITEMS = 5
MAX_STATE_ITEM_CHARS = 80
MAX_ITEMS = {
    "decisions": 4, "constraints": 5, "verified_facts": 5,
    "open_issues": 5, "next_actions": 5, "references": 5,
}
MAX_ITEM_CHARS = {
    "decisions": 100, "constraints": 100, "verified_facts": 100,
    "open_issues": 100, "next_actions": 100, "references": 160,
}
MAX_TOTAL_CHARS = 880

# A placeholder is a whole value, or a value that opens with the token. Substring matching would
# reject honest prose: "无法复现" contains 无, and "two unknowns remain" contains unknown.
_PLACEHOLDERS: tuple[str, ...] = ("n/a", "unknown", "tbd", "待补充", "暂无", "无")
_PLACEHOLDER_OPENERS: tuple[str, ...] = (":", "：", " -", " —", "-")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


class WindowSummaryError(ValueError):
    """The summary does not satisfy the contract; the message names the offending path."""


def _fail(path: str, message: str) -> None:
    raise WindowSummaryError(f"{path}: {message}")


def is_placeholder(text: str) -> bool:
    """True for a whole-value placeholder. Substring matching would reject honest prose."""
    flat = " ".join(text.split()).strip().lower()
    for token in _PLACEHOLDERS:
        if flat == token:
            return True
        if token != "无" and any(flat.startswith(token + opener)
                                 for opener in _PLACEHOLDER_OPENERS):
            return True
    return False


def _text(value: Any, path: str, *, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(path, "must be a string")
    flat = " ".join(value.split()).strip()
    if not flat:
        if allow_empty:
            return ""
        _fail(path, "must not be empty")
    if is_placeholder(flat):
        _fail(path, f"placeholder value {flat!r} is not allowed")
    if len(flat) > limit:
        _fail(path, f"is {len(flat)} characters, limit is {limit}")
    return flat


def _exact_keys(value: Any, path: str, expected: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    extra = sorted(set(value) - set(expected))
    missing = [key for key in expected if key not in value]
    if extra:
        _fail(path, f"unexpected key(s): {', '.join(extra)}")
    if missing:
        _fail(path, f"missing key(s): {', '.join(missing)}")
    return value


def _list(value: Any, path: str, *, limit: int) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    if len(value) > limit:
        _fail(path, f"has {len(value)} items, limit is {limit}")
    return value


def _objects(value: Any, path: str, keys: tuple[str, ...], *, limit: int) -> list[dict[str, Any]]:
    items = _list(value, path, limit=limit)
    for index, entry in enumerate(items):
        _exact_keys(entry, f"{path}[{index}]", keys)
    return items


def _normalize(summary: Any) -> dict[str, Any]:
    data = _exact_keys(summary, "window_summary", KEYS)
    state = _exact_keys(data["current_state"], "current_state", STATE_KEYS)
    normalized: dict[str, Any] = {
        "objective": _text(data["objective"], "objective", limit=MAX_OBJECTIVE_CHARS,
                           allow_empty=True),
        "current_state": {
            key: [_text(item, f"current_state.{key}[{index}]",
                        limit=MAX_STATE_ITEM_CHARS)
                  for index, item in enumerate(_list(state[key], f"current_state.{key}",
                                                    limit=MAX_STATE_ITEMS))]
            for key in STATE_KEYS
        },
        "decisions": [
            {"decision": _text(entry["decision"], f"decisions[{index}].decision",
                               limit=MAX_ITEM_CHARS["decisions"]),
             "reason": _text(entry["reason"], f"decisions[{index}].reason",
                             limit=MAX_ITEM_CHARS["decisions"])}
            for index, entry in enumerate(
                _objects(data["decisions"], "decisions", ("decision", "reason"),
                         limit=MAX_ITEMS["decisions"]))
        ],
        "constraints": [_text(item, f"constraints[{index}]", limit=MAX_ITEM_CHARS["constraints"])
                        for index, item in enumerate(
                            _list(data["constraints"], "constraints",
                                  limit=MAX_ITEMS["constraints"]))],
        "verified_facts": [
            {"fact": _text(entry["fact"], f"verified_facts[{index}].fact",
                           limit=MAX_ITEM_CHARS["verified_facts"]),
             "evidence": _text(entry["evidence"], f"verified_facts[{index}].evidence",
                               limit=MAX_ITEM_CHARS["verified_facts"])}
            for index, entry in enumerate(
                _objects(data["verified_facts"], "verified_facts", ("fact", "evidence"),
                         limit=MAX_ITEMS["verified_facts"]))
        ],
        "open_issues": [_text(item, f"open_issues[{index}]", limit=MAX_ITEM_CHARS["open_issues"])
                        for index, item in enumerate(
                            _list(data["open_issues"], "open_issues",
                                  limit=MAX_ITEMS["open_issues"]))],
        "next_actions": [],
        "references": [],
    }
    for index, entry in enumerate(
            _objects(data["next_actions"], "next_actions", ("action", "priority"),
                     limit=MAX_ITEMS["next_actions"])):
        priority = entry["priority"]
        if priority not in PRIORITIES:
            _fail(f"next_actions[{index}].priority",
                  f"must be one of {', '.join(PRIORITIES)}; missing priority is a format failure")
        normalized["next_actions"].append({
            "action": _text(entry["action"], f"next_actions[{index}].action",
                            limit=MAX_ITEM_CHARS["next_actions"]),
            "priority": priority,
        })
    for index, entry in enumerate(
            _objects(data["references"], "references", ("kind", "ref"),
                     limit=MAX_ITEMS["references"])):
        kind = entry["kind"]
        if kind not in REFERENCE_KINDS:
            _fail(f"references[{index}].kind", f"must be one of {', '.join(REFERENCE_KINDS)}")
        normalized["references"].append({
            "kind": kind,
            "ref": _text(entry["ref"], f"references[{index}].ref",
                         limit=MAX_ITEM_CHARS["references"]),
        })
    return normalized


def _block(heading: str, items: list[str]) -> str:
    return f"{heading}\n" + "\n".join(items) if items else heading


def _current_state_block(state: dict[str, list[str]]) -> str:
    parts = ["### Current State"]
    blocks = [_block(f"#### {label}", [f"- {item}" for item in state[key]])
              for label, key in (("Done", "done"), ("Doing", "doing"), ("Todo", "todo"))]
    return parts[0] + "\n" + "\n\n".join(blocks)


def _render(summary: dict[str, Any]) -> str:
    """Render a normalized summary. Headings are fixed English; values keep their language."""
    blocks = [
        _block("### Objective", [summary["objective"]] if summary["objective"] else []),
        _current_state_block(summary["current_state"]),
        _block("### Decisions", [f"- {entry['decision']} — {entry['reason']}"
                                 for entry in summary["decisions"]]),
        _block("### Constraints", [f"- {item}" for item in summary["constraints"]]),
        _block("### Verified Facts", [f"- {entry['fact']} — evidence: {entry['evidence']}"
                                      for entry in summary["verified_facts"]]),
        _block("### Open Issues", [f"- {item}" for item in summary["open_issues"]]),
        _block("### Next Actions", [f"- [{entry['priority']}] {entry['action']}"
                                    for entry in summary["next_actions"]]),
        _block("### References", [f"- {entry['kind']}: {entry['ref']}"
                                  for entry in summary["references"]]),
    ]
    return "\n\n".join(["## Window Summary", *blocks])


def empty_summary() -> dict[str, Any]:
    """A valid eight-key summary with nothing in it; used when a row has no summary yet."""
    return {
        "objective": "",
        "current_state": {"done": [], "doing": [], "todo": []},
        "decisions": [], "constraints": [], "verified_facts": [], "open_issues": [],
        "next_actions": [], "references": [],
    }


def validate(summary: Any) -> dict[str, Any]:
    """Check and normalize one summary; raises :class:`WindowSummaryError` naming the field."""
    normalized = _normalize(summary)
    rendered = _render(normalized)
    if len(rendered) > MAX_TOTAL_CHARS:
        _fail("window_summary", f"renders to {len(rendered)} characters, "
                                f"limit is {MAX_TOTAL_CHARS}")
    return normalized


def render(summary: Any) -> str:
    """Validated summary -> the artifact's ``## Window Summary`` section."""
    return _render(validate(summary))


def chars(summary: Any) -> int:
    """Characters the rendered section costs, without building the artifact around it."""
    return len(render(summary))


def parse(text: str) -> dict[str, Any]:
    """Model output -> validated summary. Strips a code fence; rejects anything else."""
    candidate = _FENCE_RE.sub("", (text or "").strip()).strip()
    if not candidate:
        _fail("window_summary", "empty output")
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        _fail("window_summary", f"not valid JSON: {exc}")
    return validate(value)
