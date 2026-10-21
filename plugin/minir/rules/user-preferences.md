---
id: user-preferences
version: 2
applies_to: [base:B1, dialectic, card_review]
slot: body
max_tokens: 400
sections: [contract, review_checks]
---

## CONTRACT

WHAT
How this person wants the assistant to work. A markdown body. A constraint that held for one turn
is scene, not preference.

COVER
- Language, tone, length, level of detail.
- Shape of an answer: conclusion or analysis first; lists or tables; code or prose.
- Method: plan first, staged delivery, verify before advancing, smallest reversible change.
- Decision rule: which criterion wins, evidence, cost, speed or robustness.
- Toolchain, runtime, deployment environment, version-control habits.
- Collaboration: how much the assistant drives, when it confirms, how often it reports.
- Taboos: expressions, proposals or operations they reject. Name the trigger.

EVIDENCE
- A stated preference wins. Without one, require two consistent demonstrations and rule out the
  scene (this task was short) before calling it durable.
- Never infer a preference from what the assistant did, or from silence.

BOUNDARIES
- A capability belongs to user-self; only an expectation about the assistant belongs here.
- No session state: "keep it short this time" is not a standing preference.
- Do not turn an environment constraint into a personal preference.

QUALITY
- Every line changes the next action; a line that changes nothing is noise.

## REVIEW CHECKS
- A preference was inferred from a single turn.
- A capability was written as a preference, or the reverse.
- A scene constraint was recorded as a standing preference.
- A taboo line has no trigger word or scenario.
- A line conflicts with the operator-declared material.
- A line is too vague to guide the next action.
