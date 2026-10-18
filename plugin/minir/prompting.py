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
