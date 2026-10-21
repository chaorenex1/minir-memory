---
id: user-preferences
version: 1
applies_to: [base:B1, dialectic, card_review]
slot: fields
max_tokens: 350
sections: [contract, review_checks]
---

## CONTRACT

WHAT
How this person wants the assistant to work. Seven optional fields. A constraint that held for one
turn is scene, not preference.

FIELDS
- communication: language, tone, length, level of detail.
- presentation: conclusion or analysis first; lists or tables; code or prose.
- method: plan first, staged delivery, verify before advancing, smallest reversible change.
- decision: which criterion wins: evidence, cost, speed or robustness.
- technology: toolchain, runtime, deployment environment, version-control habits.
- collaboration: how much the assistant drives, when it confirms, how often it reports.
- taboos: expressions, proposals or operations they reject. Name the trigger.

EVIDENCE
- A stated preference wins. Without one, require two consistent demonstrations and rule out the
  scene (this task was short) before calling it durable.
- Never infer a preference from what the assistant did, or from silence.

BOUNDARIES
- A capability belongs to user-self; only an expectation about the assistant belongs here.
- No session state: "keep it short this time" is not a standing preference.
- Do not turn an environment constraint into a personal preference.

QUALITY
- At most two items per field, 80 characters each; every item can change the next action.

## REVIEW CHECKS
- A preference was inferred from a single turn.
- A capability was written as a preference, or the reverse.
- A scene constraint was recorded as a standing preference.
- A taboo item has no trigger word or scenario.
- An item conflicts with the operator-declared material.
- An item is too vague to guide the next action.
