---
id: ai-self
version: 2
applies_to: [base:B2, dialectic, card_review]
slot: body
max_tokens: 400
sections: [contract, review_checks]
---

## CONTRACT

WHAT
What the assistant is doing in this session: where the task stands, what it knows, where it is
stuck. A markdown body, session-scoped; nothing here may be carried into the identity card.

COVER
- How it presents itself for this task (executor, reviewer, pair).
- The problem being solved now, not the project it belongs to.
- The result this task should reach.
- Facts this window proved, each readable without the window.
- Hypotheses still to check. "Not read yet" is a next step, not an unknown.
- Its current state, in the window's own terms: idle, executing, reasoning, reviewing, blocked,
  done.
- Open risks, each with its cause ("provider route unverified after the edit").

DISTINCTIONS
- Only what the window proves counts as known; an inference stays pending until checked.
- An uncertainty names a cause; "may be risky" is not an entry.
- The state follows the window's last evidence: finished work is done, not executing.

BOUNDARIES
- Never describe the user here; that is the user card.
- Never write identity, permissions, versions or tools; those belong to ai-identity.

## REVIEW CHECKS
- The stated state contradicts the window (work finished but still executing).
- An inference was written as a known fact.
- A session observation was carried into the identity card.
- An uncertainty is an empty hedge with no cause.
- The goal restates the task in different words.
