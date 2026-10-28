# JEV task judgment rule

Scope: task-level routing. This rule reads the user message and derives the agent set, the
workflow, the self-check, and the repair loop. Nothing runs at this stage. The fork-level dispatcher
in `doc/jev-fork-dispatch-draft.md` is a different layer; do not merge the two.

The lead applies this rule itself, first, on every new user message: the classification is a lead
step, never a dispatched agent, and no shortcut replaces the rule. Load it with
`skill_view(name="minir:hermes-lead", file_path="references/jev.md")` before routing.

Used by: the lead, loaded with `skill_view`. No agent runs this rule.

Authority order:

1. This rule for classification and routing.
2. `SKILL.md` for the role contracts and the workflow definitions (W1 to W8).
3. The two role rule files for output and engineering constraints, loaded only at the load points in
   `SKILL.md`.

If this rule and `SKILL.md` disagree about a workflow's steps, `SKILL.md` wins. If this rule is
silent, record `mixed_or_unclear` with the missing fact (`needs_lead`); do not invent a class, an
agent, or a workflow.

## Inputs

- The user message, verbatim.
- The lead's constraints for this task: acceptance check, non-goals, authorized slice.
- The project root, if supplied.

The judgment reads no project files. It classifies what the message asks for; the explorer gathers
the facts.

## Decision table

| Class | Signals | Agents | Workflow | Self-check | Repair loop | Stop |
|---|---|---|---|---|---|---|
| `internal_question` | where, how, or why in this repo; no change verb | `minir_explorer` | W1 | explorer evidence table; lead opens the citations | none; one re-probe if a citation fails | 3 searches |
| `external_question` | library, version, spec, cost, prior art | `minir_researcher` | W2 | researcher sources; lead checks URL and date | none; one re-fetch if a source is missing | 8 pages |
| `scoped_change` | change verb plus a target behavior or file | `minir_explorer` -> `minir_worker`; add `minir_researcher` if an external fact blocks | W3 | worker narrow check; lead reruns the acceptance check | yes: worker fixes, max 2 methods; explorer again if the failure disproves an assumption | blocker or 2 failed methods |
| `failure_diagnosis` | failing test, crash, regression, wrong output | `minir_explorer` -> `minir_worker`; add `minir_researcher` if the cause is external | W4 | lead reruns the original failing command | yes: worker fixes, max 2 methods; explorer again if the assumption is wrong | blocker or 2 failed methods |
| `design_verdict` | "is this sound", "can we", "which approach", "review this design" | `minir_explorer` + `minir_researcher`, parallel | W5 | lead writes 可行 / 不成立 / 需变通 per claim | none; unresolved claims are labeled | no worker |
| `curative_cleanup` | simplify, remove dead code, remove overengineering | `minir_explorer` -> `minir_worker` | W6 | green baseline before and after; lead reruns it | yes: worker's two-method limit; revert a file that breaks | 2 failed fixes or a red baseline |
| `parallel_change` | two or more independent targets with disjoint files | `minir_explorer` -> N `minir_worker`, one git worktree and branch per worker | W7 | each worker's check in its worktree; lead runs the integration check after merge | yes, inside each worktree; rebase on conflict or serialize; serialize if the file sets overlap | overlap, conflict, or integration failure |
| `draft_or_design_doc` | "draft", "design doc", "plan" | none, or `minir_explorer` / `minir_researcher` for evidence; no worker | W8 | lead checks citations and open decisions | none | document path under `<project>/docs/<type>/` or Documents |
| `mixed_or_unclear` | two classes fit, or the change target or acceptance is missing | none; lead decides | none | lead | none | one focused question |
| `blocked_or_out_of_scope` | secret, credential, destructive action without authorization | none | none | lead | none | report the blocker |

The agent names are the `agent_name` values. The workflow IDs are the sections in `SKILL.md`.

## Tie-breakers

1. Question plus change: split the message. Run the question class first, then the change class.
   Record both in `subtasks`; do not merge them into one class.
2. Change without an acceptance check: `needs_lead` with `"acceptance check missing"`. Do not send a
   worker to invent one.
3. Change without a target file or behavior: `scoped_change` with the explorer first; the
   explorer's Unknowns decide whether the lead re-scopes.
4. Design review that also says "fix": `design_verdict` first; no worker until the lead accepts a
   change slice.
5. External fact decides feasibility: researcher before worker. Do not let the worker discover it
   by failing.
6. Cleanup versus change: if the message only removes or simplifies, W6; if it adds behavior, W3.
7. Parallel only when the file sets are disjoint and each worker has its own git worktree under
   `$HERMES_HOME/plugins/minir/worktree/`. If they overlap, W3.
8. "Draft" or "plan" is W8, never a worker.
9. A secret, credential, or destructive action without explicit authorization is
   `blocked_or_out_of_scope`.
10. If two classes fit equally, choose the one with the smaller agent set; the lead can escalate.
11. If the message points to a plan or design document, set `acceptance_source: document`; if it
    states the requirement, `user`; if neither, `missing` and `needs_lead: acceptance source missing`.

## Parallel worktrees

`parallel_change` is the only class that creates worktrees. The base directory is
`$HERMES_HOME/plugins/minir/worktree/`.

1. The lead creates the base once: `mkdir -p "$HERMES_HOME/plugins/minir/worktree"`.
2. Start from a commit. If the primary checkout is dirty, commit or stash with the user's
   authorization; uncommitted changes are not in a worktree.
3. For each worker:
   `git -C <primary> worktree add "$HERMES_HOME/plugins/minir/worktree/<task>-<n>" -b "minir/<task>-<n>" <base-ref>`.
   One worktree per worker, one branch per worktree.
4. Dispatch the worker with `project_dir=<worktree>` and a slice limited to that worktree. The
   worker returns the branch, commit, diff stat, and verification.
5. Merge one branch at a time, run the integration check in the primary checkout, and send a
   conflict back to that worker's worktree (rebase onto the new base) or serialize.
6. After a merge, remove the worktree and delete the branch. On failure, keep the worktree for
   inspection and report its path.

A `minir_agents` worker is scratch-scoped: it can read a worktree but cannot write it through the
file tools. Run worktree workers on a runtime that can write the worktree (Kanban or delegation),
or have the lead apply the worker's diff. Never let the worker use `terminal` to bypass the guard.

## Document destination

`draft_or_design_doc` carries `document_type` (`draft`, `design`, or `plan`). The lead writes the
document at:

- `<project>/docs/<type>/<name>.md` when the project root is a normal project directory.
- The user's Documents folder when the project root is the user's home directory or no project root
  is known: `%USERPROFILE%/Documents/<type>/<name>.md` on Windows, `~/Documents/<type>/<name>.md`
  elsewhere.

Create the type subdirectory. Never write directly under `docs/` or Documents. If the user says
"draft design doc", record `draft`; the requested artifact is the draft.

## Acceptance and tracking

The lead derives the acceptance rubric from the user's requirement or the document plan before
dispatch. The judgment is not the acceptance. Each role is accepted against the rubric, not its
self-report.

The lead registers one Kanban goal card per task and tracks its steps in `minir_todo`. The card
carries the goal and the rubric; the todo list carries the steps. A direct answer with no dispatched
role may skip the card only if the todo records why.

## Self-check and repair

- `self_check` is the check the executing role runs before returning. Every workflow has one.
- `self_check` names the narrowest check that exercises the behavior after the change: the test the
  change touches, or size, line count, or hash for a non-code file. Never the full suite on a local
  change, and never e2e or smoke unless the user asked.
- `repair_loop` is who fixes a failed check and the bound. No loop is unbounded.
- The lead's acceptance rerun is a gate, not a repair loop. A failure goes back to the worker with
  the exact command and output, or to the explorer if the assumption is wrong.
- A role that hits its stop condition returns the blocker. The judgment does not open a new loop.

## Output

```json
{
  "task_class": "scoped_change",
  "confidence": "high",
  "agents": ["minir_explorer", "minir_worker"],
  "workflow": "W3",
  "document_type": null,
  "acceptance_source": "user",
  "self_check": "worker narrow check; lead reruns acceptance",
  "repair_loop": "worker fixes, max 2 methods; explorer on a wrong assumption",
  "gates": ["evidence", "acceptance"],
  "stop": "blocker or 2 failed methods",
  "subtasks": [],
  "needs_lead": null,
  "reason": "adds a behavior and names the target"
}
```

The lead writes this object for itself before routing; no agent returns it.

- `task_class` is one of the table rows.
- `agents` is an ordered list; the first agent runs first. It may be empty only when the lead
  executes the task (W8, `mixed_or_unclear`, `blocked_or_out_of_scope`).
- For `parallel_change`, `agents` lists one worker per worktree; the lead assigns each path under
  `$HERMES_HOME/plugins/minir/worktree/`.
- `workflow` is `null` only for `mixed_or_unclear` and `blocked_or_out_of_scope`.
- `document_type` is `draft`, `design`, or `plan` only for `draft_or_design_doc`; otherwise `null`.
- `acceptance_source` is `user`, `document`, or `missing`; the lead uses it to build the rubric.
- `subtasks` is used only for a question-plus-change split; each entry has `class`, `agents`, and
  `workflow`.
- `needs_lead` is a string when the lead must decide, otherwise `null`.
- `reason` is one line.

## Boundaries

- The judgment executes nothing: no role dispatch, no file edit, no tool beyond reading this rule
  and the skill.
- The judgment adds no workflow ID. W1 to W8 are the only IDs.
- The judgment does not classify by a single keyword. Read the intent and the target.
- The judgment does not merge classes. If the message is mixed, split it.
- The judgment is the lead's own; later evidence may revise it with a one-line reason.
