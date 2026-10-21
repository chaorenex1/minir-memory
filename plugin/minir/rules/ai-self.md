---
id: ai-self
version: 1
applies_to: [base:B2, dialectic, card_review]
slot: fields
max_tokens: 350
sections: [contract, review_checks]
---

## CONTRACT

WHAT
What the assistant is doing in this session: where the task stands, what it knows, where it is
stuck. Session-scoped; nothing here may be carried into the identity card.

FIELDS
- positioning: how it presents itself for this task, in one phrase (executor, reviewer, pair).
- current_task: the problem being solved now, not the project it belongs to.
- current_goal: the result this task should reach.
- known: facts this window proved, each readable without the window.
- unknown: hypotheses still to check. "Not read yet" is a next step, not an unknown.
- state: exactly one of idle, executing, reasoning, reviewing, blocked, done.
- uncertainty: open risks, each with its cause ("provider route unverified after the edit").

DISTINCTIONS
- known holds only what the window proves; an inference stays in unknown until checked.
- uncertainty names a cause; "may be risky" is not an entry.
- state follows the window's last evidence: finished work is done, not executing.

BOUNDARIES
- Never describe the user here; that is the user card.
- Never write identity, permissions, versions or tools; those belong to ai-identity.

## REVIEW CHECKS
- state contradicts the window (work finished but still executing).
- An inference was written into known.
- A session observation was carried into the identity card.
- uncertainty is an empty hedge with no cause.
- current_goal restates current_task in different words.
