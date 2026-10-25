---
id: markers-category
version: 3
applies_to: [base:B4, knowledge_render]
slot: fields
max_tokens: 1200
sections: [contract, review_checks]
---

## CONTRACT

WHAT
The category records what a later session does with the unit. The marker pass chooses it directly
from the table below; the `info_type` labels remain the decision vocabulary, not a field on the
unit. One unit, one category; a claim that needs two answers is two units. Category and org stay
independent: the category never picks the org.

WHAT THE READER DOES
- KNOWLEDGE: understands with it. What is, how it works, why; a measurement or a verified cause.
- VALUE: chooses with it. What is worth wanting, how good something is, which trade-off was taken.
- INTENT: aims with it. What a subject wants, plans or expects to reach.
- METHOD: acts with it. How a task is done: steps, commands, workflows, a reusable lesson.
- GOVERNANCE: obeys it. What binds a subject: rule, constraint, permission, prohibition, convention.

LABELS AND THE FIXED MAPPING
  info_type        category     epistemic
  fact             KNOWLEDGE    fact
  measurement      KNOWLEDGE    fact
  tool behaviour   KNOWLEDGE    fact
  project state    KNOWLEDGE    fact
  experience       KNOWLEDGE    fact
  inference        KNOWLEDGE    inference
  hypothesis       KNOWLEDGE    hypothesis
  prediction       KNOWLEDGE    prediction
  method           METHOD       none
  need             INTENT       none
  value judgement  VALUE        none
  convention       GOVERNANCE   none
  constraint       GOVERNANCE   none

These thirteen labels are the whole vocabulary. A unit carrying any other category is a gap in this
rule, not a judgement call: take the closest row only when that row's definition holds, and report
the gap instead of inventing a category.

EPISTEMIC
The table assigns it: fact, inference, hypothesis or prediction on KNOWLEDGE, forbidden on every
other category. Never promote a prediction or an untested cause to fact.

LABEL CHOICE (the three pairs that get mixed up)
- fact versus value judgement: an observed number or behaviour is fact; "too slow", "better", "worth
  it" is value judgement. A preference the subject states is value judgement even when imperative.
- need versus convention: the state the subject wants is need; what anyone must do to reach it is
  convention or constraint.
- method versus constraint: the step that works is method; the step that is mandatory, forbidden or
  otherwise limited is constraint. "Restore from a backup" is method, "verify the backup first" is
  constraint.

SPECIAL
- Describing a norm is not relabelling it: a rule quoted for what it requires is convention or
  constraint; only a study of its history, structure or cause is fact.
- A plan and its steps are two units: need for the target, method for the steps.
- An experience holds what happened and what it proved. The reusable procedure it left is a separate
  method unit; a lesson with no procedure stays with the experience.
- A constraint stated inside a complaint is still a constraint: "the root here means the root
  certificate" is constraint, and the complaint around it is not a unit.

TOPIC
- Noun phrase, 2 to 40 characters, naming the object and the property that matters. It is the dedupe
  key and the knowledge filename, so it stays stable across revisions.
- No sentence, question, bare verb, "misc", "other", "TBD".

IDENTITY AND SPLIT
- Identity is category + org + topic. The same triple revises that unit; the same topic under another
  category is a different unit and a different page.
- One page carries one category. A page whose own text holds a goal and a binding constraint is two
  pages, or the second claim is left out; it is never merged under the louder one.

## REVIEW CHECKS
- A category that is not the one its info_type maps to.
- An info_type outside the thirteen labels: a rule gap, not a category decision.
- KNOWLEDGE without epistemic, or epistemic on another category.
- A preference or a constraint whose label says fact; a goal written as "supports X".
- A plan and its steps in one unit; an experience carrying its own procedure.
- A "must", "only" or "never" sentence labelled fact, or a plain description labelled constraint.
- A page whose category covers only one of two claims in its own text.
- topic is a sentence, a question, a placeholder or over 40 characters.
