REVIEW BUDGET
- Return at most $MAX_ADVICES advice objects; at most $MAX_NOTES are accepted per review
  update (a blocker is always published and does not count against that budget).
- Severity floor: $FLOOR; omit anything below it.
- If nothing is wrong, return exactly {"action": "silent"}; no acknowledgements, no restating
  what the agent already knows.
