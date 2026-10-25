---
id: ai-identity
version: 2
applies_to: [base:B2, dialectic, card_review]
slot: body
max_tokens: 500
sections: [contract, review_checks]
---

## CONTRACT

WHAT
Who this assistant is in this profile and what it may do. A markdown body: identity and conduct,
durable across sessions, with no task detail.

COVER
- Who it is: name, version, type, system, creator, maintainer, when declared.
- What it is responsible for, and the domains it works in, with the depth reached.
- Its voice and the manner the operator asked for; how it phrases and structures answers.
- What it may do, and what it must not do. Restate operator text; never widen a permission or
  narrow a prohibition.

GROUNDING
- Identity claims come from the operator files or the manifest. A fact neither declares stays out.
- Never reconstruct a version, a creator or a permission from memory or from the window.
- An omitted line is better than a fabricated one.

PROVENANCE
- A front-matter `source` of review marks the periodic review's conclusion: same rank as a
  previous card, never operator authority. Operator and operator_edit are declarations.
- A reviewed line never overrides an operator-declared line.

BOUNDARIES
- No tools, completed work, session observations or user traits.
- No permission, capability or prohibition the operator did not write.

## REVIEW CHECKS
- A version, a creator or a maintainer the operator never declared.
- A permission or prohibition was added that the operator never wrote.
- A session observation (a tool used, work done) was written into the identity.
- A review-sourced line is used as if the operator had declared it.
- A version number, a lifecycle list or a project state that will drift out of date.
- The same fact appears in the identity and preference bodies.
- A line describes the task or the user instead of the assistant.
