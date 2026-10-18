"""Shared prompt framing: instructions in the system message, data in labeled blocks.

Every non-system input the plugin hands a model is wrapped by :func:`data_block`. The label lets
the system prompt name the source and its trust level, and a payload cannot close its own block:
a tag that matches the label is neutralized before insertion. This is defense in depth — the
system prompts still state the trust hierarchy, because a delimiter is not an instruction
boundary by itself.
"""

from __future__ import annotations

import re

# Every label the plugin uses for a data block. A payload can contain another block's tag just
# as easily as its own, so all reserved tags are neutralized, not only the current label. Add new
# block labels here.
RESERVED_LABELS: frozenset[str] = frozenset({
    "ai_cards_this_window",
    "base_layer_artifact",
    "chunk_summaries",
    "conversation_window",
    "dialectic_review",
    "draft_to_audit",
    "first_user_message",
    "focus_topic",
    "initial_draft",
    "live_turn_events",
    "long_term_context",
    "operator_identity",
    "operator_preferences",
    "prepared_chunk",
    "previous_ai_cards",
    "previous_dialectic_review",
    "previous_session_summary",
    "previous_user_cards",
    "previous_value_markers",
    "provider_memory_context",
    "self_audit_correction",
    "user_cards_this_window",
})
_TAG_RE = re.compile(
    r"<\s*/?\s*(?:" + "|".join(map(re.escape, sorted(RESERVED_LABELS))) + r")\b[^>]*>",
    re.IGNORECASE,
)


# The five value-marker types, defined once. Every prompt that writes, corrects, tags or
# describes a marker uses this table, so the meaning cannot drift between the pass that stores an
# item and the pass that later judges it.
MARKER_TYPES: tuple[tuple[str, str], ...] = (
    ("entities", "named people, organizations, services, systems or devices, and why they "
                 "matter here"),
    ("trajectories", "an ongoing goal or plan and its current state"),
    ("experiences", "a reusable lesson, procedure or outcome proven by the evidence"),
    ("tools", "a tool, command or API and the context in which it worked or failed"),
    ("projects", "a project, repository or workspace and its status"),
)


def marker_glossary(*, bullet: str = "  * ") -> str:
    """One aligned line per marker type, ready to paste into a prompt body."""
    width = max(len(key) for key, _ in MARKER_TYPES)
    return "\n".join(f"{bullet}{key.ljust(width)} {meaning}" for key, meaning in MARKER_TYPES)


def marker_section(*, heading: str = "VALUE MARKERS — DEFINITIONS",
                   bullet: str = "  * ") -> str:
    """The glossary under a heading, for prompts that reference the types by name."""
    return f"{heading}\n{marker_glossary(bullet=bullet)}"


# Advice targets, shared by both producers of an advisory block (the dialectic review and the
# live adviser). One vocabulary, so the agent reads the same tokens whatever produced the advice.
ADVICE_TARGETS: tuple[tuple[str, str], ...] = (
    ("user_representation", "the user's need-path card"),
    ("user_peer_card", "the user's durable preferences card"),
    ("ai_self_representation", "the assistant's action-state card"),
    ("ai_identity_card", "the assistant's identity card"),
    ("session_chunk_summary", "the window record"),
    ("value_markers", "the durable marker inventory"),
    ("tool_choice", "the tool or arguments the turn is using"),
    ("current_step", "the next action of the turn"),
    ("final_answer", "the answer given to the user"),
    ("user_intent", "the reading of what the user asked for"),
    ("general", "none of the above"),
)


def advice_target_glossary(*, bullet: str = "  * ") -> str:
    """One aligned line per advice target, ready to paste into a prompt body."""
    width = max(len(key) for key, _ in ADVICE_TARGETS)
    return "\n".join(f"{bullet}{key.ljust(width)} {meaning}"
                     for key, meaning in ADVICE_TARGETS)


def advice_target_section(*, heading: str = "ADVICE TARGETS — DEFINITIONS",
                          bullet: str = "  * ") -> str:
    """The shared target vocabulary under a heading."""
    return f"{heading}\n{advice_target_glossary(bullet=bullet)}"


def data_block(label: str, text: object, *, empty: str = "(none)") -> str:
    """Wrap *text* as one labeled data block.

    A payload that contains a reserved opening or closing tag is rewritten as literal text, so a
    conversation message cannot end ``<conversation_window>`` early — or close another block — and
    continue as if it were prompt structure.
    """
    key = str(label or "data").strip()
    body = str(text or "").strip() or empty

    def literal(match: re.Match[str]) -> str:
        name = re.search(r"[A-Za-z_][\w-]*", match.group(0))
        return f"[literal {name.group(0).lower() if name else 'block'} tag]"

    body = _TAG_RE.sub(literal, body)
    if key not in RESERVED_LABELS:
        body = re.sub(rf"<\s*/?\s*{re.escape(key)}\b[^>]*>", literal, body,
                      flags=re.IGNORECASE)
    return f"<{key}>\n{body}\n</{key}>"
