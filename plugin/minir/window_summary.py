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

PRIORITIES: tuple[str, ...] = ("high", "normal", "low")
REFERENCE_KINDS: tuple[str, ...] = ("file", "log", "config", "url", "issue", "other")

MAX_OBJECTIVE_CHARS = 120
MAX_STATE_ITEMS = 5
MAX_STATE_ITEM_CHARS = 80
MAX_DECISIONS = 4
MAX_SECTION_ITEMS = 5
MAX_SECTION_ITEM_CHARS = 100
MAX_REFERENCE_CHARS = 160
MAX_TOTAL_CHARS = 880

# The values an agent prompt interpolates: each cap is stated once, in the prompt, from here.
PROMPT_VARS: dict[str, Any] = {
    "MAX_OBJECTIVE_CHARS": MAX_OBJECTIVE_CHARS,
    "MAX_STATE_ITEMS": MAX_STATE_ITEMS,
    "MAX_STATE_ITEM_CHARS": MAX_STATE_ITEM_CHARS,
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
