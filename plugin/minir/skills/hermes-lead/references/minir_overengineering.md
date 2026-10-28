# Rule file: overengineering

What this governs: every file, line, dependency, config key, abstraction, and defensive branch
an agent adds or removes. Explorer and researcher follow it as a limit on what they recommend,
not only on what they build.

Used by: the lead at steps 7 and 8; `minir_worker` for every code slice; `minir_explorer` (W1, W6)
and `minir_researcher` (W2 recommendation) when the dispatch names it. Loaded on demand with
`skill_view`; task judgment never loads it.

Why this file exists instead of pointing at the skill alone: a named subset can be checked at
handoff against the actual diff, and the skill's curative mode needs a green baseline before it
is allowed to delete anything. The skill stays the reference; this file is the gate.

## Load block

Edit this block only. Paths in `rule_files` resolve under `$HERMES_HOME` unless absolute. Rule
names below are this file's checks; they map to sections of the skill, not to official IDs.

```yaml
mode: preventive
skills:
  - anti-overengineering                     # always
rule_files: []
rules:
  - deletion-ladder
  - five-pre-flight-questions
  - reuse-before-write
  - no-speculative-abstraction
  - smallest-change
  - boundary-validation-once
  - fail-loud
  - no-placeholder-code
  - behavior-tests-only
  - no-version-suffix
  - no-integrity-layer-over-git
  - file-budget-250
  - chestertons-fence
```

### How to execute the block

1. `mode: preventive` applies while writing. `mode: curative` is cleanup of existing code and
   requires a green baseline before the first deletion.
2. Load each `skills` entry with `skill_view(name=<name>)` when that tool exists. A conditional
   entry loads only when its condition holds.
3. Read each `rule_files` entry with `read_file`.
4. The listed `rules` are the subset enforced and named at handoff. Unlisted guidance in the
   skill does not become a gate.
5. If this file or a listed `rule_files` entry cannot be read, stop and report the exact path.
   Do not run from remembered rules.

## The contract

These hold even when no skill could be loaded.

1. **Deletion ladder before new code.** In order: delete it (not needed), reuse what the repo
   already has, use the platform (stdlib, installed dependency, native behavior), simplify in
   place. Only code that survives all four is written.
2. **Smallest change wins.** Edit an existing file before adding one. A new file is allowed only
   when no existing file owns the responsibility; the handoff names the search that proved it.
3. **No speculative structure.** No interface with one implementation, no factory with one
   product, no options object with one caller, no event system with one subscriber, no manager
   class around two functions. A second real user is the entry price.
4. **A new dependency needs a named gap.** Stdlib and installed packages do not cover it, the
   reason is one line, and the handoff names version and license cost. Otherwise use what is
   installed.
5. **Defensive code needs a named, observed failure.** Validate once at the boundary (user
   input, network, disk) and fail loud. No broad `except`, no silent fallback, no retry chain, no
   default that hides a missing value. The only allowed catch is the top-level handler that logs
   and exits non-zero.
6. **No placeholder deliverable.** No `TODO`, stub, mock standing in for the real dependency,
   empty function body, or "coming soon". If it cannot be complete, report the blocker.
7. **Versioning is git.** No `_v2`, `_final`, `_new`, hash lock, checksum file, or signature
   layer over content git already tracks. A variant is a branch or a tag.
8. **Tests pin behavior at one real seam.** No test for a test, no tautological assertion, no
   assertion that cannot fail, no deleting a failing test to go green. One narrow regression per
   behavior is enough; a prose file has no behavioral seam and gets no phrase pin.
9. **One responsibility per file.** The budget is 250 pure LOC (non-blank, non-comment). A change
   that would cross it names the split before writing, and the split follows responsibilities,
   never `utils.py` or `helpers.py`.
10. **Curative mode starts green.** Lock the behavior with a passing test before removing a line.
    Remove by category, highest certainty first. Revert a file that breaks. After three failed
    attempts on one file, stop and report.

## Guardrails

- Keep boundary validation, domain complexity, public API signatures, type hints, WHY comments,
  reflection targets, and any hardening the user asked for.
- Follow an existing repo pattern even when a shorter one exists. A local minimum is not worth
  inconsistent code.
- Never introduce a new abstraction or dependency during cleanup.
- A guard at a trust boundary stays unless an adversarial test proves it redundant.

## Handoff gate

Answer each row from the diff, not from intent.

| Check | Evidence |
|---|---|
| Every new file had no existing owner | the search that showed the gap |
| New dependencies | name and the gap stdlib did not cover, or `none` |
| New abstractions | their second real user, or `none` |
| Defensive branches | the named failure each one answers, or `none` |
| Placeholders in delivered code | `none` |
| Version suffixes or integrity layers | `none` |
| Largest changed file under 250 pure LOC | the count |
| Tests added pin behavior, none deleted | test names and result |

Report the mode and the counts in one line, for example
`overengineering: preventive (0 new files, 0 new deps, 1 reused helper)`.
