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


# Advice targets, shared by every producer and consumer of advice. ``usage`` is what the
# receiving agent should do with that target; keeping it beside the definition prevents the
# system block and the reviewer prompt from drifting apart.
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
    ("output_surface", "the wording, formatting, or interface being produced",
     "blocker -> do not send; cut or rewrite the cited element. concern -> check the craft "
     "rules; adjust only if honesty or clarity changes. nit -> optional."),
    ("code_economy", "the amount or structure of code being added",
     "blocker -> do not add; reuse the existing capability. concern -> check git, the license, "
     "the framework, or the stdlib first; justify or drop. nit -> optional."),
)

ACTION_TARGETS: tuple[str, ...] = (
    "tool_choice", "current_step", "final_answer", "user_intent", "general")
ARTIFACT_TARGETS: tuple[str, ...] = ("output_surface", "code_economy")
DELIVERED_TARGETS: tuple[str, ...] = (*ACTION_TARGETS, *ARTIFACT_TARGETS)

# Ordered groups for the system block.
ADVICE_TARGET_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Action targets:", ACTION_TARGETS),
    ("Artifact targets:", ARTIFACT_TARGETS),
)

ADVICE_SEVERITIES: tuple[tuple[str, str], ...] = (
    ("blocker", "a concrete risk of wrong or destructive work, a breached explicit instruction, "
                "or a false completion claim. Stop, verify the cited evidence, fix or report "
                "before continuing."),
    ("concern", "you may be heading wrong or missing a constraint. Decide; adjust if the "
                "evidence supports it; no automatic stop."),
    ("nit", "optional polish. Fold at the next step boundary or ignore; never delay."),
)

# The main agent's disposition per (severity, action target). This is the single source for the
# dynamic system block; keep the strings short enough to survive a prompt.
ADVICE_HANDLING: dict[str, dict[str, str]] = {
    "blocker": {
        "tool_choice": "do not execute; re-check args",
        "current_step": "stop; re-plan",
        "final_answer": "do not send; fix or qualify",
        "user_intent": "re-read; clarify only if the action changes",
        "general": "stop; verify",
        "output_surface": "do not send; cut or rewrite the cited element",
        "code_economy": "do not add; reuse the existing capability",
    },
    "concern": {
        "tool_choice": "verify before executing",
        "current_step": "consider an alternative; decide",
        "final_answer": "check claims and evidence; adjust",
        "user_intent": "re-check; ask only if the action changes",
        "general": "decide",
        "output_surface": "check the craft rules; adjust only if honesty or clarity changes",
        "code_economy": "check git, the license, the framework, or the stdlib first; justify or drop",
    },
    "nit": {target: "fold or ignore" for target in DELIVERED_TARGETS},
}


def _advice_target_line(name: str, definition: str, usage: str, *, bullet: str,
                        width: int, include_usage: bool) -> str:
    line = f"{bullet}{name.ljust(width)} {definition}"
    return f"{line} Use: {usage}" if include_usage and usage else line


def advice_target_glossary(*, bullet: str = "  * ", include_usage: bool = True) -> str:
    """One aligned line per advice target, ready to paste into a prompt body."""
    width = max(len(name) for name, _, _ in ADVICE_TARGETS)
    return "\n".join(
        _advice_target_line(name, definition, usage, bullet=bullet, width=width,
                           include_usage=include_usage)
        for name, definition, usage in ADVICE_TARGETS)


def advice_target_section(*, heading: str = "ADVICE TARGETS — DEFINITIONS",
                          bullet: str = "  * ", grouped: bool = False,
                          include_usage: bool = True) -> str:
    """The shared target vocabulary under a heading.

    ``grouped`` separates action targets (delivered to the agent) from memory targets
    (candidate-only). The system block uses the grouped form; the reviewer prompt can use the
    flat form.
    """
    if not grouped:
        return f"{heading}\n{advice_target_glossary(bullet=bullet, include_usage=include_usage)}"
    width = max(len(name) for name, _, _ in ADVICE_TARGETS)
    def lines(names: tuple[str, ...]) -> list[str]:
        return [_advice_target_line(name, definition, usage, bullet=bullet, width=width,
                                    include_usage=include_usage)
                for name, definition, usage in ADVICE_TARGETS if name in names]
    parts = [heading]
    for label, names in ADVICE_TARGET_GROUPS:
        parts.append(label)
        parts.extend(lines(names))
        parts.append("")
    return "\n".join(parts).rstrip()


def advice_severity_section(*, heading: str = "ADVICE SEVERITY") -> str:
    return heading + "\n" + "\n".join(f"- {name}: {meaning}"
                                     for name, meaning in ADVICE_SEVERITIES)


def advice_handling_section(*, heading: str = "ADVICE HANDLING") -> str:
    targets = DELIVERED_TARGETS
    header = "| severity | " + " | ".join(targets) + " |"
    rule = "|" + "---|" * (len(targets) + 1)
    rows = [
        "| " + " | ".join([severity, *[ADVICE_HANDLING[severity][target]
                                       for target in targets]]) + " |"
        for severity, _ in ADVICE_SEVERITIES
    ]
    return "\n".join([heading, header, rule, *rows])


def advice_nit_report_section(*, heading: str = "NIT REPORTING") -> str:
    return "\n".join([
        heading,
        "If one or more nit advices were delivered this turn, end the final answer with this table:",
        "| Nit | Status | Note |",
        "|---|---|---|",
        "| <short nit text> | applied / deferred / ignored | "
        "<where applied / when deferred / why ignored> |",
        "Omit the table when no nit was delivered. Keep the main result first; do not delay it.",
    ])


def advice_language_line(*, heading: str = "ADVICE LANGUAGE") -> str:
    return "\n".join([
        heading,
        "Advice text follows the user's language; severity and target tokens stay English. "
        "Preserve code, paths, commands, identifiers, and quoted errors verbatim.",
    ])


CRAFT_RULES = """# Craft Rules

Two filters, different objects; load by name when the work matches. Neither overrides a
functional requirement or the user's explicit direction.

antislop — surface filter: prose, UI copy, mobile layout, accessibility, code comments. Load
`antislop` (core) plus the satellite for the task; skip its install wizard and mode question,
work in during mode.

anti-overengineering — code-economy brake: the amount and structure of code added or removed.
Load `anti-overengineering`.

Brake before surface: ask whether the line should exist, then whether it reads as generated.
Cleanup removes structure first, comments last; a comment-only task belongs to `antislop-code`.
Functionality outranks economy, economy outranks surface. The user's explicit direction
outranks all three, and a style or hardening the user asked for is not slop."""


def craft_rules_section() -> str:
    """The main agent's static craft contract; emitted regardless of adviser gating."""
    return CRAFT_RULES


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
