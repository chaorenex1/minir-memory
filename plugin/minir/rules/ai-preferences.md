---
id: ai-preferences
version: 1
applies_to: [base:B2, dialectic, card_review]
slot: body
max_tokens: 500
sections: [contract, review_checks]
---

## CONTRACT

WHAT
How this assistant is required to work in this profile. A markdown body: working method, durable
across sessions, with no task detail, no permission and no tool inventory.

COVER
- Reading and retrieval: which source wins, when to search before acting, what counts as evidence.
- Execution: scope discipline, reversibility, how a change is staged and reported.
- Verification and reporting: what must be exercised before "done", how failures and unverified
  claims are labelled.
- Collaboration: when to ask, when to act on a clear instruction, how a handoff is shaped.

GROUNDING
- Every line restates an operator declaration (SOUL, dispatch, a user preference card) or a
  review conclusion; never invent a working rule the operator did not state.
- A review-sourced line has review rank: it may be revised by a later review.
- Prefer a shorter, enforceable rule over a slogan. "Run the changed path" beats "be careful".

BOUNDARIES
- No permissions, capabilities, tool lists or prohibitions: those belong to ai-identity.
- No session observations, completed work, versions or lifecycle facts.
- No user traits or task state.

## REVIEW CHECKS
- A permission, capability or prohibition written here instead of ai-identity.
- A tool list, a version, a deployment fact or a session observation.
- A rule the operator never declared, or one that widens what the assistant may do.
- The same line repeated in ai-identity and ai-preferences.
- A rule that cannot change the next action.
