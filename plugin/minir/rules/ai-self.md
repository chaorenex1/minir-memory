---
id: ai-self
version: 4
applies_to: [base:B2, dialectic]
slot: body
max_tokens: 400
sections: [contract, review_checks]
---

## CONTRACT

WHAT
The assistant's six-field orientation for this session, projected from the identity body and the
window summary. A markdown body, session-scoped; nothing here may be carried into the identity
card.

FIELDS
- Positioning: the first line of the identity body you wrote in the same pass (role: executor,
  reviewer or pair). Do not invent a second positioning.
- Current Task: the goal sentences under window_summary current_state.doing, at most 2;
  `unknown` when Doing is empty — an empty ledger is missing evidence, never idle work.
- Current Goal: the unfinished goals (Doing + Todo) from window_summary.objective, at most 2, as
  goal sentences with no ids; "no active goal" when every goal is Done.
- Known: 1-3 verified facts that bear directly on the current state; the full list stays in the
  window summary. An unknown comes from open_issues.
- State: idle, executing, blocked or complete, mapped from Doing/Todo and open_issues.
- Uncertainty: the open_issues entries still to be verified, each with its cause. "Not read yet"
  is a next step, not an unknown.

DISTINCTIONS
- Only what the window proves counts as known; an inference stays pending until checked.
- No objective or current_state evidence: write `unknown`, never `idle`, and never guess.
- `idle` requires evidence that the session is waiting; an empty Doing produces `unknown`.
- Current Task and Current Goal are one sentence each, at most 120 characters.
- The state follows the window's last evidence: finished work is done, not executing.
- An uncertainty names a cause; "may be risky" is not an entry.

BOUNDARIES
- Never describe the user here; that is the user card.
- Never write identity, permissions, versions or tools; those belong to ai-identity.
- Never restate objective, next_actions, decisions or the full verified_facts table: point at the
  window summary.

## REVIEW CHECKS
- The stated state contradicts the window (work finished but still executing).
- The window summary's ledger was restated instead of referenced.
- More than three facts, or a fact unrelated to the current state.
- An inference was written as a known fact.
- A session observation was carried into the identity card.
- An uncertainty is an empty hedge with no cause.
- Current Task or Current Goal is longer than 120 characters or carries an id.
