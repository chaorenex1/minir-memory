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
    "active_forks",
    "ai_cards_this_window",
    "applied_forks",
    "base_layer_artifact",
    "chunk_summaries",
    "conversation_window",
    "dialectic_review",
    "draft_to_audit",
    "first_user_message",
    "focus_topic",
    "initial_draft",
    "knowledge_index",
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
    "review_identity",
    "review_preferences",
    "rulers",
    "self_audit_correction",
    "session_record",
    "session_summary",
    "soul_constraints",
    "target_stats",
    "token_usage",
    "user_cards_this_window",
    "value_markers",
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


# Advice targets, shared by every producer and consumer of advice. ``usage`` is what the
# receiving agent should do with that target; keeping it beside the definition keeps the parser
# and the reviewer prompt on one vocabulary.
#
# The five consistency targets are flat: there is no ``soul`` parent, and each one owns a
# disjoint ruler (the user preference card, the AI self representation, one group of SOUL
# sections). One advice carries exactly one target token.
CONSISTENCY_TARGETS: tuple[str, ...] = (
    "user_preferences_check",
    "user_identity_check",
    "ai_self_representation_check",
    "knowledge_claim_check",
    "soul_tool_policy_check",
    "soul_workflow_check",
    "soul_craft_check",
)

ADVICE_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("tool_choice", "the tool or arguments the turn is using",
     "blocker -> do not execute; re-check name, args, scope, permissions. "
     "concern -> verify before executing. nit -> optional."),
    ("current_step", "the next action of the turn",
     "blocker -> stop and re-plan. concern -> consider an alternative and decide. "
     "nit -> fold or ignore."),
    ("final_answer", "the answer about to be sent",
     "blocker -> do not send; fix or qualify. concern -> check claims and evidence. "
     "nit -> optional."),
    ("user_intent", "the reading of what the user asked",
     "blocker -> re-read; clarify only if the action changes. "
     "concern -> re-check; ask only if the action changes. nit -> optional."),
    ("general", "none of the above",
     "blocker -> stop and verify. concern -> decide. nit -> optional."),
    ("delegation", "the handoff to a subagent and the acceptance of its result",
     "blocker -> do not hand off or accept the child's claim; verify with a named check. "
     "concern -> narrow the context and name the acceptance. nit -> optional."),
    ("output_surface", "the wording, formatting, or interface being produced",
     "blocker -> do not send; cut or rewrite the cited element. concern -> check the craft "
     "rules; adjust only if honesty or clarity changes. nit -> optional."),
    ("code_economy", "the amount or structure of code being added",
     "blocker -> do not add; reuse the existing capability. concern -> check git, the license, "
     "the framework, or the stdlib first; justify or drop. nit -> optional."),
    ("user_preferences_check", "the session against the user preference card",
     "blocker -> a safety or process preference was breached and the action is out; stop, "
     "report. concern -> correct the expression now. nit -> optional."),
    ("user_identity_check", "the session against the operator's identity card",
     "blocker -> the work is for the wrong domain or deliverable; stop and re-read. "
     "concern -> re-check identity, deliverable or assumptions. nit -> optional."),
    ("knowledge_claim_check", "the session against a cited knowledge claim",
     "no blocker; a derived claim can only concern. concern -> the action contradicts, "
     "duplicates or misses a recorded decision; cite the claim and correct it. nit -> optional."),
    ("ai_self_representation_check", "the session against its own published self representation",
     "blocker -> the completion claim is false; do not send, verify now. concern -> recalibrate "
     "now. nit -> optional."),
    ("soul_tool_policy_check", "the session against SOUL Precedence and Boundaries plus the "
     "dispatch skill's Tool policy",
     "blocker -> stop; report; do not restate sensitive content. concern -> correct the "
     "procedure now. no nit."),
    ("soul_workflow_check", "the session against SOUL Delivery contract and Critical plus the "
     "dispatch skill's Workflow and Verification",
     "blocker -> stop; report; do not restate sensitive content. concern -> correct the step "
     "now. no nit."),
    ("soul_craft_check", "the session against the dispatch skill's Craft",
     "concern -> correct the code economy now. nit -> fold at the next boundary. no blocker."),
)

ACTION_TARGETS: tuple[str, ...] = (
    "tool_choice", "current_step", "final_answer", "user_intent", "general", "delegation")
ARTIFACT_TARGETS: tuple[str, ...] = ("output_surface", "code_economy")
DELIVERED_TARGETS: tuple[str, ...] = (*ACTION_TARGETS, *ARTIFACT_TARGETS, *CONSISTENCY_TARGETS)

#: What one target needs in order to be judged at all, keyed by the capability a run declares
#: when it is dispatched (``plugin/minir/agent_watch.py``). A capability the map does not name is
#: not required, so the primary session — which declares nothing — keeps every target.
TARGET_NEEDS: dict[str, str] = {
    "soul_tool_policy_check": "soul",
    "soul_workflow_check": "soul",
    "soul_craft_check": "soul",
    "user_preferences_check": "memory",
    "user_identity_check": "memory",
    "knowledge_claim_check": "memory",
    "ai_self_representation_check": "memory",
    "code_economy": "file",
}


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
