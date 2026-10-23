"""Window summary: the task-state section, its shape asset and its prompt values.

The model writes this section as one markdown block following ``templates/window_summary.md``;
the template is the shape, the prompt states the caps, and :func:`validate` only insists that the
value is a string. Nothing here rewrites or re-renders the model's text, because the next base
refresh reads exactly what this pass wrote.

The numbers the prompt interpolates live in ``PROMPT_VARS``, so a cap is stated in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

TEMPLATE_VERSION = 1
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "window_summary.md"

#: The eight sections a summary must carry, in template order. Compression and the 24h final
#: summary validate against this same list so one shape covers the window, its snapshots and the
#: session's last record.
FIELD_HEADINGS: tuple[str, ...] = (
    "### Objective", "### Current State", "### Decisions", "### Constraints",
    "### Verified Facts", "### Open Issues", "### Next Actions", "### References",
)

PRIORITIES: tuple[str, ...] = ("high", "normal", "low")
REFERENCE_KINDS: tuple[str, ...] = ("file", "log", "config", "url", "issue", "other")

# The goal ledger (``objective`` + ``current_state``) has no item cap and no share of the total:
# it must list every goal, so only the per-line budget bounds it. ``MAX_TOTAL_CHARS`` is the
# prompt target for the other six sections.
MAX_GOAL_CHARS = 100
MAX_DECISIONS = 4
MAX_SECTION_ITEMS = 5
MAX_SECTION_ITEM_CHARS = 100
MAX_REFERENCE_CHARS = 160
MAX_TOTAL_CHARS = 880

# The values an agent prompt interpolates: each cap is stated once, in the prompt, from here.
PROMPT_VARS: dict[str, Any] = {
    "MAX_GOAL_CHARS": MAX_GOAL_CHARS,
    "MAX_DECISIONS": MAX_DECISIONS,
    "MAX_SECTION_ITEMS": MAX_SECTION_ITEMS,
    "MAX_SECTION_ITEM_CHARS": MAX_SECTION_ITEM_CHARS,
    "MAX_REFERENCE_CHARS": MAX_REFERENCE_CHARS,
    "MAX_TOTAL_CHARS": MAX_TOTAL_CHARS,
    "PRIORITIES": ", ".join(PRIORITIES[:-1]) + " or " + PRIORITIES[-1],
    "REFERENCE_KINDS": ", ".join(REFERENCE_KINDS[:-1]) + " or " + REFERENCE_KINDS[-1],
}

# A placeholder is a whole line, or a line that opens with the token. Substring matching would
# reject honest prose: "无法复现" contains 无, and "two unknowns remain" contains unknown.
_PLACEHOLDERS: tuple[str, ...] = ("n/a", "unknown", "tbd", "待补充", "暂无", "无")
_PLACEHOLDER_OPENERS: tuple[str, ...] = (":", "：", " -", " —", "-")


class WindowSummaryError(ValueError):
    """The section does not satisfy the contract; the message names the field."""


class WindowTemplateError(RuntimeError):
    """The template asset is missing or unreadable.

    Deliberately not a :class:`WindowSummaryError`: a broken asset is not a malformed model reply,
    so the pass fails instead of spending its one format retry on what the retry cannot fix.
    """


def _fail(path: str, message: str) -> None:
    raise WindowSummaryError(f"{path}: {message}")


def load_template() -> str:
    """The template asset, read at call time.

    A missing or unreadable template fails the pass instead of falling back to a copy in Python:
    the empty section and the prompt's path reference must both point at this one asset.
    """
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise WindowTemplateError(
            f"cannot read the window summary template {TEMPLATE_PATH}: {exc}") from exc


def is_placeholder(text: str) -> bool:
    """True for a whole-value placeholder. Substring matching would reject honest prose."""
    flat = " ".join(str(text or "").split()).strip().lower()
    for token in _PLACEHOLDERS:
        if flat == token:
            return True
        if token != "无" and any(flat.startswith(token + opener)
                                 for opener in _PLACEHOLDER_OPENERS):
            return True
    return False


def validate(text: Any) -> str:
    """The section is model-written markdown; the contract only checks that it is a string."""
    if not isinstance(text, str):
        _fail("window_summary", "must be a markdown string")
    return text.strip()


def empty_summary() -> str:
    """The template with nothing filled in; what a row without a summary carries."""
    return load_template()


def is_empty(text: Any) -> bool:
    """True when the section carries no line besides its headings."""
    if not isinstance(text, str):
        return True
    return not any(line.strip() and not line.strip().startswith("#")
                   for line in text.splitlines())


#: The two sections that make up the window's goal ledger.
_GOAL_SECTIONS = frozenset({"### Objective", "### Current State"})
TRUNCATED_MARKER = "[summary truncated]"


def _sections(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split a summary into (preamble lines, [(heading, body lines)])."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("### "):
            sections.append((line.rstrip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return preamble, sections


def for_prompt(text: str, limit: int = 2000) -> str:
    """Bound injected summary text: keep Objective/Current State whole, cut the rest.

    This is a prompt budget, not a validator: the stored summary is never rewritten. When the
    ledger alone exceeds *limit* it stays whole, because a goal list is not droppable.
    """
    if len(text) <= limit:
        return text
    preamble, sections = _sections(text)
    ledger = [section for section in sections if section[0] in _GOAL_SECTIONS]
    rest = [section for section in sections if section[0] not in _GOAL_SECTIONS]
    head = "\n".join(preamble + ["\n".join([heading, *body]) for heading, body in ledger]).strip()
    budget = limit - len(head) - len(TRUNCATED_MARKER) - 4
    kept: list[str] = []
    if budget > 0:
        for heading, body in rest:
            chunk = "\n".join([heading, *body])
            if len(chunk) > budget:
                kept.append(chunk[:budget].rstrip())
                break
            kept.append(chunk)
            budget -= len(chunk) + 1
    return "\n\n".join(part for part in [head, *kept, TRUNCATED_MARKER] if part)
