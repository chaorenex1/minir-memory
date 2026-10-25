"""Layer contracts: output shapes, validation and rendering for the model-driven passes.

Each pass's role, inputs and boundaries live in a text asset under ``agents/``, read at call time
(``load_core``); the body rules live in ``rules/*.md`` and are appended as text, untransformed.
This module keeps what code must own: the output contracts, the strict validators and the
renderer. A prompt edit does not touch this module, and an output-shape change does not touch a
prompt asset. The B3 asset's ``$NAME`` placeholders are filled from ``window_summary.PROMPT_VARS``,
so the caps the prompt states cannot drift from the code.

Validation is strict on purpose. A base refresh that returns a missing key, an unknown key, a
wrong enum or a placeholder fails and is retried once; repairing it silently would publish a card
the next pass reads as fact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import Any

from . import window_summary as summary_template
from .window_summary import WindowSummaryError

# Every card part is markdown text: the model writes the body, the code only frames it. Empty is a
# valid card, so nothing here is a field contract; CARD_TEXT_LIMIT stops a runaway body.
CARD_TEXT_LIMIT = 8000
USER_PARTS: tuple[str, ...] = ("user_self_representation", "user_preferences")
AI_PARTS: tuple[str, ...] = ("ai_self_representation", "ai_identity")

CATEGORIES: tuple[str, ...] = ("KNOWLEDGE", "VALUE", "INTENT", "METHOD", "GOVERNANCE")
ORGS: tuple[str, ...] = ("entities", "trajectories", "experiences", "events", "tools", "projects")
EPISTEMIC: tuple[str, ...] = ("fact", "inference", "hypothesis", "prediction")
RELATION_TYPES: tuple[str, ...] = ("supersedes", "caused_by", "part_of", "contradicts",
                                   "updates", "instance_of", "derived_from", "same_as")
PART_NAMES: tuple[str, ...] = (
    "user_self_representation", "user_preferences",
    "ai_self_representation", "ai_identity", "window_summary",
)
PART_SCOPES: dict[str, str] = {
    "user_self_representation": "user_card", "user_preferences": "user_card",
    "ai_self_representation": "ai_card", "ai_identity": "ai_card",
    "window_summary": "window_summary",
}
CHANGE_OPS: tuple[str, ...] = ("add", "update", "remove")

_PRESET_OCCUPATION = {"zh": "软件开发工程师", "en": "Software Development Engineer"}

MAX_UNITS = 16
MAX_UNIT_CONTENT = 1200
MAX_UNIT_WHY = 160
MAX_UNIT_TOPIC = 40
MAX_UNIT_RELATIONS = 3
MAX_UNIT_OBJECTS = 5

BASE_HEADINGS: tuple[str, ...] = (
    "## User Card", "## AI Card", "## Window Summary", "## Value Markers",
)


class LayerContractError(ValueError):
    """The model's object does not satisfy the layer's output contract."""


# -- prompts -----------------------------------------------------------------------------------

AGENTS_DIR = Path(__file__).resolve().parent / "agents"


class AgentPromptError(RuntimeError):
    """A packaged agent prompt is missing or unreadable; the pass cannot run without it."""


def prompt_vars() -> dict[str, Any]:
    """The values an agent prompt may interpolate: each cap comes from the code that owns it."""
    return {
        "MAX_UNITS": MAX_UNITS, "MAX_UNIT_CONTENT": MAX_UNIT_CONTENT,
        "MAX_UNIT_WHY": MAX_UNIT_WHY,
        "MAX_UNIT_TOPIC": MAX_UNIT_TOPIC, "MAX_UNIT_RELATIONS": MAX_UNIT_RELATIONS,
        "MAX_UNIT_OBJECTS": MAX_UNIT_OBJECTS,
        **summary_template.PROMPT_VARS,
    }


def load_core(name: str) -> str:
    """One pass's system prompt from ``agents/<name>.md``, read and filled at call time.

    A missing asset fails the pass rather than degrading to the rule files alone: the rules state
    what each body covers, but the core is what carries the pass's role, caps and boundaries.
    ``$NAME`` placeholders are filled from :func:`prompt_vars`; an unknown one is an error, not a
    literal left in the prompt.
    """
    path = AGENTS_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise AgentPromptError(f"cannot read agent prompt {path}: {exc}") from exc
    try:
        return Template(text).substitute(prompt_vars())
    except (KeyError, ValueError) as exc:
        raise AgentPromptError(f"agent prompt {path} has a bad placeholder: {exc}") from exc


OUTPUT_B1 = """OUTPUT
Return exactly one JSON object:
{
  "user_self_representation": "<markdown body>",
  "user_preferences": "<markdown body>",
  "window_summary": "<the B3 child's markdown, copied verbatim>",
  "value_markers": [<the B4 child's marker objects, copied verbatim>],
  "page_writes": [<the B4 child's write results, copied verbatim>]
}
The card values are markdown text, not objects; an empty string is a valid empty card. A failed
child leaves its key as "" or []. No code fence, no extra keys.
"""

OUTPUT_B2 = """OUTPUT
Return exactly one JSON object:
{
  "ai_self_representation": "<markdown body>",
  "ai_identity": "<markdown body>"
}
Both values are markdown text, not objects: the body as you want it in the file. An empty string is
a valid empty card. No code fence, no extra keys.
"""

OUTPUT_B3 = """OUTPUT
Return exactly one JSON object:
{"window_summary": "<the section as one string, newlines escaped as \\n>"}
No prose, no code fence, no extra keys.
"""

OUTPUT_B4 = """OUTPUT
Return exactly one JSON object with the units you wrote and the write results you observed:
{"value_markers": [<units>], "page_writes": [<minir_kb_write results, topic added>]}
No prose, no code fence, no extra keys.
"""

OUTPUT_DIALECTIC = """OUTPUT
Return exactly one JSON object:
{
  "user_card": {"user_self_representation": "<markdown body>", "user_preferences": "<markdown body>"},
  "ai_card": {"ai_self_representation": "<markdown body>", "ai_identity": "<markdown body>"},
  "window_summary": "## Window Summary ... the same template as the base pass, filled in",
  "base_changed": {"changed": false, "fields": []},
  "changes": [{"scope": "user_card", "part": "user_self_representation", "op": "update",
               "reason": "", "sessions": []}],
  "prunings": []
}
The four card values are complete markdown bodies, not objects; an empty string is an empty card.
All sections are required. base_changed.fields may be empty; prunings must be empty. No code fence,
no extra keys.
"""


# -- validation helpers ------------------------------------------------------------------------

def _fail(path: str, message: str) -> None:
    raise LayerContractError(f"{path}: {message}")


def _keys(value: Any, path: str, expected: tuple[str, ...], *, optional: tuple[str, ...] = ()
          ) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    allowed = set(expected) | set(optional)
    extra = sorted(set(value) - allowed)
    missing = [key for key in expected if key not in value]
    if extra:
        _fail(path, f"unexpected key(s): {', '.join(extra)}")
    if missing:
        _fail(path, f"missing key(s): {', '.join(missing)}")
    return value


def _string(value: Any, path: str, limit: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(path, "must be a string")
    flat = " ".join(value.split()).strip()
    if not flat and not allow_empty:
        _fail(path, "must not be empty")
    if summary_template.is_placeholder(flat):
        _fail(path, f"placeholder value {flat!r} is not allowed")
    if len(flat) > limit:
        _fail(path, f"is {len(flat)} characters, limit is {limit}")
    return flat


def _strings(value: Any, path: str, limit: int, max_items: int) -> list[str]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    if len(value) > max_items:
        _fail(path, f"has {len(value)} items, limit is {max_items}")
    return [_string(item, f"{path}[{index}]", limit, allow_empty=False)
            for index, item in enumerate(value)]


def _clamped(value: Any, path: str, limit: int) -> str:
    """One free-text field capped at *limit*: an over-long answer is trimmed, not fatal.

    The size limit exists to bound storage and prompt cost, so a model that overshoots by a
    few characters loses that tail instead of the whole pass (a rejected window costs two more
    model calls and drops the evidence it already merged).
    """
    if not isinstance(value, str):
        _fail(path, "must be a string")
    flat = " ".join(value.split()).strip()
    if not flat:
        _fail(path, "must not be empty")
    if summary_template.is_placeholder(flat):
        _fail(path, f"placeholder value {flat!r} is not allowed")
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit // 2 else cut).rstrip(" ,;:.-·")


def validate_card_body(value: Any, path: str, limit: int = CARD_TEXT_LIMIT) -> str:
    """One card body: markdown text, empty allowed, framed by the size limit only."""
    if not isinstance(value, str):
        _fail(path, "must be a string of markdown, not an object")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text and summary_template.is_placeholder(" ".join(text.split())):
        _fail(path, f"placeholder value {text!r} is not allowed")
    if len(text) > limit:
        _fail(path, f"is {len(text)} characters, limit is {limit}")
    return text


def validate_user_self_representation(value: Any) -> str:
    return validate_card_body(value, "user_self_representation")


def validate_user_preferences(value: Any) -> str:
    return validate_card_body(value, "user_preferences")


def validate_user_card(value: Any) -> dict[str, str]:
    data = _keys(value, "user_card", USER_PARTS)
    return {part: validate_card_body(data[part], f"user_card.{part}") for part in USER_PARTS}


def validate_base_pass(value: Any) -> dict[str, Any]:
    """B1's reply: cards and summary keep their contracts, markers are data.

    ``page_writes`` is optional because a run whose page tool failed still has markers worth
    publishing; an unknown extra key there would kill the same window the marker tolerance is
    meant to save.
    """
    data = _keys(value, "base_pass",
                 ("user_self_representation", "user_preferences", "window_summary"),
                 optional=("value_markers", "page_writes"))
    card = validate_user_card({
        "user_self_representation": data["user_self_representation"],
        "user_preferences": data["user_preferences"]})
    summary = validate_summary_pass({"window_summary": data["window_summary"]})["window_summary"]
    markers = coerce_value_markers(data.get("value_markers"))
    writes = data.get("page_writes")
    if not isinstance(writes, list):
        writes = []
    return {**card, "window_summary": summary, "value_markers": markers,
            "page_writes": [dict(item) for item in writes[:MAX_UNITS] if isinstance(item, dict)]}


def validate_ai_card(value: Any) -> dict[str, str]:
    data = _keys(value, "ai_card", AI_PARTS)
    return {part: validate_card_body(data[part], f"ai_card.{part}") for part in AI_PARTS}


def validate_ai_self_representation(value: Any) -> str:
    return validate_card_body(value, "ai_self_representation")


def validate_ai_identity(value: Any) -> str:
    return validate_card_body(value, "ai_identity")


def coerce_value_markers(value: Any, *, previous: Any = None) -> list[dict[str, Any]]:
    """Keep B4's units as data: no field is required, no extra key is fatal.

    A window's summary and cards must survive a malformed marker, so this function never raises.
    Identity strings are coerced only because the renderer and the merge key read them; every
    other field is carried through and the page writer validates it at write time.
    """
    data = value.get("value_markers") if isinstance(value, dict) else value
    if not isinstance(data, list):
        data = []
    units = [dict(entry) for entry in data[:MAX_UNITS] if isinstance(entry, dict)]
    return _merge_units(units, previous)


def _merge_units(units: list[dict[str, Any]], previous: Any) -> list[dict[str, Any]]:
    """A unit already carrying a path is identified by it: the newer entry replaces the older one.

    A carried entry without a category, an org or a topic cannot be merged, rendered or written,
    so it is not carried forward. A legacy inventory whose entries still carry all three
    survives until the one-shot reset in scripts/drop_legacy_markers.py removes it.
    """
    merged: dict[object, dict[str, Any]] = {}
    order: list[object] = []
    for unit in list(previous or []) + units:
        if not isinstance(unit, dict):
            continue
        path = str(unit.get("path") or "").strip()
        topic = " ".join(str(unit.get("topic") or "").split())
        org = str(unit.get("org") or "").strip()
        category = str(unit.get("category") or "").strip().upper()
        if not topic or not org or not category:
            continue
        key = path or (category, org, topic.casefold())
        if key not in merged:
            order.append(key)
        merged[key] = unit
    return [merged[key] for key in order]


def validate_value_markers(value: Any, *, previous: Any = None) -> list[dict[str, Any]]:
    """B4's units, carried as data; the page writer validates them at write time."""
    return coerce_value_markers(value, previous=previous)


def validate_summary_pass(value: Any) -> dict[str, Any]:
    """Pass 3: the model-written summary section, nothing else."""
    data = _keys(value, "summary_pass", ("window_summary",))
    try:
        summary = summary_template.validate_nonempty(data["window_summary"])
    except WindowSummaryError as exc:
        raise LayerContractError(str(exc)) from exc
    return {"window_summary": summary}


def validate_review(value: Any) -> dict[str, Any]:
    """The dialectic object: four markdown bodies, the summary, plus what it says it changed."""
    data = _keys(value, "review", ("user_card", "ai_card", "window_summary", "base_changed",
                                   "changes", "prunings"))
    user_card = _keys(data["user_card"], "user_card", USER_PARTS)
    ai_card = _keys(data["ai_card"], "ai_card", AI_PARTS)
    changed = _keys(data["base_changed"], "base_changed", ("changed", "fields"))
    if not isinstance(changed["changed"], bool):
        _fail("base_changed.changed", "must be a boolean")
    fields = _strings(changed["fields"], "base_changed.fields", 40, len(PART_NAMES))
    unknown = [field for field in fields if field not in PART_NAMES]
    if unknown:
        _fail("base_changed.fields", f"unknown part(s): {', '.join(unknown)}")
    if changed["changed"] and not fields:
        _fail("base_changed", "changed is true but fields is empty")
    try:
        summary = summary_template.validate(data["window_summary"])
    except WindowSummaryError as exc:
        raise LayerContractError(str(exc)) from exc
    changes = data["changes"]
    if not isinstance(changes, list) or len(changes) > 20:
        _fail("changes", "must be an array of at most 20 entries")
    for index, entry in enumerate(changes):
        path = f"changes[{index}]"
        change = _keys(entry, path, ("scope", "part", "op", "reason", "sessions"))
        if change["scope"] not in set(PART_SCOPES.values()):
            _fail(f"{path}.scope", "must be user_card, ai_card or window_summary")
        if change["part"] not in PART_NAMES:
            _fail(f"{path}.part", f"must be one of {', '.join(PART_NAMES)}")
        if PART_SCOPES[change["part"]] != change["scope"]:
            _fail(f"{path}.scope", f"does not match part {change['part']!r}")
        if change["op"] not in CHANGE_OPS:
            _fail(f"{path}.op", f"must be one of {', '.join(CHANGE_OPS)}")
        _string(change["reason"], f"{path}.reason", 200, allow_empty=False)
        _strings(change["sessions"], f"{path}.sessions", 80, 5)
    if data["prunings"] != []:
        _fail("prunings", "the dialectic may not prune; return an empty array")
    return {
        "user_self_representation": validate_user_self_representation(user_card["user_self_representation"]),
        "user_preferences": validate_user_preferences(user_card["user_preferences"]),
        "ai_self_representation": validate_ai_self_representation(ai_card["ai_self_representation"]),
        "ai_identity": validate_ai_identity(ai_card["ai_identity"]),
        "window_summary": summary,
        "changed": list(fields),
    }


def empty_user_self_representation() -> str:
    return ""


def empty_user_preferences() -> str:
    return ""


def empty_ai_self_representation() -> str:
    return ""


def empty_ai_identity() -> str:
    return ""


def preset_user_self_representation(language: str = "en") -> str:
    """First-refresh baseline: one occupation line; the first real window replaces the body."""
    key = "zh" if str(language or "").lower().startswith("zh") else "en"
    heading, label = ("## 自我描述", "职业") if key == "zh" else ("## Self", "Occupation")
    return f"{heading}\n\n- {label}: {_PRESET_OCCUPATION[key]}"


def artifact_from_columns(row: dict[str, Any] | None) -> dict[str, Any]:
    """A base row's JSON columns -> the logical parts (unknown/legacy values become empty)."""
    def load(column: str, default: Any) -> Any:
        try:
            value = json.loads(str((row or {}).get(column) or ""))
        except (json.JSONDecodeError, TypeError):
            return default
        return value if isinstance(value, type(default)) else default

    raw_markers = load("value_markers", {}) if row else {}
    if isinstance(raw_markers, dict):
        value_markers = raw_markers.get("value_markers")
        if value_markers is None:
            value_markers = raw_markers.get("units")
    else:
        value_markers = raw_markers
    return {
        "user_self_representation": load("user_representation", ""),
        "user_preferences": load("user_peer_card", ""),
        "ai_self_representation": load("ai_self_representation", ""),
        "ai_identity": load("ai_identity_card", ""),
        "window_summary": load("window_summary", ""),
        "value_markers": value_markers if isinstance(value_markers, list) else [],
    }


def render_user_card(user_self_representation: str, preferences: str) -> str:
    return "\n\n".join([
        "## User Card",
        _section("Self Representation", user_self_representation),
        _section("Preferences", preferences),
    ])


def render_ai_card(ai_self_representation: str, identity: str) -> str:
    return "\n\n".join([
        "## AI Card",
        _section("Self Representation", ai_self_representation),
        _section("Identity", identity),
    ])


def _section(title: str, text: str) -> str:
    body = str(text or "").strip()
    return f"### {title}\n\n{body}" if body else f"### {title}"


def render_markers(value_markers: list[dict[str, Any]]) -> str:
    """The artifact's marker index: tolerant of missing fields, never raises."""
    lines: list[str] = []
    for unit in value_markers or []:
        if not isinstance(unit, dict):
            continue
        topic = " ".join(str(unit.get("topic") or "").split())
        if not topic:
            continue
        category = str(unit.get("category") or "").strip().upper()
        org = str(unit.get("org") or "").strip()
        summary = " ".join(str(unit.get("summary") or "").split())
        if not summary:
            body = " ".join(str(unit.get("content") or "").split())
            summary = (body[:117] + "...") if len(body) > 120 else body
        anchor = unit.get("anchor") if isinstance(unit.get("anchor"), dict) else {}
        where = ""
        if anchor.get("from_ts") is not None:
            where = " (anchor %s..%s)" % (anchor.get("from_ts"), anchor.get("to_ts"))
        label = "/".join(part for part in (category, org) if part) or "unit"
        lines.append("- [%s] %s: %s%s" % (label, topic, summary, where))
    heading = "## Value Markers"
    return heading + ("\n" + "\n".join(lines) if lines else "")


def render_artifact(*, user_self_representation: dict[str, Any], user_preferences: dict[str, Any],
                    ai_self_representation: dict[str, Any], ai_identity: dict[str, Any],
                    summary: str, value_markers: list[dict[str, Any]]) -> str:
    """The four-section artifact, in the order the parser and the cross-session rules expect."""
    try:
        summary_md = summary_template.validate(summary)
    except WindowSummaryError as exc:
        raise LayerContractError(str(exc)) from exc
    return "\n\n".join([
        render_user_card(user_self_representation, user_preferences),
        render_ai_card(ai_self_representation, ai_identity),
        summary_md,
        render_markers(value_markers),
    ])


def is_artifact(content: str) -> bool:
    """True when the four headings sit on their own lines, in order, with nothing before them."""
    positions = []
    for heading in BASE_HEADINGS:
        match = re.search(rf"^{re.escape(heading)}\s*$", content, re.MULTILINE)
        if match is None:
            return False
        positions.append(match.start())
    return positions == sorted(positions) and content.lstrip().startswith(BASE_HEADINGS[0])
