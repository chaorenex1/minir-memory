---
id: user-self
version: 3
applies_to: [base:B1, dialectic, card_review]
slot: body
max_tokens: 400
sections: [contract, review_checks]
---

## CONTRACT

WHAT
Who this person is, apart from the task and from how the assistant should behave. A markdown body,
short headings and bullets. An empty body is a correct answer.

COVER
- Occupation, field or responsibility, in one phrase, when stated.
- Skills: languages, frameworks, infrastructure actually used; mark learning ones "learning".
- Thinking habits that recur (systems, causal, first principles, risk).
- Durable interests, naming the field when the window does.
- How they express and coordinate work; a capability, not a tone request.
- What they optimise for and want to reach; never the task goal.

EVIDENCE
- Stated in the window, or shown more than once across the baseline and the window.
- One example is not a pattern. The baseline is never evidence for itself.

BOUNDARIES
- How the assistant should talk, format or proceed belongs to user-preferences.
- Current task and next step belong to the window summary.
- Never infer a job title from a tool used once.

QUALITY
- One fact per line; every line stands alone outside the window.
- Prefer 6 to 10 sharp lines over a transcript.

## REVIEW CHECKS
- A single example was promoted to a long-term pattern.
- A preference about the assistant was written here instead of user-preferences.
- The current task goal leaked into the card.
- The same fact appears in both user bodies.
- A role, employer or skill has no support in the window or baseline.
- A count, a version, a project state or an expiry date that will drift out of date.
- The same fact appears in the identity and preference bodies.
- A line is too vague to act on.
- A line was removed or reworded without an explicit user request to remove it.
