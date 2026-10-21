---
id: user-self
version: 1
applies_to: [base:B1, dialectic, card_review]
slot: fields
max_tokens: 350
sections: [contract, review_checks]
---

## CONTRACT

WHAT
Who this person is, apart from the task and from how the assistant should behave. Six optional
fields; an empty array is a correct answer.

FIELDS
- occupation: role, field, responsibility. One phrase; omit when unstated.
- skills: languages, frameworks, infrastructure the person uses. Mark learning ones "learning".
- thinking_models: recurring reasoning habits (systems, causal, first principles, risk).
- interests: durable interests and fields being explored; say which when the window does.
- communication: how they express and coordinate work. A capability, not a tone request.
- values_motivation: what they optimise for and want to reach; never the task goal.

EVIDENCE
- Stated in the window, or shown more than once across the baseline and the window.
- One example is not a pattern. The baseline is never evidence for itself.

BOUNDARIES
- How the assistant should talk, format or proceed belongs to user-preferences.
- Current task and next step belong to the window summary.
- Never infer a job title from a tool used once.

QUALITY
- At most two items per field, 80 characters each; self-contained; one fact in one field.

## REVIEW CHECKS
- A single example was promoted to a long-term pattern.
- A preference about the assistant was written into communication.
- The current task goal leaked into the card.
- One fact appears under two fields.
- A role, employer or skill has no support in the window or baseline.
- An item is too vague to act on.
