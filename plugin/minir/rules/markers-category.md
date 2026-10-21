---
id: markers-category
version: 1
applies_to: [base:B4, knowledge_render]
slot: fields
max_tokens: 500
sections: [contract, review_checks]
---

## CONTRACT

WHAT
What one knowledge unit says. Exactly one primary category.

CATEGORIES
- KNOWLEDGE: facts, definitions, mechanisms, measurements.
- VALUE: judgements, priorities, what is worth pursuing.
- INTENT: goals, desired outcomes.
- METHOD: procedures, commands, techniques, workflows.
- GOVERNANCE: rules, permissions, prohibitions, compliance.

EPISTEMIC
- KNOWLEDGE only: fact, inference, hypothesis or prediction. Required; any other category
  carrying one is an error.

TOPIC
- A noun phrase of 2 to 40 characters naming the subject. It is the dedupe key and the knowledge
  filename, so it stays stable. No sentences, questions, "misc", "other" or a bare verb.

IDENTITY
- category + org + topic. The same triple revises one unit; it never adds a second.

DISAMBIGUATION (first match wins)
- what it is / whether it matters: K / V
- existing fact / wanted result: K / I
- why or what / how to operate: K / M
- reality / allowed, forbidden, required: K / G
- worth pursuing / binding requirement: V / G
- evaluation stance / desired state: V / I
- criterion or trade-off / implementation: V / M
- purpose or result / steps or means: I / M
- wants to do / must obey: I / G
- how it is done / mandated or forbidden: M / G

SPECIAL
- Describing a rule is not the rule ("the repo requires X" is K; "X must happen" is G).
- Split a plan (I) from its steps (M).
- Keep fact, inference, hypothesis and prediction apart.

## REVIEW CHECKS
- Two primary categories.
- KNOWLEDGE without epistemic, or another category carrying one.
- An evaluation written as a fact, or a fact as a goal.
- Plan and steps in one unit.
- topic is a sentence, a question or a placeholder.
- A quoted rule classified as KNOWLEDGE.
