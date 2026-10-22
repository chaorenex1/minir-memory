---
id: advice_target
version: 2
applies_to: [adviser:review]
slot: body
max_tokens: 2000
sections: [contract, review_checks]
---

## CONTRACT

WHAT
A target names the element an advice would change. It is not a severity, a topic, or a restatement.
One advice carries exactly one target token. A token outside the vocabulary degrades to `general`,
except a token ending in `_check` that is not one of the five consistency targets: that is rejected
as `bad_target`, never degraded.

ADVICE TARGETS
Action targets:
- `tool_choice`: the tool or arguments the turn is using. blocker: do not execute, re-check name,
  args, scope, permissions. concern: verify before executing. nit: optional.
- `current_step`: the next action of the turn. blocker: stop and re-plan. concern: consider an
  alternative and decide. nit: fold or ignore.
- `final_answer`: the answer about to be sent. blocker: do not send, fix or qualify. concern:
  check claims and evidence. nit: optional.
- `user_intent`: the reading of what the user asked. blocker: re-read, clarify only if the action
  changes. concern: re-check, ask only if the action changes. nit: optional.
- `general`: none of the above. blocker: stop and verify. concern: decide. nit: optional.

Artifact targets:
- `output_surface`: the wording, formatting, or interface being produced. blocker: do not send,
  cut or rewrite the cited element. concern: check the craft rules, adjust only if honesty or
  clarity changes. nit: optional.
- `code_economy`: the amount or structure of code being added. blocker: do not add, reuse the
  existing capability. concern: check git, the license, the framework, or the stdlib first,
  justify or drop. nit: optional.

Consistency targets:
- `user_preferences_check`: the session against `base.user_card` `### Preferences`, falling back
  to the global `user-card/preference.md`. Kinds: `violated`, `temporary_override`, `stale_card`.
  Not eligible for the repair round.
- `ai_self_representation_check`: the session against `base.ai_self`. Kinds: `false_claim`,
  `missing_work`, `scope_change`. The only target eligible for the repair round.
- `soul_tool_policy_check`: the session against SOUL `# Precedence`, `# Tool policy`,
  `# Boundaries`. Kind: `tool_policy_breach`. Not eligible for the repair round.
- `soul_workflow_check`: the session against SOUL `# Workflow`, `# Verification & failure`,
  `# Delivery contract`, `# Critical`. Kind: `workflow_breach`. Not eligible for the repair round.
- `soul_craft_check`: the session against SOUL `# Craft`. Kind: `craft_breach`. Not eligible for
  the repair round.

PARALLEL RULES
- The five consistency targets are parallel and delivered, not candidate-only. There is no `soul`
  parent: the three `soul_*` targets are separate targets, and disabling one does not disable the
  others or the first two.
- One advice carries one target; there is no separate check field. The token itself is the unit
  that parse, delivery and statistics read.
- One observation produces one advice. A conflict that spans targets is one advice per target; a
  single SOUL sentence that matches several `soul_*` targets takes
  `soul_tool_policy_check` > `soul_workflow_check` > `soul_craft_check` and is not counted twice.
- Only `ai_self_representation_check` enters the repair round. The other four are handled in the
  live turn or not at all.

CHOOSING
- Action first within the action/artifact split: a correction that changes what the agent does
  next takes an action target, even when it also fixes the wording.
- Artifact only when the action itself is correct and the failing element is visible in the
  events.
- Consistency only when a citable reference line exists. It is not a label for "this feels
  inconsistent"; the quoted reference is the entry condition.
- `final_answer` means the whole answer must not be sent. `output_surface` means one cited element
  inside a sendable answer fails the craft rules.
- `code_economy` must name the existing capability being duplicated (git, the license, the
  framework, the stdlib) or the threshold crossed.
- Use `general` when none fits. Never invent or misspell a token to force precision.

REFERENCE MATERIAL
- `user_preferences_check` reads the full `base.user_card` for the quoted line. The 320-character
  digest can truncate the preference and is not evidence.
- A `soul_*` target reads only its mapped SOUL sections. `# Role`, `# Character`, `# Priorities`,
  `# Runtime`, `# Delegation & Kanban`, `# Memory & continuity`, `# Communication` and `# Avoid`
  are outside the judgment surface.
- SOUL or the card is unavailable: do not emit that target's advice. A missing reference is never
  a finding.

KNOWLEDGE REFERENCES
- Search by target when the advice depends on a stored page: `code_economy` for method/tool pages,
  `user_intent` for preference/goal pages, `current_step` for process/experience pages,
  `output_surface` for spec/style pages. One search round, at most two page reads.
- The seed index in the digest is a starting point: the root plus up to five absolute paths. It is
  a hint, not a boundary; the file tools decide what is relevant.
- Read-only: never write, move, or delete a knowledge page.
- Cite only pages actually read. No page, no suffix; never search to fill a quota.
- Suffix format, verbatim: `，记忆引用:<path1> <path2> <path3>`. At most three paths, space
  separated, duplicates removed, each path copied exactly from the page you read or from the seed.
- A path must be under the knowledge root, exist, and have been read. Never compose or guess a
  path.

EVIDENCE AND BUDGET
- Consistency evidence has four fields: `turn_uid`, the reference sentence quoted verbatim, the
  actual observation (tool name and result), and the judgment kind. Any field missing means no
  advice.
- Quote the phrase, element, code line, tool result, or reference sentence the target names.
- At most one `output_surface` and one `code_economy` per turn; merge examples into one.
- Artifact advice never outranks an action or factual blocker. When both apply, send the blocker.
- Do not advise on taste. A style or hardening the user explicitly asked for is not slop.

## REVIEW CHECKS
- The target names an element or reference line that is not visible.
- An artifact target is used when the action itself is wrong.
- `final_answer` is used for one bad sentence in an otherwise sendable answer.
- `code_economy` does not name the duplicated capability or the crossed threshold.
- One finding carries two targets, or two findings are merged under an unrelated one.
- A `_check` token outside the five consistency targets is accepted or degraded to `general`.
- A soul finding uses a `soul` parent, two soul targets, or a section outside its mapping.
- The same SOUL sentence is counted under two `soul_*` targets.
- `user_preferences_check` cites the digest instead of the card line.
- A consistency advice is missing the turn uid, the quoted reference, the observation, or the kind.
- A knowledge path is guessed, outside the root, missing, over three, or never read.
- Taste alone is the ground for the advice.
