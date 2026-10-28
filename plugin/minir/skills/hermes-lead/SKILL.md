---
name: hermes-lead
description: "Use when coordinating explorer, researcher, and worker."
version: 1.1.0
author: Masia
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [lead, coordinator, explorer, researcher, worker, dispatch, minir]
    related_skills: [hermes-dispatch, durable-agent-orchestration]
---

# Hermes Lead

The main session is the lead. It owns scope, authorization, acceptance, and the final report. For a
new user message it runs task judgment itself against `references/jev.md`, then scopes and routes.
It does not dispatch an agent for the judgment. The three roles gather evidence and execute one
slice each. `hermes-dispatch` is the control loop; this skill is the routing and handoff contract on
top of it. Load `hermes-dispatch` first if it is not already active.

## Roles

| Role | Agent name | Answers | Returns | Refuses |
|---|---|---|---|---|
| Explorer | `minir_explorer` | What is true in this repo? | Question, Answer, Evidence, Flow, Reuse, Unknowns, Next probe | Editing files, guessing contents, citing unread code |
| Researcher | `minir_researcher` | What is true outside this repo? | Question, Answer, Sources, Conflicts, Inference, Unverified, Recommendation | Inventing sources, treating one source as settled, writing the project |
| Worker | `minir_worker` | What is the smallest change that passes the check? | Change, Verification, Rules check, Remaining risk | Widening the slice, placeholders, weakening tests, bypassing the write guard |

Exposed by the minir plugin as `minir:hermes-lead`. The prompts, the rules, and the model route ship
inside this skill: `agents/minir_*.md`, `references/*.md`, and `references/model_route.yaml` in the
source tree, or `$HERMES_HOME/plugins/minir/skills/hermes-lead/` when installed. The role names
above are the `agent_name` values this plugin's `minir_agents` tool resolves with
`skill_name="hermes-lead"`.

## Rules

The rules live in `references/`:

- `references/jev.md`: task judgment.
- `references/minir_antislop.md`: output quality.
- `references/minir_overengineering.md`: code economy.

Read one with `skill_view(name="minir:hermes-lead", file_path="references/<file>.md")`. For an agent,
name the rule file in `user_prompts` and require the same call; the agent loads it on demand. Grant
the `skills` toolset to any agent that will load a rule; alternatively, paste the rule text in
`user_prompts` and label it as a rule. Do not pass rule files through `context_files`; that channel
labels them as data. The base contract in each agent prompt still applies when no rule is loaded.
Each rule file opens with an editable `load block` (`mode`, `skills`, `rule_files`, `rules`); edit
that block, not the contract below it.

Task judgment is the lead's own step: it runs under this file and `references/jev.md`, and the two
craft rules keep the load points below.

### Rule load points

Both craft rules are on demand. A load point names a rule file only when its trigger holds. Naming
neither file when a row applies, or both on every dispatch, is a dispatch defect. The lead loads the
same file it injects, so one rule governs the role's handoff and the lead's gate.

| Load point | Rule file | Trigger |
|---|---|---|
| Lead, step 7 acceptance and step 8 cleanup | `minir_overengineering.md` | the diff adds or removes a file, dependency, config key, abstraction, defensive branch, or test |
| Lead, step 9 deliver, and W8 document | `minir_antislop.md` | the lead writes the user-facing report or the document |
| `minir_worker`, W3, W4, W7 | `minir_overengineering.md` | always: the slice changes code |
| `minir_worker`, W3, W4, W6, W7 | `minir_antislop.md` | the slice edits comments, docstrings, user-facing strings, UI copy, or a repo document |
| `minir_explorer`, W1 and W5 | `minir_antislop.md` | the report is the answer the user reads, or is quoted into the verdict table |
| `minir_explorer`, W1 and W6 | `minir_overengineering.md` | the answer names what would have to change, or the inventory lists dead code, single-impl interfaces, or version copies |
| `minir_researcher`, W2 and W5 | `minir_antislop.md` | the report is the answer the user reads, or is quoted into the verdict table |
| `minir_researcher`, W2 | `minir_overengineering.md` | the recommendation names a dependency, service, or abstraction |

The role states the rules it loaded on one line. The lead checks that line against the dispatch it
sent; a rule missing on an applying row goes back to the role. When the runtime cannot resolve
`minir:hermes-lead`, the lead pastes the rule text into `user_prompts` and labels it a rule. A load
point is never dropped because the skill lookup failed.

## Model routes

`references/model_route.yaml` assigns a provider and a model to each dispatched role. Load it once
per session, before the first dispatch:

```text
skill_view(name="minir:hermes-lead", file_path="references/model_route.yaml")
```

Every `minir_agents` call for `minir_explorer`, `minir_researcher`, or `minir_worker` passes that
role's pair as one route string: `model="<provider>:<model>"`. The run payload echoes the `provider`
and `model` that actually served the run, so a route that did not land is visible there rather than
inferred. A role with no entry, or a file that will not load, runs on the gateway's route and the
final report names the gap. Task judgment is a lead step and has no route.

## Lead loop

1. **Task judgment.** Load `references/jev.md` and classify the message yourself: class, agents,
   workflow, self-check, repair loop, gates, and stop, written in the rule's JSON shape. No agent
   runs this step. A `needs_lead` fact is resolved, or asked as one question, before any role runs.
   Completion: a plan exists before any role runs. Do not skip the judgment, and do not send it to
   a role.
2. **Scope.** Write the goal, the acceptance rubric from the user's requirement or the document's
   plan, the non-goals, and the authorized slice. Each rubric item names the evidence that settles
   it and the narrowest check that produces it. Completion: every user requirement or plan item has
   a rubric row.
3. **Track.** Register one Kanban goal card (assignee: the lead's profile, initial status running,
   body: goal + rubric + judgment plan) and track its steps in `minir_todo`: a top-level item for the
   card, child items for the steps. Send the complete list on every update; `minir_todo` replaces
   the session list. Do not create a Kanban card per step. A direct answer with no dispatched role
   may skip the card only if the todo records `no card: direct answer`.
4. **Route.** Pick the smallest set of roles that settles the question. Completion: each dispatch
   answers one question or changes one slice.
5. **Evidence gate.** Open the cited lines, check the dates, resolve or accept the unknowns.
   Completion: every claim passed to the worker has an address that opens.
6. **Implement.** Give the worker the slice, the acceptance command, the explorer's Reuse section,
   the researcher's constraints, and the rule files its load point names. Completion: the worker
   has everything it needs without re-searching.
7. **Acceptance gate.** Load `minir_overengineering.md` for the diff gate, rerun the check in the
   main thread at the scope the change needs (see Test scope), read the diff, and map every rubric
   item to evidence. Completion: each item is verified or a named blocker; no downgrade.
8. **Cleanup.** Worker removes its own scaffolding; the lead checks for leftovers and unrequested
   docs. Completion: no scaffolding, no scope creep.
9. **Deliver.** Load `minir_antislop.md`, then report verified, unverified, and risk. Completion: no
   claim without a command or a label. Only the lead talks to the user.

## Workflows

### W1 Internal answer (explorer only)

Trigger: "Where is X", "how does Y flow", "what would have to change".

Dispatch: lead frames one question -> `minir_explorer` -> evidence gate -> lead answers.

Gate: every claim has `path:line`; Reuse names existing owners; Unknowns lists the failed searches.

Stop: the explorer's three-search limit. No worker.

### W2 External answer (researcher only)

Trigger: library behavior, version, spec, cost, prior art.

Dispatch: lead frames the decision the fact informs -> `minir_researcher` -> source gate -> lead
answers.

Gate: URL plus access date per claim; single-source claims labeled; conflicts named.

Stop: eight fetched pages. No worker.

### W3 Scoped change (explorer -> worker -> lead)

Trigger: add, fix, or refactor a named behavior.

Dispatch:

1. Lead writes the acceptance command and the file slice.
2. `minir_explorer` returns Evidence, Reuse, and Unknowns.
3. Gate: open the cited lines. A bad citation goes back to the explorer.
4. `minir_worker` gets the slice, the acceptance command, the Reuse list, and the constraints.
5. Worker returns Change, Verification, and Rules check.
6. Lead reruns the acceptance command, reads the diff, and either accepts or returns the exact
   failing output.

Gate: acceptance passes in a fresh process; the diff touches only authorized files; no placeholder,
new dependency, or new abstraction without the overengineering handoff evidence.

Stop: the worker reports the blocker after two different methods; do not request a third variation
of the same command. The lead reports the blocker.

### W4 Failure diagnosis (explorer -> worker -> lead)

Trigger: failing test, crash, regression, wrong output.

Dispatch: lead reproduces the failure -> `minir_explorer` traces the cause from the failing path ->
gate -> `minir_worker` fixes the cause -> lead reruns the original failing command and the suite the
change touches.

Gate: the original failure passes; no test was deleted or weakened; the cause, not the symptom,
changed.

Stop: if the cause is external, switch to the researcher; if two different fixes fail, stop the loop.

### W5 Design verification (explorer plus researcher, no worker)

Trigger: "is this design sound", "can we build X this way", "which approach".

Dispatch: lead splits the design into claims -> explorer and researcher run in parallel on the
claims each can settle -> lead writes 可行 / 不成立 / 需变通 per claim with the evidence.

Gate: every verdict has `path:line` or a dated source; unverified claims are labeled, not decided.

Stop: no worker unless the decision is to implement; then open W3.

### W6 Curative cleanup (explorer -> worker)

Trigger: simplify, remove overengineering, delete dead paths.

Dispatch: lead sets a green baseline -> explorer inventories candidates (dead code, single-impl
interfaces, version copies, unused config) -> gate -> worker removes by category with tests -> lead
reruns the baseline. The lead loads `minir_overengineering.md` in curative mode before the first
deletion; the explorer and the worker load it on the rows the load-point table names.

Gate: baseline green before and after; no new abstraction or dependency; one category per pass.

Stop: revert a file that breaks; two different fixes failing on one file stop the loop.

### W7 Parallel changes on disjoint files (explorer -> workers, git worktrees)

Trigger: independent changes with mutually exclusive file ownership.

Dispatch:

1. Lead partitions by file boundary and starts from a commit. If the primary checkout is dirty,
   commit or stash with the user's authorization; a worktree starts from HEAD, not the working tree.
2. Create one worktree and one branch per worker, under
   `$HERMES_HOME/plugins/minir/worktree/`:
   `git -C <primary> worktree add "$HERMES_HOME/plugins/minir/worktree/<task>-<n>" -b "minir/<task>-<n>" <base-ref>`
3. One explorer report or one per area. Dispatch one worker per partition with `project_dir` set to
   its worktree, its slice, and its acceptance.
4. Lead merges one branch at a time and runs the integration check in the primary checkout.

Gate: file sets are disjoint; each worktree's check passes; the integration check covers the
cross-file behavior; no worker touched the primary checkout or another worktree.

Stop: if the file sets overlap, serialize. On a conflict, rebase that worktree onto the new base or
serialize; do not merge a conflicted branch. Remove the worktree after a merge; keep it on failure
for inspection.

### W8 Draft or design document (lead plus research, no worker)

Trigger: "draft", "design doc", "plan" under the dispatch draft rule.

Dispatch: lead writes the document; explorer and researcher supply cited evidence; no worker, no
wiring, no call-site edits. The lead loads `minir_antislop.md` for the document.

Destination: `<project>/docs/<type>/<name>.md`, one subdirectory per document type (`draft`,
`design`, `plan`). Create the subdirectory; never write directly under `docs/`. If the project root
is the user's home directory or no project root is known, use the user's Documents folder:
`%USERPROFILE%/Documents/<type>/<name>.md` on Windows, `~/Documents/<type>/<name>.md` elsewhere.

Gate: every claim cited or labeled; open decisions listed; no half-applied patches.

Stop: deliver the document path under the destination rule and what remains unverified.

## Task judgment

Task judgment is the lead's own first step for every new user message, not a dispatched role. The
lead loads `references/jev.md` with `skill_view` and applies it: class, agents, workflow,
self-check, repair loop, gates, stop. It reads intent and target, not keywords, and reads no project
files; the explorer gathers the facts. A question plus a change splits into ordered subtasks.

There is no judgment agent and no `minir_agents` call for this step. The judgment decides the route;
later evidence may revise it with a one-line reason, and the lead still owns scope and acceptance.

## Dispatch recipes

Explorer and researcher:

```text
minir_agents(
  agent_name="minir_explorer",
  skill_name="hermes-lead",
  user_prompts=[
    "<prior report, verbatim, when there is one>",
    "<one question>. What settles it: <acceptance>. Return your Output contract.",
  ],
  project_dir="<repo>",
  model="<provider>:<model>",  # this role's pair in references/model_route.yaml
)
```

Worker:

```text
minir_agents(
  agent_name="minir_worker",
  skill_name="hermes-lead",
  user_prompts=[
    "<explorer Evidence/Reuse/Unknowns plus researcher constraints, verbatim>",
    "<change>. Acceptance: <command that must pass>. Slice: <files>. Non-goals: <...>.",
  ],
  project_dir="<repo>",
  model="<provider>:<model>",  # this role's pair in references/model_route.yaml
)
```

Parallel workers:

```text
# once, from a committed base
git -C <primary> worktree add "$HERMES_HOME/plugins/minir/worktree/<task>-<n>" -b "minir/<task>-<n>" <base-ref>

minir_agents(
  agent_name="minir_worker",
  skill_name="hermes-lead",
  user_prompts=[
    "<explorer report plus the partition boundary, verbatim>",
    "<change>. Acceptance: <command>. Slice: <files>. Worktree: <path>. Return the branch, commit, and diff stat.",
  ],
  project_dir="<worktree>",
  model="<provider>:<model>",  # this role's pair in references/model_route.yaml
)
```

The base directory is `$HERMES_HOME/plugins/minir/worktree/`. One worktree and one branch per
worker; no two workers share either. A worktree starts from HEAD, so a dirty primary checkout must
be committed or stashed with the user's authorization first. Merge one branch at a time, run the
integration check, then remove the worktree. Keep a failed worktree for inspection.

A `minir_agents` worker is scratch-scoped: it can read a worktree but cannot write it through the
file tools. Run worktree workers on a runtime that can write the worktree (Kanban or delegation),
or have the lead apply the worker's diff. Never let the worker use `terminal` to bypass the guard.

Rules:

- Pass `skill_name="hermes-lead"` to every `minir_agents` call; the prompts live in this skill's
  `agents/` directory.
- Pass the role's `provider` and `model` from `references/model_route.yaml` as the one route string
  `model="<provider>:<model>"`, and check the payload's `provider` and `model` echo. A route absent
  from the echo did not land; it is a dispatch defect, not a model quirk.
- One question per explorer, one slice per worker.
- Pass prior reports verbatim, not as a summary: inside `user_prompts` (a list is joined with blank
  lines), never in `background`, which is the boolean run-mode flag. The worker opens the cited
  lines.
- `minir_agents` is asynchronous by default: the tool result is a job handle (`status` running or
  queued, `job_id`, `queue_position`, `transcript_path`), not the report. The report arrives in a
  later turn as an `[ASYNC DELEGATION COMPLETE]` notification, and `transcript_path` only exists
  once the run ends, so read it after that turn rather than on the handle. Pass `background=false`
  when the next step needs the answer in the same turn (one small explorer).
- The role's system prompt carries its output contract; `user_prompts` carries the task, the
  acceptance, the non-goals, and the slice.
- If the runtime accepts `tool_sets`, grant the narrowest surface: explorer and researcher read-only,
  worker file and terminal. If the tool is not exposed, use the runtime's delegation tool and
  require the same output sections; do not drop the gates.
- When the worker's write is blocked, it returns a diff. The lead applies it or switches runtime.
  The worker must not use `terminal` to route around the write guard.

## Gates

Evidence gate (after explorer or researcher):

- Every factual claim has `path:line` or a URL with an access date.
- The cited line opens and says what the report claims.
- Unknowns list the failed searches; no theory is substituted.
- Reuse names existing owners, or states the search found none.
- Single-source claims are labeled.

Test scope (lead and worker):

- The check exercises the behavior after the change. A non-code file is confirmed by size, line
  count, or hash; running it proves nothing about the edit, and re-reading the edited file is not a
  check.
- The scope is the requirement and the current goal: the narrowest check that can fail for this
  change, then the suite the change touches. The full suite runs when the codebase changed
  materially; e2e and smoke only when the user asks.
- The expected output is stated before the command runs. A mismatch is a finding: name the
  difference, change one variable. Rerunning for a different-looking output is not a step.

Acceptance gate (after worker):

- The lead reran the acceptance command; it did not read the worker's summary.
- The process started after the code change, or the state came from another process.
- The diff touches only the authorized files.
- No placeholder, stub, mock, new dependency, or new abstraction without the overengineering
  handoff evidence.
- The Rules check says which rules were injected; the lead verified the counts in the diff.

Delivery gate (lead):

- Verified and unverified are explicit.
- Every rubric item is reported as verified, blocked, or unverified.
- No invented number, quote, API, or test result.
- The report answers the user's question, not the plan's stages.
- The lead's own report passes the antislop rules.

## Role acceptance

The lead accepts each role against the user's requirement or the document's plan, never against the
role's self-report. Build the rubric before dispatch: one row per requirement or plan item, with the
evidence that settles it.

| Role | Accepted when |
|---|---|
| Explorer | every rubric question has a `path:line` evidence row; Reuse and Unknowns are complete; no narrower question |
| Researcher | every external rubric question has a dated source; single-source and conflicts are labeled; the recommendation covers the rubric |
| Worker | every rubric item has a passing check or a named blocker; no downgrade; the diff stays in the authorized slice |

A worker result that passes its own check but narrows the behavior or skips a rubric item is
rejected. A role that reports `needs_lead` or a blocker is not a failed acceptance; the lead
resolves the missing fact or reports the blocker.

## Tracking

- **Kanban goal card.** One card per task, assigned to the lead's profile with initial status
  running, so the dispatcher does not spawn a worker for it. The body carries the goal, the user's
  requirement or the document plan, and the acceptance rubric. Complete it with `kanban_complete`;
  block it with `kanban_block` and the matching kind.
- **`minir_todo` steps.** A two-level list: the top-level item names the goal card, the child items
  are the card's steps. Statuses are `pending`, `in_progress`, `blocked`, `completed`, and
  `cancelled`. Use `blocked` for a step stopped by a named blocker, and put the reason in its
  content. Send the complete list on every update; the tool replaces the session list. The todo
  list is session-bound; the card is the durable record.
- **Do not duplicate.** Do not create a Kanban card per step, and do not let a role create either
  tracker. A direct answer with no dispatched role may skip the card only if the todo records
  `no card: direct answer`.
- **Missing tools.** If `minir_todo` or the Kanban tools are not exposed, use the available tracker
  and state the gap in the final report; do not silently drop tracking.

## Failure routing

| Symptom | Route |
|---|---|
| Citation does not resolve | Back to explorer with the exact path; do not pass it down |
| Researcher has one source | Lead decides, labels low confidence, or asks for a second source |
| Worker write blocked | Lead applies the diff or switches runtime; no terminal bypass |
| Acceptance fails | Worker with the exact command and output |
| Failure disproves an assumption | Explorer with the failure as the new question |
| Two different fixes fail | Stop, report the blocker |
| External behavior in doubt | Researcher, then re-gate |

## Lead only

- Goal, acceptance rubric from the user's requirement or the document plan, non-goals, authorized
  slice.
- Route and parallelism.
- Kanban goal card and `minir_todo` steps.
- Document type and destination (`docs/<type>/` or Documents).
- Design verdicts (可行 / 不成立 / 需变通).
- Final verification.
- Commit, push, history changes.
- User communication and the word "done".
- Rollback and interruption handling.

## Pitfalls

- Do not chain roles. Explorer does not call researcher; worker does not call explorer. Every hop
  goes through the lead.
- Do not skip task judgment on a new user message, and do not dispatch it to an agent; a wrong route
  costs more than the classification.
- Do not dispatch a role on the gateway route when `references/model_route.yaml` names one for it.
- Do not dispatch a worker without an acceptance check; it will invent one.
- Do not accept a worker's green as verification; rerun it.
- Do not accept a role's self-report; check it against the user's requirement or the document plan.
- Do not let a worker downgrade scope; a partial result is a blocker, not done.
- Do not create a Kanban card per step; one goal card, steps in `minir_todo`.
- Do not treat a worker's remaining risk as an accepted rubric item.
- Do not use a researcher for internal behavior or an explorer for external versions.
- Do not pass a paraphrase of a report; pass the artifact.
- Do not run all three roles when one answers the question.
- Do not put two parallel workers in one worktree or one branch; the lead owns merge order.
- Do not let a worker use `terminal` to route around the scratch write guard.
- Do not verify with a process that started before the change.
- Do not widen a local change to the full suite, e2e, or smoke; the scope is the requirement and the
  current goal.
- Do not rerun a command whose output already contradicted the expectation; name the difference
  first.
- Do not let a role write the final user report.
- Do not append a second summary that restates the worker's report; the lead's value is the gate.

## Verification

- [ ] The three agent names resolve with `skill_name="hermes-lead"`: `minir_explorer`,
      `minir_researcher`, `minir_worker`.
- [ ] `references/jev.md` classifies every message this skill routes, and every W1 to W8 ID it names
      resolves in this skill.
- [ ] `skill_view(name="minir:hermes-lead", file_path="references/jev.md")` previews the rule after
      the plugin skill is registered.
- [ ] W7 names the worktree base and one git worktree per worker.
- [ ] The two role rule files exist under `references/` and are readable by a plugin agent.
- [ ] `references/model_route.yaml` names `provider` and `model` for the three roles, and every
      dispatch builds `model="<provider>:<model>"` from it.
- [ ] Every load point in `SKILL.md` names its trigger, and task judgment loads neither craft rule.
- [ ] Every workflow names its trigger, sequence, gate, and stop.
- [ ] Test scope names the behavior check, the non-code evidence, the full-suite and e2e limits, and
      the expected-output rule.
- [ ] The role table matches the prompt contracts.
- [ ] The lead-only list covers scope, acceptance, verification, commit, and the user report.
