# Rule file: antislop

What this governs: every sentence an agent writes. Reports, plans, code comments, commit
messages, user-facing strings. It governs UI only when the task produces UI.

Used by: the lead at step 9 and W8; `minir_explorer` (W1, W5), `minir_researcher` (W2, W5), and
`minir_worker` (W3 to W7) when the dispatch names it. Loaded on demand with `skill_view`;
task judgment never loads it.

Why this file exists instead of pointing at the skill alone: a named subset can be checked at
handoff, and the skill's install wizard and usage-mode question have no operator to answer
inside a subagent. The skill stays the reference; this file is the gate.

## Load block

Edit this block only. Paths in `rule_files` resolve under `$HERMES_HOME` unless absolute.

```yaml
mode: during
skills:
  - antislop                                  # always
  - antislop-code                             # conditional: the task edits code comments
  - antislop-copywriting                      # conditional: the output is user-facing prose
rule_files: []
rules:
  - R-02
  - R-15
  - R-16
  - R-17
  - R-18
  - R-31
  - R-35
  - R-36
  - R-38
  - C-1
  - C-3
  - C-5
```

### How to execute the block

1. `mode: during` applies the rules while writing and runs the handoff gate. `mode: after`
   audits only: a numbered findings list, no edits.
2. Load each `skills` entry with `skill_view(name=<name>)` when that tool exists. A conditional
   entry loads only when its condition holds. A missing tool is skipped, not an error.
3. Read each `rule_files` entry with `read_file`.
4. The listed `rules` are the subset enforced and named at handoff. Unlisted rules in the skill
   remain guidance, not gates.
5. Never run the antislop install wizard and never ask the usage-mode question. This file is the
   mode decision; an agent has no operator to ask.
6. If this file or a listed `rule_files` entry cannot be read, stop and report the exact path.
   Do not run from remembered rules.

## The contract

These hold even when no skill could be loaded.

1. **Evidence over claims.** A factual sentence carries `path:line`, a command with its output,
   or a URL with its access date. Without a source it is labeled `unverified` or removed. (C-5)
2. **Nothing invented.** No number, benchmark, API, quote, user, testimonial, or test result
   that a source does not contain. "No data" is an acceptable answer. (R-17, R-18, R-36, R-38)
3. **No em dash in English prose.** Use a comma, colon, or parentheses. In Chinese prose use
   Chinese punctuation; the English copy rule does not transfer. (R-02)
4. **Every recommendation has a one-line reason.** If the reason needs a paragraph, the
   recommendation is not ready. (R-31)
5. **No filler vocabulary.** Seamless, robust, powerful, comprehensive, leverage, cutting-edge,
   revolutionary, effortless, world-class. Name the behavior or delete the sentence. (R-16)
6. **No template sections.** A heading exists because the content needs it. "Conclusion", "Key
   takeaways", and "Next steps" that restate the body are deleted. (C-3)
7. **No intent narration.** Do the step, then report what it returned. "I will now search" is
   deleted; the search result stays. (C-1)
8. **Delivered means exercised.** A claim of working behavior names the command that ran and the
   line that proves it. (R-35)

## Scope by output

- **Code, reports, plans:** rules 1 to 8.
- **UI output:** add the skill's checks for contrast, keyboard, mobile, and states. Gate items
  without a counterpart in this task are recorded as `not applicable`, with the reason.
- **Copy for users:** add R-15 and R-16 to every string, not only the headline.

## Handoff gate (during mode)

Answer each row. A FAIL is fixed before returning, not explained.

| Check | Evidence |
|---|---|
| Every claim has a source or is labeled unverified | the labels used |
| No invented number, quote, API, or result | the command or URL behind each number |
| No U+2014 in English text | the search that found none |
| Every recommendation has a reason | one line per recommendation |
| No template section or intent narration | the sections removed |
| Completed behavior was exercised | command and the passing line |

Report the mode and the PASS/FAIL count in one line, for example
`antislop: during (R-02, R-16, R-31, R-35, C-5), 6 PASS`. A FAIL line names the rule and the
text to change.
