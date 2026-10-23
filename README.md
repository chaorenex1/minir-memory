# minir-memory

A minimal, dependency-free **Hermes memory-provider plugin template** — a working
reference implementation of the `MemoryProvider` contract that you can fork, rename,
and grow into a real provider.

It keeps explicit value markers and every derived conclusion in a per-profile SQLite database,
recalls them through an FTS5 trigram index (CJK-safe, with a token-scan fallback), and exposes
seven tools (`minir_search`, `minir_kb_search`, `minir_remember`, `minir_forget`, `minir_todo`,
`minir_stats`, `minir_agents`) and one local counter (`minir_content_valid`). On top of that it runs
two **independent** reasoning layers: the base layer builds the user/AI cards every 24 turns, the
dialectic reviews them every 8 turns against its own evidence window, and corrects the newest base
artifact (a pre-overwrite snapshot and an FTS index are kept). The review publishes a short
`## Base Revision` note naming the fields it changed and emits no advice.

Three file-backed layers sit on top: a **three-day global card review** merges sessions into four
durable files; a **knowledge pipeline** writes one page per marker unit and injects path hints;
and a **transcript event store** records host turns and layer runs as JSONL (append-only,
permanent, one event per line).

A fourth pass closes the loop after a conversation goes quiet: `on_session_end` records every
turn's final token usage, captures the session's value markers and arms a 24-hour timer. Typing in
the session again cancels it; otherwise a **session-summary agent** (the calibrated window summary is its first
user prompt input) and a **knowledge-extraction agent** turn the session into a durable summary
and knowledge pages. The seven tools are also registered as the `minir` toolset, so every agent
this plugin runs can use the store and the knowledge search directly; the same toolset carries the
layer-scoped transcript search.

It also ships an optional **main adviser**: a fast, per-turn side channel that watches the live
tool loop (user goal, assistant intent, tool results) and injects its advice before the next
model request through the host's `llm_request` middleware. It is the only advice producer in the
provider: the dialectic corrects memory, the adviser advises the turn, and there is one
review → emission guard → ledger → delivery chain. There is no model-visible consult tool: the
synchronous consult route is internal to the worker (`worker._adviser_consult`), so the main agent
cannot call the adviser as a tool. Unlike the dialectic layer the adviser never writes
base cards or revisions. It is controlled by configuration only
(`memory.minir.adviser_mode.enabled`; there is no `/adviser` command).

The adviser has two delivery tiers. Without host changes it uses `llm_request`, the
turn-boundary aside, `pre_tool_call` and `pre_verify`. With the **host patch series** in
`plugin/minir/host-patches/` installed it also steers the live turn (a queued
`agent.steer(kind="adviser")` through `hermes_cli.plugin_runtime`), **interrupts the current
tool batch for blocker advice** (`agent.interrupt`; tools that poll stop, tools that never
poll are abandoned), can keep a finished turn open for one more answer (the plugin stop gate, budget
`agent.max_plugin_continuations`), and can hand the surface one fresh turn after a terminal
blocker (the existing `pending_steer` contract; bounded per session). The Desktop transcript
draws its durable rows as an **adviser card** (severity, label, Markdown body) instead of a
user bubble, **including a live `message.adviser` event while the turn runs**.
`scripts/install.py` applies the patch series on install, verifies it, records it, and
`--revert` takes it back off. On an unpatched host every bridge call is a no-op and the ledger
channels remain in charge; the Desktop patch degrades to showing the self-describing text row.

A plan-mode publisher can call `plugin_runtime.set_flag(session, "plan_mode", True)`; the adviser
also mirrors the `plan_mode` tool's `on/off/submit/status` results when that plugin is installed.
In plan mode it advises without steering, interrupting, or triggering turns. With no publisher
and no tool, the flag is absent and behaviour is unchanged.

The reviewer runs **read-only**: the `file` toolset supplies `read_file`/`search_files`, and
`agent_loop` strips `write_file`/`patch` plus the memory-write/spawn tools before the run. Its
conversation is persisted by
`TranscriptBuffer` as `<HERMES_HOME>/minir-memory/sessions/adviser/<session>--<adviser>.json`
(one file per adviser session, appended across reviews); no conversation row goes into
`state.db`. The last three advice batches are fed back into the next prompt for continuity.
A project `WATCHDOG.yml` can declare `advisors[]` (name, model, tools, instructions, cadence,
review_mode); config `roster` entries override it field by field.

**SQLite holds no transcripts.** The worker reads `(from_ts, to_ts]` messages from
`$HERMES_HOME/state.db` read-only (plus the in-process buffer for turns not yet committed) and
persists only what the layers *derived* from them plus the conclusions you asked it to keep.
What the provider
does keep is a separate **transcript event store** of JSONL files under
`$HERMES_HOME/minir-memory/sessions/` (host turns and layer runs, append-only and permanent, with
no recall from it). Derived content may quote or reference the messages it reasoned over — it is a
derived artifact, not a redacted one — but the recall store never copies the conversation itself. Because
the host store is the source of truth, a job queued before a restart can still be completed; only
when no `state.db` exists are inherited jobs expired.

The same directory also ships a **context engine** for audited summary compression: at 95% of the
configured threshold the provider pauses periodic scheduling, refreshes the base and dialectic
layers synchronously, and the engine replaces the unprotected middle of the window with one
assistant `session_summary`. That summary is one B3 calibration of the newest base window summary:
B3 gets the previous summary plus the turns since that window's end, so compression and the base
refresh share one shape contract. The summary is stored and FTS-indexed; the head and tail stay
verbatim.

No external binaries, stdlib only. Layer calls go through the host's auxiliary client, so there is
no key to configure; set `reasoning_enabled: false` to keep the provider a pure note store.

```
pyproject.toml         package metadata, pytest config, pip entry point
plugin/minir/          the deployable plugin directory (copy this to plugins/minir)
  plugin.yaml          manifest (name/version/description + metadata)
  __init__.py          register(ctx) + MinirMemoryProvider + the engine export
  store.py             SQLite storage: conclusions + FTS5, dialectic refreshes, jobs, metrics
  history.py           read-only host history, profile resolution, identity files
  transcript.py        bounded turn window + the permanent JSONL event store (sessions/)
  transcript_process.py per-process locks, first/last-process verdicts, cache replay
  transcript_prompt.py per-pass transcript digest renderer (plain text, coverage line)
  turn_counter.py      base / dialectic layer turn counters + job windows
  worker.py            drains base and review jobs FIFO per session, publishes artifacts
  card_review.py       three-day review: four card files, validation, revisions, anchor policy
  knowledge.py         one page per unit, path-hint retrieval block
  rgtool.py            managed ripgrep: facts.json discovery + the selection's search call
  tools.py             the minir toolset: store tools + agent transcript search; file/skills grant
  agent_history.py     minir_aiagent_search: the caller's own layer stream, layer from contextvar
  minir_agents.py      run one named custom agent and flush its transcript under sessions/
  session_summary.py   end-of-session summary agent (its own template; the compression record is input)
  extraction.py        idle-timer due-row runner + knowledge-page extraction agent
  file_guard.py        pre-tool-call scope for the plugin agents' file tools
  scheduler.py         one clock for turn and time tasks, with a per-task ledger and logs
  layers.py            output contracts, field validation, the four-section renderer
  agents/              one markdown core per model pass, filled at call time (B1-B4, dialectic,
                       four card-review passes, knowledge_render, session_summary)
  rules.py             rule-file loader: validation, override, cache, prompt assembly
  rules/               one file per contract (contract + review checks), read at call time
  window_summary.py    the summary section: template asset, prompt caps, type check
  templates/           the Window Summary and Session Summary shapes the model fills
  skills/              bundled read-only skills, registered as minir:<name> (data-layout)
  compress_pass.py     compression's one B3 calibration of the newest window summary
  content_valid.py     minir_content_valid: the local character/token counter
  cadence.py           store-path helper shared by the provider and the engine
  context_engine.py    audited summary compression (context.engine: minir)
  agent_loop.py        one-shot in-process AIAgent runs — sync + async + the bounded run (Feishu-shaped)
  adviser.py           auxiliary routes: task registration (adviser + three layer routes) + host session search
  adviser_prompts.py   adviser prompt loader: prompts/adviser sections + rule bodies
  adviser_mode.py      the only advice producer: event ring, review, guard, delivery channels
  host_bridge.py       optional bridge to hermes_cli.plugin_runtime (steer / continuation)
  host-patches/        git patch series for the host control surface + manifest.json
  session_search.py    read-only multi-shape search over the host's session store (state.db)
  prompts/adviser/     the adviser prompt sections read at run time
tests/                 pytest suite: contract, store, transcript, provider, counting, worker, engine
scripts/install.py               copy the plugin + apply/verify the host patch series
scripts/install_host_patches.py  apply/check/revert the series on a hermes-agent checkout
scripts/verify_against_host.py   end-to-end check against the real Hermes install
```

## How a memory plugin works

Hermes loads memory providers by **name**, one at a time:

1. Discovery scans `plugins/memory/<name>/` (bundled), `$HERMES_HOME/plugins/<name>/`
   (user-installed) and `./.hermes/plugins/<name>/` (project-local, opt-in via
   `HERMES_ENABLE_PROJECT_PLUGINS`) for a directory whose `__init__.py` mentions
   `register_memory_provider` / `MemoryProvider`. Nothing is imported during the scan.
2. `register(ctx)` runs and calls `ctx.register_memory_provider(instance)`.
3. The `MemoryManager` then drives the lifecycle, per turn and per session:

| Method | When the host calls it | Notes |
| --- | --- | --- |
| `name` (property) | always | must equal the plugin directory name and `memory.provider` |
| `is_available()` | before activation | config/deps checks only — no network |
| `initialize(session_id, **kwargs)` | once at agent startup | `kwargs` include `hermes_home` (profile-scoped), `platform`, `agent_context`, `agent_identity`, `user_id` |
| `system_prompt_block()` | prompt assembly | **static** text only; dynamic layer state goes through `prefetch()` |
| `prefetch(query, session_id=)` | before each LLM request, **critical path** | injects the live AI self + window summary and the todo list; one indexed read, no model call |
| `recall_status()` | after `prefetch()` | drives the provider indicator; counts injected bullets and todo items from the last prefetch |
| `sync_turn(user, assistant, **kw)` | after each turn, background worker | hands the turn to the layer worker (in memory); `messages`, `turn_author` are optional kwargs |
| `get_tool_schemas()` / `handle_tool_call()` | tool dispatch | handlers **must return a JSON string** |
| `shutdown()` | teardown | idempotent |
| `on_memory_write(...)` | built-in memory tool writes | mirror `add`/`replace`/`remove` if you want both stores searchable |
| `on_session_switch(...)` | `/resume`, `/branch`, `/reset`, `/new`, compression | rebind per-session state; `reset=True` means a new conversation, a non-empty `parent_session_id` marks a branch |
| `get_config_schema()` | `hermes memory setup` | env-var-only fields keep it simple (see below) |
| `on_turn_start(turn_number, message, **kw)` | before each user turn | **the counting tick** — the host's own per-session turn number; never fires for a subagent/cron turn |
| `on_session_end(messages)` | real session boundary | freeze the session + drain the base layer (counted tail *and* any content no job covered); the dialectic review follows the base job |
| `on_pre_compress(messages)` | before compression | pause periodic scheduling, synchronously refresh base → dialectic, reset counters; must return a string (this one returns `""`) |
| `on_delegation`, `backup_paths` | optional | not implemented here — read the ABC before opting in |

The ABC lives in the Hermes install at `agent/memory_provider.py`; treat that file
as the source of truth, not this table.

### Plugin hooks this template registers

`register(ctx)` also subscribes to the *plugin* hook surface (`ctx.register_hook`), which the
memory-provider loader forwards to a real `PluginContext` — so one plugin directory can expose
both surfaces:

| Hook | Used for |
| --- | --- |
| `on_session_start(session_id=, model=, platform=)` | record the session; queue one cold-start *base* job when history exists, gated to the bound primary session |
| `on_session_reset(session_id=, old_session_id=, reason=)` | `/new`, `/reset`: stop the old counters, initialize the new session |
| `on_session_finalize(session_id=)` | process teardown: stop counting, expire adviser-mode advice, flush tool metrics |
| `pre_llm_call` | record the turn's user goal; inject the `adviser_only` brief and below-floor advice (one-shot aside). Advice has a single producer (the live reviewer); nothing is staged here |
| `post_llm_call` / `post_tool_call` | per-turn logging; `post_tool_call` also aggregates tool counters and feeds the adviser-mode event ring |
| `pre_tool_call` | `adviser_only` enforcement, the `file_guard` scope for peer-agent file tools, plus the opt-in blocker gate: refuse mutating tools before execution and hand the model the reason |
| `on_interim_message` | record the assistant's mid-loop intent for the adviser-mode reviewer |
| `post_api_request` / `api_request_error` | confirm adviser-mode delivery or return staged advice to pending |
| `pre_verify` | opt-in (`verify_continue`): a live blocker keeps one code-editing turn going |

Every hook `register()` subscribes to must also be listed under `provides_hooks:` in
`plugin.yaml` — `hermes plugins validate` fails the manifest otherwise (`hermes plugins doctor`
warns). `tests/test_contract.py` keeps the two lists in step.

These carry **no counting state**: the hot ones (`pre_tool_call`, `pre_llm_call`,
`post_llm_call`, `post_tool_call`, `on_interim_message`, `post_api_request`,
`on_session_start`) run under the host's 30s `plugins.hook_callback_timeout`,
which *abandons* a slow callback and suppresses it for 60s. They therefore never call a model:
`on_session_start` only enqueues, `pre_llm_call` reads the advice ledger, and `post_tool_call`
increments an in-memory counter and appends one bounded event. The plugin-level `on_session_end` is a per-turn event (not a
session boundary, so the real boundary uses the memory-provider method. Observer callbacks return
`None`; `pre_llm_call` may return injected context and `pre_tool_call` may return a block in
`adviser_only`. `on_session_finalize` must stay empty because the host sends a non-empty result to
the chat. `register()` closes the hooks over the provider instance and falls back to the
process-wide active provider, since the host can construct the plugin twice.

## Tools

The provider exposes eight tools; every response is a JSON string (errors are `{"error": ...}`).
The same schemas are registered as the `minir` **registry toolset**, so the plugin's own agents can
be granted them explicitly (see "The `minir` toolset" below).

| Tool | Parameters | Behaviour |
| --- | --- | --- |
| `minir_search` | `query` (required), `limit`, `scope`, `type`, `session_id` | Ranked FTS5 recall. Default `scope="session"`: the current session plus durable cards and markers. `scope="all"` searches every session; `session_id` targets one explicitly. `limit` defaults to `prefetch_limit` and is capped at 20; `type` keeps only that conclusion kind. Each result carries `conclusions_id`, `kind`, `key`, `text` (600-char excerpt), `when`, `author`, `subject`, `session_id`, `score` and `rank`. |
| `minir_kb_search` | `query` (required), `org`, `category`, `limit` | Read-only search of the knowledge directory the hint block points at. `query` is a ripgrep (Rust regex) pattern matched case-insensitively; `org`/`category` filter front-matter; `limit` defaults to 8, capped at 25. A result carries `path` (absolute), `relative`, `org`, `category`, `topic`, `summary`, `revisions`, `updated_at` and up to two matched body lines (`line` + `text`); `total` counts every matching page, so `total > count` means "narrow the pattern". Hits outside `<org>/<page>.md` under the knowledge root are never reported. |
| `minir_remember` | `type` (required), `content` (required) | Stores one conclusion of that kind (`note`, `entities`, `trajectories`, `experiences`, `tools`, `projects`, `todo`) in the single memory table, bound to the current session and indexed for FTS. Returns `{stored, conclusions_id, type, session_id}`. |
| `minir_forget` | `type` (required), `conclusions_id` (required) | Deletes that row only when its stored kind matches. Returns `{deleted, conclusions_id, type}`; a wrong kind or unknown id yields `deleted: false`. |
| `minir_todo` | `todos` (required) | Replaces this session's todo list, at most two levels deep (`parent` names a top-level id) and 256 items. It is stored as one row (`kind="todo"`), rendered as a markdown table into the final answer, and carried by every prefetch block for the session. An empty list deletes the row. |
| `minir_stats` | none | Store counts (with `conclusions_by_kind` for this session), layer counters, the scheduler ledger, published artifacts, buffered messages, in-memory tool metrics, the session's token usage and its newest turn rows, and the database path. |
| `minir_agents` | `agent_name` (required), `user_prompts` (required), `skill_name`, `model`, `system_prompts`, `context_files`, `project_dir`, `tool_sets`, `skip_memory`, `skip_context_files`, `skip_background_review`, `load_soul_identity` | Runs a named in-process agent; omitting `tool_sets` grants every host tool. `model` is `provider:model`. The system prompt is the first prompt file found under `HERMES_HOME` (see "Custom agents"); `system_prompts` is the last fallback, and `system_prompt_source` reports which one won. The full run is flushed to `sessions/<agent_name>/<layer_session_id>.jsonl`. |
| `minir_content_valid` | `content` (required), `limit` (required), `mode` | Local deterministic length check. `mode="string"` (default) counts Unicode code points; `mode="token"` applies `rules.estimate_tokens` (CJK one token per character, otherwise four characters per token, plus a 10% margin). Returns `{ok, mode, unit, measured, limit, over, remaining}` and, in token mode, `estimator`. It never truncates, rewrites or stores, and it answers before the provider is initialized. |

Explicit markers are separate from the base layer's automatic value markers: the worker keeps
merging its own five-key inventory per refresh, while a remembered marker is a session-bound,
kind-tagged conclusions row that search can recall and `minir_forget` can remove. Writes mirrored
from the host's built-in memory tools (`on_memory_write`) land under `kind="note"`; they are
recallable in both public scopes.

Two further tools exist only in the agent-facing `minir` registry toolset (not in the provider's
main-agent surface):

| Tool | Scope | Behaviour |
| --- | --- | --- |
| `minir_hermes_session_search` | host caller: its own platform's sources; agent caller: the current host session only | Read-only FTS5/browse/transcript/window/stats over `state.db`. `platform="*"` disables the default source filter; the envelope echoes the filter it applied. |
| `minir_aiagent_search` | the calling layer only | Reads the caller's own JSONL stream (`messages`/`search`/`around`/`stats`) through the transcript buffer. No layer argument: the layer is bound per agent run, and an unbound call is refused. Every answer echoes `coverage`, `source` and `dropped_before`. |

## The `minir` toolset

The eight tools have two delivery paths. The memory-provider surface (`get_tool_schemas`)
injects them into a main agent that has the host's `memory` toolset enabled. For the plugin's own
agents — the adviser, the layer worker, the session-summary and knowledge-extraction agents — the
same schemas are registered through the plugin context under a registry toolset
named `minir`, together with `minir_hermes_session_search` and `minir_aiagent_search`. That
toolset also carries the host's `file` toolset and `skills`, so a plugin agent grants all three
with:

```python
toolsets = ["file", "minir", "skills"]
```

Every in-process agent this plugin runs builds its list with `tools.agent_toolsets(...)`, which
appends the three to whatever the caller configured, `file` included and not left to the caller.
The registered handlers call the live provider's `handle_tool_call`, so the registry path and
the provider path cannot drift.

## Custom agents: `minir_agents`

`minir_agents` runs one named in-process agent on the gateway's model route. `agent_name` and
`user_prompts` are required; `context_files` are read under the same `file_guard` scope as the
agent's own file tools, with relative paths resolved against `project_dir` (at most 20 files and
200 KB in total).

- `model` pins the route as `provider:model` (split at the first colon, so
  `openrouter:anthropic/claude-sonnet-4` keeps its namespaced model id). A bare model name pins
  only the model and keeps the gateway's provider; omitted, the gateway route is used.
- The system prompt is a lookup, not just an argument. The first readable, non-empty prompt file
  wins, each directory accepting `<agent_name>.md` or a bare `<agent_name>` file; `skill_name`
  gates the two skill-scoped directories:

  | # | Path |
  | --- | --- |
  | 1 | `$HERMES_HOME/.agents/<agent_name>` |
  | 2 | `$HERMES_HOME/agents/<agent_name>` |
  | 3 | `$HERMES_HOME/skills/<skill_name>/agents/<agent_name>` |
  | 4 | `$HERMES_HOME/plugins/minir/agents/<agent_name>` |
  | 5 | `$HERMES_HOME/plugins/minir/skills/<skill_name>/agents/<agent_name>` |
  | 6 | the `system_prompts` argument |

  The name is the identity: the same agent name never loads twice, and a file found at an earlier
  step is not merged with a later one. A leading YAML front-matter block (the shape
  `hermes import-agent` writes: `name`, `description`, `tools`, `model`) is metadata and is
  stripped; its `tools` and `model` do not override `tool_sets` or `model`. The payload's
  `system_prompt_source` is the winning path, `"argument"`, or `""` when neither supplied one.
- `tool_sets` omitted, empty or `["*"]` grants every host tool; an explicit list is passed to the
  agent unchanged.
- `skip_memory=False`, `skip_context_files=False`, `skip_background_review=True` and
  `load_soul_identity=True` are the custom-agent defaults. The plugin's own layer, review,
  extraction and adviser agents use the opposite posture: no host memory, no context files, no
  soul identity.
- The run's `AgentRun.messages` are recorded with `TranscriptBuffer.record_aux` and flushed
  before the tool returns, at
  `<HERMES_HOME>/minir-memory/sessions/<agent_name>/<layer_session_id>.jsonl`. The path is
  returned as `transcript_path`; `transcript_written` says whether the flush reached disk.
- `minir_agents` may recurse when the custom agent is granted the `minir` toolset; the tool caps
  the depth at three.

## Session end: idle timer, summary and knowledge extraction

`on_session_end` is the real boundary (the plugin-level hook of the same name is a per-turn
event). Four things happen there beyond the layer drain:

1. **Final turn usage.** Every `post_api_request` for the bound session folds its normalized usage
   into a `(session_id, turn_id)` row in `turn_usage`; the boundary closes those rows and stores
   the session total in `session_usage` beside the host's own `sessions.input_tokens` /
   `output_tokens` aggregate. `minir_stats` reports both, so a turn's final consumption is
   auditable even after the process is gone.
2. **A marker snapshot.** The session's explicit typed markers plus its newest base-layer
   value markers are captured into `session_extractions` with `due_at = now + 24h`. A
   session with no markers arms nothing; the timer is for durable sessions.
3. **A cancellable timer.** Any user turn in that session (`on_turn_start`, `sync_turn`) deletes
   the pending row. Opening the session is not input; typing in it is.
4. **The due run.** The unified scheduler's `session_extraction` time task
   (`ExtractionScheduler.tick`) claims due rows atomically, so a timer armed in one process is
   honoured after a restart. For each due session it runs the
   **session summary agent** — the session's newest compression record (`session_summary:<n>`) is
   the first evidence block of its user message, and it writes its own template
   (`templates/session_summary.md`: `### Request`, `### Outcome`, `### Decisions`,
   `### Constraints`, `### Verified Facts`, `### Lessons`, `### Open Threads`, `### References`) —
   then the **knowledge extraction agent**, which reads each marker's anchor, writes the page body
   from the session slice, and saves it through `knowledge.write_unit`.

`session_extraction_enabled` (default `true`) and `knowledge_idle_hours` (default `24`; `0` fires
on the next tick) control it. Both need `reasoning_enabled: true`; with reasoning off the provider
stays a pure store. A failed run retries once after an hour, then is parked as `failed` in
`session_extractions` rather than retried forever.

## Scheduling: one clock for turn and time work

`scheduler.py` owns every recurring decision. A task is registered as one of two kinds:

- **turn**: `layers` ticks the base/dialectic counters on `on_turn_start`; the host's turn
  number is the clock, and the task returns how many jobs it queued.
- **time**: `card_review` (polled every 10 minutes; due every `card_review_days`) and
  `session_extraction` (polled every 60 seconds; due when a session's idle timer expires) run on
  one daemon thread, and each polls its own durable state before running.

`Scheduler.snapshot()` records runs, failures, skips, the last outcome/detail and the duration
of every task; `minir_stats` exposes it under `scheduler`. Every execution also emits one
structured line (`Minir scheduler: task=… trigger=… outcome=… detail=… ms=…`), and startup logs
the inventory (`turn=[layers] time=[card_review@600s, session_extraction@60s]`). A task that
raises is contained and counted, so the clock keeps running. `shutdown()` stops the one clock
before the store closes: no scheduled job can touch a closing database.

## The two layers (base / dialectic)

`turn_counter.py` keeps two **independent** counters per session. Each turns its own cadence into
*jobs* ("reason over session X's history in `(from_ts, to_ts]`"): the base layer at 24 turns, the
review at 8. Neither waits for the other; when both are due on the same turn the base job is
recorded first, and the store claims strictly FIFO **within a session**, so the artifact is
published before the review that reads it. A job is one row in the `jobs` table; `worker.py`
drains it from the host's `state.db` (plus the uncommitted buffer), calls the auxiliary model, and
publishes into `layer_state`/`conclusions`. Each base refresh also appends one structured row to
`base_refreshes` (session, time window, actual turn range, the six outputs, `is_latest`); one base
window has one job.
`prefetch()` injects the live base row's `ai_self_representation` (≤400 chars) and
`window_summary` (≤2000 chars; Objective and Current State are kept whole and only the other six
sections are truncated), plus the session's todo list. It injects neither knowledge page paths nor
explicit recall: `minir_kb_search` and `minir_search` are the retrieval surfaces. Another session's
rows are reachable only through `minir_search(scope="all")`, never by automatic injection. Advice has one producer
(the live reviewer); the dialectic layer writes no advice at all.

| Layer | Models | Artifact sections | Subject key |
| --- | --- | --- | --- |
| base | this profile's user: B1 writes the user card, starts B3 (summary) and B4 (markers) through `minir_agents`, then B2 writes the AI card on `auxiliary.minir_dialectic` | `## User Card`, `## AI Card`, `## Window Summary`, `## Value Markers` | `user:<id>` |
| dialectic | the live base artifact + its own evidence window; reviews drift and replaces whole parts | `## Base Revision` only (cards stay on the base layer, no advice) | `agent:<profile>:<identity>` |

The review's **evidence** window starts at its own watermark and ends at the live edge; the base
artifact it may correct is the newest published row for the session. A review that finishes after
a newer base published is discarded as `stale_base` (whole review, never half a correction): the
newest row's id and `window_to_ts` are the version fence.

The review is **not** queued by the base job. It fires on its own tick; if no base artifact has
ever been published it degrades to a no-op (`no_base`) instead of becoming a second base writer.
It re-reads its window's raw turns even when a previous review advanced the dialectic watermark.
It returns one review object:
`user_card`, `ai_card`, `window_summary`, `base_changed` (`{changed, fields}`), `changes` and an
empty `prunings`. A listed part is replaced in full, so a review cannot half-update a card. The
pass count is adaptive: short windows get one synthesis, longer ones add a self-audit and a
reconcile, capped by `dialectic_depth`. A failed or malformed pass falls back to the last valid
one. `layer_state.version` counts refreshes; `prefetch` reads the live base row's two columns, and
the newest review note stays searchable.

A base refresh is **one orchestration pass (B1), two child agents (B3, B4), then one AI-card pass
(B2)** on the `auxiliary.minir_dialectic` route. Each system prompt is
built at call time from the pass's core asset under `plugin/minir/agents/`, the rule texts that
pass reads and the output contract; the
rule files in `plugin/minir/rules/*.md` are passed as text (`rules.rule_text`), untransformed. An
operator override under `$HERMES_HOME/minir-memory/rules/` replaces a packaged file and falls back
with a warning when it is unreadable. The worker writes the window digest and the previous markers
to scratch files and gives B1 their paths; B1 calls `minir_agents` for B3 (`tool_sets` file +
session_search) and B4 (file + minir + session_search), copying B3's markdown verbatim and B4's
JSON array. A failed child leaves its key empty; the worker retries that one child once and fails
the refresh if the summary still cannot be produced. B2 runs last, so the six `ai_self` fields see
this window's ledger. Each pass returns **one JSON object** whose card values are markdown bodies
(no code fence, no front-matter); the worker validates it against the same rules the reviewer
reads, and only a validated object is rendered into the artifact. The `minir` toolset is attached
on this route, and the tool hint carries the window bounds: a pass may call
`minir_hermes_session_search` with `after`/`before` for evidence inside `(from_ts, to_ts]`, but
must not read later turns. The first user message is the only allowed outside-window evidence, and
only on the first refresh.

1. **B1 user card** — the previous user card + this window (+ the first user message as intent
   anchor on bootstrap) → `{"user_self_representation", "user_preferences", "window_summary",
   "value_markers"}`, where the last two are the children's answers copied verbatim;
2. **B3 window summary** — the window digest + the previous summary of *this session* → the
   eight-section markdown. `objective` is the complete goal ledger (one goal per line, no ids, no
   status); `Current State` groups those goals under Done/Doing/Todo. The prompt is the only
   enforcement: `window_summary.validate` checks that the value is a string;
3. **B4 value markers** — the digest + the previous markers + the marker rules →
   `{"value_markers": [...]}`. Each marker is `{id, info_type, topic, why, relations, render_plan,
   anchor, objects}`: an index and a render plan, never a page body. `anchor` is required; the
   `knowledge_render` pass reads that slice and writes the page;
4. **B2 AI card** — B1's user card + this window's summary + the previous AI card + operator
   identity/preference files + `plugin.yaml` → `{"ai_self_representation", "ai_identity"}`. The
   identity is written first; the six AI-self fields (positioning, current task, current goal,
   1-3 state facts, state, uncertainty) cite the summary's goal lines instead of restating them.

The baseline order is **global card files -> newest row for the subject -> preset**. The four
files under `$HERMES_HOME/minir-memory/` (see below) are the durable state once they exist; the
session row is the horizon. With neither, the user card starts from the `software-engineer` preset
(one markdown occupation line) and evidence replaces
the preset without counting as a clear. The **first base job runs even with an empty window**: it
publishes that preset baseline so the review cadence has an artifact to read. Later empty windows
are no-ops. The user card and the AI identity are durable across sessions, while the AI
self-representation, the window summary and the value markers are session-bound and drop on a session
change (`previous_content`). An exact repeat dispatch returns the existing job instead of
refreshing the same window twice.

The base row keeps `user_representation` for `user_self_representation`, `user_peer_card` for
`user_preferences`, the same-named `ai_self_representation` and `ai_identity_card` for `ai_identity`,
`window_summary` for the summary section, and `value_markers` for
`{"value_markers": [...]}`. The same data is
rendered into the four-section artifact, and every column is published once per session as its own
conclusion (`base.user_representation`, `base.user_peer_card`, `base.ai_self_representation`,
`base.ai_identity_card`, `base.window_summary`, `base.value_markers`) through
`publish_base_conclusions`, guarded by `base_refreshes.conclusions_published_at`. `is_latest=1` marks the row with the
greatest `window_to_ts` per `(session_id, subject)` — window order, not write order, so a slow
older window that finishes late is history, never "latest". `window_from_turn`/`window_to_turn`
record the session turns the window actually covered (one user message is one turn), extending
past the dispatch stamp when the newest job drained newer messages. Older windows stay queryable as
history (`base_refreshes()` / `latest_base_refresh()` in `store.py`). A
`(session, subject, from_ts)` retry updates the same row/chunk; the retry reuses the chunk's turn
range and never treats its own chunk as the previous window.

`base_refreshes` also has its own external-content FTS5 table over the six columns
(`base_refreshes_fts`, trigram). `search_base_refreshes()` supports session/history filtering and
`latest_only`; like `minir_search`, it falls back to `LIKE` when FTS5 is unavailable **and when all
query terms are shorter than three characters** (common Chinese queries such as `工具`, which
trigram cannot match). The `conclusions_fts` index remains the recall index for the rendered
sections; `base.value_markers` is indexed there too.

The window summary is a **rolling state, not a log**: pass 3 receives the previous section as
markdown and returns the whole new section following `templates/window_summary.md` (its prompt
states the caps; `window_summary.validate` only checks the value is a string). The section is
stored as written and appended to the canonical chunk chain for recall; the next pass 3 reads that
same text. Older chunks stay independent rows with their turn range and remain searchable inside
their own session, unless an explicit session-final summary supersedes them (a compression fold
never does). The dialectic worker reads the active chain up to its review window and may correct
the summary as a whole part; when the chunk row cannot be updated, the correction is not claimed.
Pass 4 is separate: it reads the same window plus the recorded value marker inventory and returns
`{"value_markers": [...]}` only, so a malformed marker set cannot cost the summary and vice versa.

### Dialectic review, base corrections, and advices

The dialectic prompt receives the session's **live** base artifact (the newest published row; a
catch-up drain may have moved past its own window), the previous review note, the operator files,
the chunk chain up to that window, and the window's messages (tool rows included). It may call
`minir_hermes_session_search` with `before=<window end>` to inspect earlier history for drift; it never
reads later turns. It reviews whole parts: the user card, the AI card and the window summary. Its
system prompt is assembled from the layer core plus the four card **review checks** in
`plugin/minir/rules/`, so the reviewer reads the same contract the writer was given. It lists a
part in `base_changed.fields` only when it changed it, and a listed part is replaced in full;
`prunings` must stay empty because only the three-day review may delete durable content. The
dialectic writes no advices: advice has a single producer, the live reviewer.

When `base_changed.fields` is non-empty, the corrected parts overwrite the base row **in one
transaction**:

1. the current six columns are snapshotted into `dialectic_refreshes` (with the affected
   `base_refresh_id`, window/turn ranges, `changed_fields`, `source_job_id`, `is_latest`);
2. the changed columns of `base_refreshes` are updated in place (window order and `is_latest` are
   unchanged);
3. the base `layer_state` artifact is rewritten from the corrected parts, and its `conclusions`
   sections are updated.

`dialectic_refreshes_fts` is an external-content trigram index over the six snapshot columns, so a
previous model state is still recallable with `search_base_revisions()`. (The table was
`base_revisions` and carried a dead `advices` column; both name and column are gone, and the
Python method names keep their old spelling.) The `advisories` ledger belongs to the live
reviewer alone. A base correction never changes which
window is `is_latest`; an operator-authored identity still wins over any inference.

The model may be told who the assistant is from
`$HERMES_HOME/minir-memory/agents/<profile>/identity.md` and `preference.md`; the profile comes from
`sessions.profile_name`. Operator-authored identity wins over inference. Session init queues a
**base** refresh when history exists (the dialectic review follows it); the call is gated to the
provider's bound primary session and is skipped for subagents/cron, which fire the same plugin
hook.

Both layers are strict on purpose: base passes must return their staged JSON object and the
dialectic must return the three parts plus `base_changed`. Any missing key, extra key, wrong enum
or placeholder fails the object; a failed object is retried once with the format rule restated, and
a second failure fails the job and leaves the previous artifact in place. A review may not empty a
part the base row carried: a listed empty part fails the job rather than clearing durable content.
A part the base already had empty is a faithful copy, not a broken reply; the base pass accepts
sparse cards, so the review must be allowed to echo them. A failed job is retried once (the cached
content is still there) and, if it fails again, the next job of that layer re-covers the same
content (the content watermark never moved).

* **Tick** — `on_turn_start` advances both counters with the host's turn number
  (`max(local + 1, host)`, so a `/resume` that rehydrates the host count moves forward instead of
  double-counting). Trivial prompts count too: this tick runs before the trivial-prompt gate that
  skips `sync_turn`. It only *records* jobs — the drain is kicked where content exists
  (`sync_turn`, a boundary, shutdown), because a job drained at tick time would read a window whose
  turn has not arrived yet.
* **Cadence** — a layer fires when it has accumulated `interval` turns *since its last trigger*
  (`turns - last_trigger_turn >= interval`), not when the count is a multiple: the periodic tick
  and the boundary flushes read the counter from different threads, and a watermark makes every
  trigger idempotent. `last_trigger_turn`/`last_trigger_ts` advance at dispatch time.
* **Windows are edge-to-edge** — `from_ts` of the next window is `to_ts` of the previous one (the
  first starts at the session's start time), so no turn falls between two windows or is reasoned
  over twice.
* **The content watermark, not the dispatch stamp, decides coverage** — `on_turn_start` stamps
  `to_ts` *before* the turn exists, so a job's own window cannot contain the turn that triggered
  it. A job reads `(max(from_ts, processed_ts), end]`, where `processed_ts` is what the previous
  completed job of that layer/session left behind and `end` is the job's `to_ts` — or, when no
  later job exists to inherit the open end, the newest of `now`, the newest buffered message and
  the newest `state.db` row, so the newest job always drains everything available. The next job or the boundary drain
  therefore covers the trigger turn, exactly once, and a re-queued window reads the same content
  again.
* **Boundaries** — `on_session_end` flushes **both** layers' counted tails (when the counter
  already sits on a dispatched watermark the flush is a no-op, so a `/new` — which fires
  `on_session_finalize`, the queued `on_session_end` *and* `on_session_reset` for one boundary —
  queues that window exactly once) and then queues each layer whose *content* no job has covered.
  `on_pre_compress` sets the compression gate, queues and synchronously drains the base -> review
  pair (base first), then moves both watermarks to the current turn.
* **First job** — `cold_start` queues one base job at session start even before history exists.
  It advances that layer's window edge (`mark_dispatched`) so the next tick cannot re-dispatch the
  same coarse-clock window; the review watermark still starts at session start, so the first review
  waits for its own cadence. A stop never swallows a tail that arrives after it (the CLI freezes the
  session before the queued flush lands); only `tick` treats a stop as final.
* **Nothing to observe costs nothing** — a window whose messages are already covered, or empty,
  completes as `done` without a model call, and a session that only ever greets never pays.
* **Restart** — the window is read from `$HERMES_HOME/state.db`, so a job that was open when the
  process ended can still be completed. Only when the profile has no `state.db` does
  `initialize()` expire inherited jobs (queued before this process started); a second provider
  instance in the same process never loses its live queue.
* **`/branch` inherits** — a branch continues its parent's transcript, so it continues the
  parent's counters and watermarks *and* is handed the parent's buffered messages; `/resume`
  opens a fresh state that the host's turn number rehydrates, and `/reset`/`/new` stop the old
  session and open the new one with `session_mode`.
* **Guardrails** — counters exist only for primary-context sessions, per-session state is capped
  (64 sessions, oldest closed evicted first), the transcript keeps the newest messages under a
  count and a character budget (8 sessions), and the registry is process-wide
  (`turn_counter.TRACKER`) because the host can construct the provider twice — once through the
  memory-provider loader and once through general plugin discovery — while driving only one
  instance.

### Durable card files and the three-day review

Four files under `$HERMES_HOME/minir-memory/` are the profile's durable card state. They are the
first baseline for a base refresh and the only place a durable item may be deleted:

| File | Content |
| --- | --- |
| `user/identity.md` | `user_self_representation` markdown body: who the person is |
| `user/preference.md` | `user_preferences` markdown body: how they want the assistant to work |
| `agents/<profile>/identity.md` | `ai_identity` markdown body: who the assistant is, duties and domains |
| `agents/<profile>/preference.md` | `ai_preference` markdown body: voice and conduct, permissions, prohibitions |

Each file is front-matter (`schema: minir-card/v1`, scope, part, generated_at, source, evidence,
reviewer) plus one markdown body. There is no field list and no JSON body: the body is the card,
and an empty body is a valid empty card. `card_review.py` is the only writer: the merge runs as
**four single-file passes**, one for each global card file. The two user files share
`card_review_user`; the two AI files share `card_review_ai`. Each pass reads one rule file and
returns one final card body and nothing else: the database is the change history. The four passes
are joined into one write, and the write validates all four bodies, enforces the per-file
`card_token_budget` (2000 by default) and requires markdown text; one invalid file fails the whole
review and leaves all four files untouched.
Every write records a `card_revisions` row (`before_hash`/`after_hash`, `changed_fields: ["body"]`,
source). A hand edit is detected by comparing the file hash with the last revision and recorded as
`operator_edit` before the review takes over; the same diff also lists the file under
"Hand-edited since the last review" in that file's prompt, so the model knows which card carries
operator text. A legacy file with no front-matter is copied to `<name>.declared.md` once. The
review receives the current files as its baseline.

The scheduler is wall-clock, not turn-driven: a review is due every **3 days**
(`card_review_days`), the first anchor is written at install, the anchor advances only on success,
and a failure retries after 30 minutes, 2 hours and 6 hours before waiting for the next period.
It runs on the `minir_card_view` route with no tools (files and evidence are inline).

### Knowledge pages

B4's value markers feed a file layer: one page per `(category, org, topic)` under
`$HERMES_HOME/minir-memory/knowledge/<org>/<CATEGORY>-<slug>.md`. The orgs are `entities`,
`trajectories`, `experiences`, `events`, `tools` and `projects`; a single dated occurrence with no
reusable lesson files under `events`, and the lesson it did leave would file under `experiences`.
The filename is stable for a
topic (CJK preserved, separators stripped, collisions hashed); a rewrite bumps `revisions` in the
front-matter, an older `time` never overwrites a newer page, and the per-file
`knowledge_token_budget` (2000 by default) rejects an over-long page, keeping the old one and
writing `_failed/<CATEGORY>-<slug>.json`. A failed page never propagates into a base job.
`agents/knowledge_render.md` is the page renderer's agent contract: the idle-session extraction
pass runs it, with `rules/markers-category.md` and `rules/markers-org.md` appended to its system
prompt, to write pages from a finished session. A marker supplies the page's classification, its
`render_plan` and the `anchor` slice to re-read; the body always comes from the session, never from
the marker.

Retrieval is explicit: `minir_kb_search` searches the page text with the profile's managed `rg`
(found through `<HERMES_HOME>/tools/facts.json`, `PATH` as fallback), preferring body lines, and
the model opens a page with its file tool. `prefetch()` does not inject page paths; the profile's
`knowledge.select_paths` scorer still backs the adviser's seed index.

### The transcript event store

Completed host turns and every layer pass are appended to
`$HERMES_HOME/minir-memory/sessions/<profile>/<layer>/<session_id>.jsonl`: one JSON object per
line, one line per event, never rewritten, compressed or deleted. Host streams use the `host`
layer; base, dialectic, card_review, adviser and `minir_agents` runs use their own layer
directory, partitioned by the profile `initialize` resolved. Parts rotate at 2000 lines
(`<session_id>.partN.jsonl`) and reads continue across them by `seq`. The adviser's reasoning
trail is raw text in `sessions/<profile>/reasoning/<session_id>.log`.

Each row carries `v`, `seq`, `ts`/`iso`, `profile`/`layer`/`session_id`, `turn`/`step`, `role`
(`user`/`system`/`assistant`/`tool`/`reasoning`), `kind` (`msg`/`aux`/`run_start`/`run_end`/`gap`/
`marker`), `run_id`, `redacted`, and the role's own fields (`text`, `tool_calls`, `usage`,
`tool_call_id`, `result`, `fp`, ...). Bodies are full: the 4000-character cut and whitespace
collapse survive only in the in-memory window the layer worker reads.

Reads go through `TranscriptBuffer.read_events` (memory first, the stream's own file for rows the
bounded cache no longer holds) and report `source` (`buffer`/`disk`/`mixed`) plus `coverage`.
`sync_turn` appends only the tail after the last `fp` fingerprint; a compaction that removed the
anchor writes a `gap` row instead of silently replaying. A clean close appends one
`marker{marker:"last_cache"}` row per stream this process wrote, and the first process after a
full shutdown replays that range. A crash skips the marker: the appended rows are still readable,
nothing is replayed. `minir_stats` reports the stream table with row and byte counts.

### Where the model call comes from

Base and dialectic passes run through `agent_loop.run` on the `auxiliary.minir_dialectic` route
with a **fixed** toolset built by `tools.agent_toolsets(...)`: the host's `file` toolset, this
plugin's `minir` toolset (which carries `minir_hermes_session_search` and `minir_aiagent_search`)
and `skills`. Operator-configured toolsets are additive defaults, never a widening: the list is
never `None`, because `agent_loop.run` passes the list it was given, so an empty list means no
tools while `None` would mean every tool.
`agent_loop.run` marks every peer run with `file_guard.aux_scope()` and passes the project root
(the nearest `.git` above the cwd), so a `pre_tool_call` guard scopes the file tools for every
plugin agent: reads stay under `$HERMES_HOME` and the project root, minus secret files and
`.git` metadata; writes only under `minir-memory/scratch/`. Each pass is prompted to call
`minir_hermes_session_search` with the session id and window from its prompt when it needs more
evidence:

```yaml
auxiliary:
  minir_dialectic:
    provider: deepseek
    model: deepseek-v4-pro
    toolsets: [file]        # file/minir/skills are always appended
```

If the agent route cannot run (host module absent, bad config, provider outage), the pass falls
back to `worker.call_model`, which imports `agent.auxiliary_client.call_llm` lazily, under the
same key. Base and dialectic share one route on purpose: one model serves both, and only the
prompt and the output cap differ. The three routes are pinned independently:

```yaml
auxiliary:
  minir_dialectic:  {provider: deepseek, model: deepseek-v4-pro,   timeout: 90}
  minir_card_view:  {provider: deepseek, model: deepseek-v4-pro,   timeout: 180}
  minir_knowledge:  {provider: deepseek, model: deepseek-v4-flash, timeout: 120}
```

Without those blocks the host's normal auto chain applies (main model first), exactly as
`plugins/memory/query_rewrite.py` does it. Every failure is contained: the worker is a daemon
thread, a layer refresh never raises into a turn, and `reasoning_enabled: false` makes
`sync_turn` a no-op so no window is buffered and no model is ever called.

### Prompt contract

Every model call uses the same split: the **system message** owns the role, trust order and output
contract; the **user message** owns task metadata and labeled data blocks. This keeps instructions
stable across windows and leaves the evidence replaceable.

- **Trust order** — operator-authored identity/preferences (trusted) > the raw conversation
  window (untrusted evidence) > previous cards, reviews, chunk summaries and drafts (derived
  drafts). A derived item is never evidence for itself; current evidence beats a previous item.
- **Data, not instructions** — `prompting.data_block()` wraps every non-system input in a named
  tag (`<base_user_transcript>`, `<base_ai_transcript>`, `<window_summary_transcript>`,
  `<value_markers_transcript>`, `<dialectic_transcript>`, `<previous_user_cards>`,
  `<base_layer_artifact>`,
  `<live_turn_events>`, …). Reserved tags appearing inside a payload are rewritten as literal
  text, so a message cannot close its block early or forge another block. Adviser-mode events are
  JSONL: one event per line, with newlines escaped, so event text cannot create prompt structure.
- **Exact output shape** — base passes return their staged JSON keys and the dialectic pass
  returns the same parts; pass 3's `window_summary` is one markdown section, and the B3 prompt
  states its caps. Cards state the display-safe item counts and character limits (a
  fixed-size card section would otherwise truncate a long list); the worker keeps a wider
  hard cap and truncates anything that exceeds it. Arrays may be empty when a sparse window
  proves nothing. A dialectic reply that leaves a field as `[unknown] …` placeholders is not a
  correction; `[]` on a listed representation card empties it. The window record and the marker
  inventory are merged, never cleared.
- **One vocabulary per concept** — `prompting.MARKER_TYPES` defines the five value markers and
  `prompting.ADVICE_TARGETS` defines the advice targets; the base, dialectic, provider and
  adviser prompts are composed from those tables, so a stored item and the pass that later judges
  it cannot disagree about what a type means. Both advice producers normalize an unknown target
  to `general`.
- **Evidence discipline** — each prompt says to omit rather than guess, to preserve exact paths,
  commands, error text, versions, IDs and user corrections, and to keep secrets out. The
  dialectic pass separates drift review per field from the advice policy
  (`nit`/`concern`/`blocker`), and `base_changed.changed` is true exactly when `fields` is
  non-empty.
- **Bounded inputs** — the window keeps the newest messages under its character budget with an
  omission marker; the previous artifact and value markers are fed back only where the merge
  needs them (the base `## Window Summary` projection is deliberately recomputed, not fed back).

## The adviser auxiliary (optional agent loop)

`adviser.py` owns the plugin's auxiliary routes, and the loop they feed runs a **real agent loop**
(its own LLM and tools) rather than a one-shot completion. The base refresh (B1 + its B3/B4
children + B2) and the
independent review share `auxiliary.minir_dialectic` (`worker._adviser_consult`); the adviser chain
keeps `auxiliary.adviser`, the card review uses `auxiliary.minir_card_view` and knowledge
extraction `auxiliary.minir_knowledge`. The loop is not route-specific: it lives in
`agent_loop.py` and can be reused for any one-shot peer run (a review, a summary, a critic):

```python
from . import agent_loop

result = agent_loop.run("message", context="evidence", model="", provider="",
                        toolsets=["file"], system_message="be blunt")
result = await agent_loop.run_async("message", context="evidence", toolsets=["file"])
result.answer, result.api_calls, result.model, result.provider, result.toolsets
```

The external surface is deliberately small — **message, context, system message, model, provider,
toolsets**. Everything else is resolved inside, the way the gateway resolves a message before it
builds an agent (`_resolve_model_and_runtime` in
`plugins/platforms/feishu/feishu_comment.py`): `_resolve_gateway_model(_load_gateway_config())`
for the model (with the provider's default as the fallback), `_resolve_runtime_agent_kwargs()` for
`api_key`/`base_url`/`api_mode`/`credential_pool` and the gateway's fallback chain, and
`resolve_reasoning_config(config, model)` for the final model — the chokepoint that makes
`reasoning_effort` reach the adapter. The system message goes to the *turn*
(`run_conversation(..., system_message=...)`), so the user message stays exactly what the caller
asked and the instructions stay instructions.

Every peer agent, layer passes included, inherits read-only access to the project root: the
nearest `.git` above the cwd, or `MINIR_PROJECT_DIR` when the process does not run inside the
project. `auxiliary.adviser.project_dir` overrides it on the adviser routes; an unusable
override disables project reads there rather than falling back silently. Writes stay under
`minir-memory/scratch` for every plugin agent.

A pinned `provider` that differs from the gateway route is *re-resolved as a provider*
(`resolve_runtime_provider`), so an override is a real route and not another provider's model with
the main key.

Both entry points share one implementation: construct one `AIAgent` with `quiet_mode=True`,
`skip_context_files=True`, `skip_memory=True`, `skip_background_review=True`,
`load_soul_identity=True`, the fixed tool budget (`MAX_ITERATIONS`, the Feishu comment agent's 15)
and an explicit `enabled_toolsets`, call `run_conversation(prompt, system_message=...)`, and
`close()` it in a `finally` so a long-lived host does not leak tool subprocesses or HTTP clients.
`run_async` is `asyncio.to_thread` around the same run, because `AIAgent`'s loop is synchronous (no
`async def` in `run_agent`/`agent.conversation_loop`); the blocking HTTP and tool subprocesses stay
off the event loop, and `close()` still happens in the thread that built the agent.

`adviser.py` is a thin route owner: it registers the four auxiliary blocks (`adviser` plus
`minir_dialectic` / `minir_card_view` / `minir_knowledge`, each with
`provider`/`model`/`toolsets`) and the read-only host session search, while
`adviser_mode.AdviserMode.consult()` supplies the shared `ADVISER_CORE + MODE_CONSULT` system
message for the worker's internal synchronous path. There is no model-visible consult tool: its
schema, handler, dynamic registration, prompt section and toolset were deleted. The host session
search is registered synchronously (`is_async=False`); its handler injects the caller class and
platform, so a model cannot pick another layer or widen the host-session filter by argument.

```yaml
auxiliary:
  adviser:
    provider: ''            # '' / auto / main = the gateway's route; anything else is re-resolved
    model: ''               # '' = the gateway's model
    toolsets: []            # e.g. [file, web]; empty = no tools, one completion
```

`register()` adds both surfaces next to the memory provider — `ctx.register_auxiliary_task("adviser")`
for the config block/picker entry and `ctx.register_tool(...)` for
`minir_hermes_session_search` — and the tools are
declared under `provides_tools:` in `plugin.yaml`, which is what `hermes plugins validate` checks.

### Session search (`minir_hermes_session_search`)

The `minir` toolset exposes `minir_hermes_session_search`: read-only, stdlib-only recall over
the host's own `$HERMES_HOME/state.db` (`session_search.py`). Nothing is copied; every answer is the
host's rows, bounded per message. Five shapes, selected by `mode` or inferred from the arguments:

| `mode` | What it answers | Main arguments |
| --- | --- | --- |
| `search` | FTS5 over message content (`messages_fts`; `messages_fts_trigram` for CJK substrings), `LIKE` fallback; returns snippets + session context | `query`, `match` (all/any/phrase/raw), `role`, `source`, `after`/`before`, `limit`/`offset` |
| `sessions` | Browse/filter session metadata: id/title substring, source, time window, archived/hidden | `query`, `source`, `sort`, `after`/`before` |
| `messages` | Read one session's transcript, paged from either end | `session_id`, `role`, `order`, `limit`/`offset` |
| `around` | A window of messages on both sides of one message id | `session_id`, `message_id`, `window` |
| `stats` | Counts, roles/sources, FTS availability, DB size, oldest/newest row | — |

Defaults mirror the host's own recall: live (`active`) rows only, compaction summaries excluded,
`user`/`assistant` for `search`, all roles for a transcript read, and synthetic rows
(`[Assistant Rules]`, `[System: …]`, `[minir summary …]`, `[minir memory notice]`) hidden. Hidden session sources
(`kanban`, `subagent`, `tool`) and archived/hidden sessions are excluded unless `include_hidden` /
`include_archived` says otherwise; `include_inactive`, `include_compacted` and `include_synthetic`
open the other filters. `profile: work` reads `$HERMES_HOME/profiles/work/state.db` instead of the
active profile. Time bounds accept epoch seconds, ISO date/datetime, or `7d`/`24h`/`2w`; results
report `total`, `has_more` and `next_offset` so a caller can page without guessing.

The default scope is the caller's platform: a host caller sees only sessions whose
`sessions.source` maps to the `platform` the host passed to `initialize` (`cli`, `tui`, `desktop`,
`telegram`, `feishu`, ...; the map lives in `session_search._PLATFORM_SOURCES`). `platform="*"`
disables the default filter, an explicit `platform` selects another surface's sources, and an
agent caller is scoped to the current host session unconditionally (its `platform` argument is
ignored, not rejected). The envelope echoes the filter it applied, and an empty filtered result
says whether the store has rows at all.

The handler is synchronous (`is_async=False`): one bounded SQLite read with no model call. It returns
the same `{"success": true, "data": …}` / `{"success": false, "error": {"type", "message"}}` envelope
as every other tool, and never raises into the tool loop.

Registration is deliberately *optional*: if the host refuses the auxiliary key (another plugin owns
it), the warning is logged and the memory provider still loads, because the provider is why the
directory is imported at all; the session search is registered before the key, so it survives that
refusal too. The key is process-wide, so a standalone `adviser` plugin cannot stay
installed next to this one — the second registration is refused (route) or ignored (tool name).

Why a private `AIAgent` instead of `ctx.subagent_lifecycle.launch()`: the adviser is a **peer** of the
caller (advice, not delegated work), it must not appear in the delegation tree, and it needs its own
model route rather than the delegation config. The cost is the private `run_agent` import that
`hermes plugins validate` reports as a catalog-rule-9 warning — the same trade the Feishu plugin
makes, and the reason this surface is one module you can delete wholesale.

One ordering trap, documented in `agent_loop.run`: `run_agent` bootstraps the managed environment and
*replaces* `sys.path`, so importing it after `hermes_cli.config` orphans that module's lazy
`hermes_yaml`/`ruamel` import. `run_agent` is imported first, inside the runner, every time.


## Adviser mode (per-turn side channel)

The dialectic layer is slow and window-scoped: it corrects the long-term user/AI cards after a
base refresh. Adviser mode is the fast loop: it watches the current turn and nudges the main
agent before its next model request. The two share the `auxiliary.adviser` route, the
`minir_hermes_session_search` toolset, the `advisories` ledger and severity/dedupe rules, but not
write authority:

| | dialectic layer | adviser mode |
| --- | --- | --- |
| Trigger | base refresh / compression boundary | tool cadence, tool error, mutation, final answer |
| Input | base artifact + window history | user goal, intent, tool results, final answer, long-term context |
| Output | six corrected cards + `## Base Revision` note | `silent` or 1–3 advices |
| Writes | `base_refreshes`, `dialectic_refreshes`, `layer_state` | `advisories` only |
| Delivery | none: memory correction, no advice | `llm_request`, boundary aside, gate, gateway |
| Lifetime | durable audit | current turn, expires |

Config (under `memory.minir`) is the only switch — there is no `/adviser` command:

```yaml
adviser_mode:
  enabled: false            # config controls the mode; no per-session toggle
  mode: watch               # watch | adviser_only
  cadence: 3                # tool calls between reviews
  severity_floor: concern   # nit | concern | blocker
  max_reviews_per_turn: 6
  max_advices: 3            # rows per injected block
  max_chars: 900
  toolsets: [adviser]       # read-only by default; never inherits the base/dialectic route
  # omp `advisor` parity knobs; remaining host-only bounds are in doc/omp-adviser-hermes-
  # integration.md §5, and the closed reviewer-side list is §9.8.
  review_mode: turn         # turn | agent-end (only the finished turn)
  review_interval: 1        # every N tool-triggered updates
  max_notes_per_update: 4   # non-blockers accepted per review; blockers exempt
  continue_severity: concern
  immune_turns: 3           # concerns stay out of mid-turn injection after an intervention
  block_tools: true         # a live blocker refuses the next mutating tool (once per advice)
  verify_continue: false    # a live blocker keeps a code-editing turn going once
  aside_at_turn_start: true # below-floor advice (nit) rides the next turn's user message
  sync_backlog: off         # wait: let one in-flight review finish before the request
  wait_ms: 1500
  notify_gateway: true      # gateway turns: send preserved advice to the chat
  project_context: true     # AGENTS.md + WATCHDOG.md into the review prompt
  max_project_chars: 1500
  include_reasoning: false  # needs host plugins.stream_reasoning_deltas too
  reasoning_log: true       # with include_reasoning: full stream to sessions/reasoning/
  pause_seconds: 300        # usage/rate-limit pause for one adviser
  failure_pause_seconds: 60 # repeated generic failures pause the same member briefly
  max_consecutive_failures: 3
  boundary_every: 50        # turn-end blocks carry a boundary guidance line
  history_messages: 60      # model-facing adviser history cap
  history_chars: 120000
  roster: []                # empty = one `default` adviser; no fixed cap; see below
```

A roster pays for one review per member. There is no fixed cap; a member with `paused: true`
is skipped until the flag is removed. Each member reviews the same delta with its own cursor,
model, instructions and persistent runtime, and its name is stamped on every row it publishes:

```yaml
  roster:
    - name: quality
      instructions: Focus on robustness and failure paths.
    - name: cost
      model: deepseek-v4-flash
      instructions: Focus on wasted model calls and latency.
```

Changes are read when the provider opens a session; restart Hermes to apply them to a running one.

The main adviser runs on the same `auxiliary.adviser` route with a five-iteration tool budget.
Its input is a bounded transcript delta: `pre_llm_call` records the user goal,
`on_interim_message`/`post_api_request` record the assistant's intent, `post_tool_call` records
the tool arguments/result/error plus the reasoning since the previous event, and `post_llm_call`
records the final answer. Tool rows keep the full result up to the per-field cap and mark every
kept without a per-field cap. The delta budget is 24k of whole events: the oldest events drop
first, and one oversized event is kept whole. Each review continues that
adviser's own `AdvisorRuntime` through `conversation_history`, so it can build on its earlier
tool reads instead of re-deriving them. It may call `minir_hermes_session_search` to ground a claim.
Its system block is composed dynamically from
`memory.minir.adviser_mode.enabled`: severity, every target in `prompting.ADVICE_TARGETS`
(definition + usage), the severity × target handling matrix, the nit-report table, and the
language rule. When the mode is off, those sections are absent.

Delivery is one chain with one producer: the live reviewer writes advice, the guard filters it,
the ledger stages it, and the hooks deliver it.

1. Review — `review_once` reads the live event ring from its own cursor and returns the strict
   JSON advice contract. There is no second reviewer and no candidate intake; the dialectic
   layer corrects memory and emits no advice.
2. Mid-loop (`llm_request`) — pending `source='adviser_mode'` advice at or above
   `severity_floor` is appended to the next provider request (after the last tool result, or as a
   synthetic user message), then staged. `post_api_request` marks it delivered;
   `api_request_error` returns it to pending.
3. Emission guard — before a row is written it is normalized (NFKC, lowercase, punctuation-free),
   checked against the noise list, deduped against the session ledger (an escalation still
   passes), and capped at `max_notes_per_update` non-blockers per review. A blocker is always
   published and does not spend that budget. Drop counts land on the review row
   (`notes_dropped`, `drop_detail`) and surface in `minir_stats`.
4. Turn boundary (`pre_llm_call`) — advice below the floor (nits, and concerns inside the immune
   window) rides the next turn's user message once, then is marked `injected`.
   A note marked `deferred`, or a non-blocker from an earlier turn, waits for the turn-end
   aggregate. The final review renders every pending row once, with `(N turns ago)` and, every
   `boundary_every` turns, a `[boundary-guidance]` line.
5. Gates — a live blocker refuses the next mutating tool through `pre_tool_call` (at most twice
   per turn, consumed by the refusal), and with `verify_continue` keeps one code-editing turn
   going through `pre_verify`.
6. Gateway — when a turn ends before the review publishes, `notify_gateway` sends the preserved
   block to the chat through the host's `send_message` tool instead of waiting for the next turn.
   A send failure leaves the rows pending, so the normal channels still carry them.
7. Failure isolation — model errors, non-JSON output, a session that ended mid-review, and
   dropped reviews insert nothing; the review row records `failed`/`dropped` and the next review
   recomputes from the same cursor. A non-JSON reply gets one no-tools repair pass first, because
   a small route can spend its tool budget before writing the object.

`adviser_only` is enforced, not just prompted: `pre_llm_call` injects the advisory brief and
`pre_tool_call` blocks mutating tools (`write_file`, `patch`, `terminal`, code execution,
`delegate_task`, `cronjob`, `cronjob_manage`) with a message asking for a proposed change.
The policy is checked independently of the review/compression gate, so a compression cannot
briefly re-enable writes.
The main agent still sees the schemas; the gate is the policy, so a blocked call costs one tool
iteration.

The block is bounded and tagged (`[adviser — advisory only]`). Two hard limits remain: plugins
cannot interrupt a running tool, and they cannot wake a finished turn. A blocker therefore
refuses the *next* mutating tool instead of aborting the current one, and preserve-in-chat is the
only way advice reaches a turn that already ended. The middleware skips a block already present in
the request tail, so the host loading the same plugin directory twice cannot inject it twice.

Adviser mode never writes base cards. It may use a compact dialectic digest (user path, peer
constraints, latest review) as context; dialectic input filters adviser injections so ephemeral
advice cannot become a "fact" in the long-term model. Compression clears the event ring and
expires pending adviser advice; the compression gate pauses new reviews until the summary is
committed.

## The context engine (audited summary compression)

One directory, two surfaces: the same plugin is a **memory provider** (`memory.provider: minir`)
and a **context engine** (`context.engine: minir`). Only one context engine can be active, and the
host never activates one on its own — so installing minir as a provider does not take that slot;
`register()` deliberately registers the provider and its hooks only, and the engine is found by the
engine loader as the module-level `MinirContextEngine` subclass.

```yaml
context:
  engine: minir        # replaces the built-in ContextCompressor for this profile
```

### One trigger, one synchronous pipeline

The engine no longer folds progressively on the dialectic beat. It has a single trigger: the
window has consumed **95% of the configured threshold** (`last_prompt_tokens >=
0.95 * threshold_tokens`). At that point the host calls `on_pre_compress` and then `compress()`:

1. `on_pre_compress` sets a per-session **compression gate**, queues the base job for the
   unreasoned window, drains the base and review jobs **synchronously** (base first), resets both
   layer counters to the current turn, and leaves the gate in place.
2. `compress()` calls B3 once more, with the newest base window summary as its merge baseline and
   the turns since that window's `window_to_ts` as its digest. The pass runs on the gateway route
   with no tools, `layer="window_summary"` and a 90-second budget; its system prompt is B3's core
   plus `templates/window_summary.md`. The reply is cleaned, not validated: a summary missing a
   section is still folded, and a failed call folds the previous base summary (or the empty
   template when there is none).
3. The returned message list is:

```text
[system prompt]
[first-compression notice]     # once, inserted after the system prompt
[protected head messages]
[assistant summary message]    # full final summary, [minir summary #<run_id>]
[protected tail messages]      # unmodified, opens on a user turn
```

The summary message is an **assistant** row and carries no folded-uid list. Recovery is through
`minir_search` and `minir_hermes_session_search`; the old carrier/`select_context()` /
`minir_context_recall` path is gone.

### Gate, counters, and resume

Periodic base/dialectic dispatch is paused while the gate is active. `on_turn_start` skips the
tick, so the explicit pre-compression jobs cannot race a periodic job. The engine clears the gate
after `compress()` (or on `on_turn_complete` when it sees the summary marker); a ten-minute lease
also expires the gate if compression crashes before it can resume. `reset_layers()` moves both
`last_trigger_turn` watermarks to the current turn, so the next periodic tick starts a fresh
interval instead of immediately repeating the same window.

### Summary storage and FTS

The summary is written to `conclusions` as `session_summary`, session-scoped and keyed by
`session_summary:<index>`, where `index` is this session's next compression count
(`next_session_summary_index`). It is `active` from the start: there is no pending phase and no
promotion step, and the `[minir summary #<run_id>]` marker is only for folding the carrier message
(the run id rides `evidence`). The full summary text is stored (the assistant
message is not a 900-character excerpt) and `conclusions_fts` already indexes it with the trigram
tokenizer; `minir_search` can recall it inside its own session. There is no separate FTS table to
build. A subsequent compression replaces the previous summary message rather than accumulating a
second one in the head.

### Synthetic rows do not recurse

The assistant summary and the first-compression notice are derived rows. `history.read_messages`,
`latest_ts`, and `minir_hermes_session_search` filter the `[minir summary` and
`[minir memory notice` prefixes role-agnostically, so a later base/dialectic window cannot treat
the model's own summary as real assistant evidence and summarize it again. With
`include_synthetic=True`, `minir_hermes_session_search` can still expose those rows explicitly.

`reasoning_enabled: false` skips the summary agent and the layer worker; `compress()` then folds
to the prepared chunk/review (or the previous summary) so the window still shrinks.

### Safety

* **Fail-open.** Every hook returns "leave it alone" on any error: a broken engine is never worse
  than no engine.
* **Thread model.** The compression pass may run on a pooled daemon thread with different sessions
  concurrently; the engine holds a lock around its own state and touches no thread-local state.
* **`clone_for_agent()`** hands every agent (parent, subagents, gateway sessions) its own engine
  with the same config and no session state.
* **Legacy rows** without a `message_uid` are simply not recoverable by id; the engine never fails
  closed on them.

## Data layout

Everything the plugin writes lives under `$HERMES_HOME/minir-memory/`:

    memory.db          SQLite: conclusions + FTS, layer artifacts, jobs, usage, extraction, adviser
    sessions/          <profile>/<layer>/<session>.jsonl transcripts; .log for the reasoning trail
    knowledge/         <org>/<CATEGORY>-<slug>.md durable pages
    user/              operator-written user cards (identity.md, preference.md)
    agents/<profile>/  the agent's own cards
    run/               one process lock per live process

Conclusions are searched with `minir_search`, pages with `minir_kb_search`, the host's
conversation with `minir_hermes_session_search`, and the plugin's own transcripts with
`minir_aiagent_search`. The bundled `minir:data-layout` skill carries the per-table, per-field
and per-file reference; load it before inspecting any of these by hand.

## Install & activate

Clone the repo anywhere, then install the plugin **into a directory whose name equals the
provider name** and point `memory.provider` at it:

```bash
# Windows (git-bash); use $HERMES_HOME on Linux/macOS
git clone <your-fork-url> ~/src/minir-memory
cd ~/src/minir-memory
python scripts/install.py --check   # report: plugin target + patch status, change nothing
python scripts/install.py           # copy the plugin, apply + verify the host series
```

`scripts/install.py` is the one-command install. It mirrors `plugin/minir` into
`$HERMES_HOME/plugins/minir` (backing up the previous copy under
`$HERMES_HOME/minir-memory/plugin-backup/`), then applies `plugin/minir/host-patches/` to
`$HERMES_HOME/hermes-agent` with `git apply`. The series adds the plugin control bridge
(`hermes_cli/plugin_runtime.py`), the stop-gate continuation channel, the live `message.adviser`
event with a labelled adviser block on every host surface (Desktop card, TUI card that the web
chat inherits over its PTY, classic CLI block, web session-inspector row), and the
history/compaction guards that keep an adviser row from being treated as a human turn;
without it the adviser falls back to the ledger channels (see the adviser section). The installer
is idempotent, refuses to apply over overlapping local edits, verifies the patched host by
importing both Python modules, and rolls back if verification fails. Its record lives in
`$HERMES_HOME/minir-memory/host-patches.json`; `--revert` undoes the series in reverse order.

```bash
python scripts/install_host_patches.py --check     # patch status only
python scripts/install_host_patches.py --revert    # undo what this install applied
python scripts/install.py --no-copy                # plugin installed by hermes; patch only
```

Hermes has no post-install hook, so the patch step belongs to this script rather than
`hermes plugins install`; re-run it after a host update (the update overwrites patched files, the
state file remembers what was applied).

`pip install .` is the alternative for the **memory provider**: the wheel ships
`plugin/minir` as a top-level `minir` package and declares the `hermes_agent.memory_providers`
entry point, so discovery resolves it without a directory copy. The context engine
(`context.engine: minir`) resolves only from `$HERMES_HOME/plugins/<name>`, so keep the
directory install for that surface. Use exactly one install visible on the discovery path.

```yaml
# $HERMES_HOME/config.yaml
memory:
  provider: minir
  minir:                       # the provider's own block: memory.<provider name>
    context_cadence: 24        # turns per base-layer job (alias: base_interval)
    dialectic_cadence: 8       # turns per review job; the two layers never share a watermark
    dialectic_depth: 2         # upper bound: 1 synthesize, 2 audit, 3 reconcile (adaptive by window size)
    reasoning_enabled: true    # run the layer reasoning passes (auxiliary model, no key needed)
    session_mode: default      # mode stamped on each newly opened session
    prefetch_limit: 5          # cap for one minir_search result set
    content_language: en       # language for card/knowledge prose; field names stay English
    buffer_streams: 20         # transcript streams cached in memory (current session pinned)
    buffer_events_per_stream: 4000  # per-stream cache cap; the JSONL file keeps everything
    buffer_bytes: 268435456    # total cache budget in bytes (oldest streams evict first)
    card_token_budget: 2000    # hard per-file budget for the four global card files
    card_review_days: 3        # days between global card reviews
    knowledge_token_budget: 2000  # hard per-file budget for one knowledge page
    session_extraction_enabled: true  # final summary + knowledge extraction after the idle window
    knowledge_idle_hours: 24      # quiet hours after session end before that run (0 = next tick)
    adviser_mode:
      enabled: false              # the optional main adviser
      host_steer: true            # use the host control bridge when the patch series is installed
      host_continue: true         # let a finished turn be kept open for advice
      watchdog_roster: true       # merge advisors[] from the nearest WATCHDOG.yml
      roster: []                  # [{name, model?, instructions?, toolsets?, cadence?, review_mode?}]
      include_reasoning: false    # needs plugins.stream_reasoning_deltas too
      sync_backlog: off           # off | wait | strict (strict is capped by strict_max_ms)
      strict_max_ms: 30000
```

`memory.minir.*` is the block this plugin reads and the one `hermes memory setup` writes
(`memory_setup.py` persists the schema answers under `memory.<provider>`), so the wizard and a
hand-edited config.yaml are equivalent. Most keys also have an env override —
`MINIR_CONTEXT_CADENCE`, `MINIR_DIALECTIC_CADENCE`, `MINIR_DIALECTIC_DEPTH`,
`MINIR_BASE_INTERVAL`, `MINIR_DIALECTIC_INTERVAL`, `MINIR_REASONING_ENABLED`,
`MINIR_SESSION_MODE`, `MINIR_PREFETCH_LIMIT`, `MINIR_CONTENT_LANGUAGE`,
`MINIR_CARD_TOKEN_BUDGET`, `MINIR_CARD_REVIEW_DAYS`, `MINIR_KNOWLEDGE_TOKEN_BUDGET`,
`MINIR_SESSION_EXTRACTION`, `MINIR_KNOWLEDGE_IDLE_HOURS`, `MINIR_BUFFER_STREAMS`,
`MINIR_BUFFER_EVENTS`, `MINIR_BUFFER_BYTES` — and **the
env var wins** when both are set. Layer intervals, depth, `reasoning_enabled` and the transcript
cache budget are read when a session opens.

Every published conclusion is searchable: the six base conclusions, the tool markers, the
dialectic review, the compression record written at each fold, and the final `session_summary` of
the idle close. The index uses
FTS5 `trigram` so Chinese queries work; if the SQLite build lacks FTS5, `minir_search` falls back
to a token scan. Dialectic refreshes are a separate audit surface (`search_base_revisions`,
trigram over the six pre-overwrite fields); pending advices live in `advisories` and are
delivered through `pre_llm_call`, not injected as durable recall. Session summaries, reviews,
chunks and session-bound value markers only match their own `session_id`; the four durable base
conclusions and durable markers recall across sessions. `minir_search` defaults to the
current session's view and only crosses sessions when `scope="all"` is passed.

Restart Hermes (an already-running Gateway/TUI keeps the previous code in memory).
Data lands in `$HERMES_HOME/minir-memory/memory.db`; delete that file to reset. A database
written by an earlier release that captured every turn is migrated on first open: the `turns`
table is dropped and its conversations are gone for good.

The **directory name is what `memory.provider` selects** — `find_provider_dir(name)`
looks up `$HERMES_HOME/plugins/<name>/`. The `name:` field in `plugin.yaml` and the
provider's `name` property are labels and should match, but the directory is the
address. To install under a different name, rename all three together.

The same directory is also what `context.engine` selects (`find_engine_dir(name)`), so one install
covers both surfaces: set `memory.provider: minir` for memory, `context.engine: minir` for the
context engine, or both. Neither one activates the other.

## Verify

```bash
# 1. behavior suite — hermetic, uses a shim when the host is absent
python -m pytest tests -q

# 2. authoritative contract run — real ABC from a Hermes source checkout
HERMES_WORKSPACE=/path/to/hermes/workspace python -m pytest tests -q
#    (or simply run pytest with the Hermes venv python, where the real modules already import)

# 3. end-to-end against the live install, in a throwaway HERMES_HOME — discovery, the provider,
#    the counting loop, the plugin hooks, and the context engine's load + summary fold
python scripts/verify_against_host.py

# 4. Hermes' own registration validator / discovery doctor
hermes plugins validate plugin/minir
hermes plugins doctor plugin/minir

# 5. host patch series: installer logic is covered without a host; the series itself is
#    verified against the real checkout by the installer's post-apply probe
python -m pytest tests/test_install_host_patches.py -q
python scripts/install_host_patches.py --check
```

`tests/conftest.py` reports which mode it ran in (`contract mode = host|partial|shim`).
Host mode is the one that counts — and `HERMES_WORKSPACE` set to a workspace whose modules do
not import now *fails* the run (with the import error) instead of silently testing the shim,
which cannot fail. Pytest config lives in `pyproject.toml` (`testpaths = ["tests"]`); the plugin
package is `plugin/minir`, so it is never collected as a test module.

## Fork checklist

1. **Rename** `minir` → your name in: the `plugin/minir/` directory, `plugin.yaml` `name:`, the
   provider's `name` property, the engine's `name` property (it must equal the directory name for
   `context.engine` to resolve), tool prefixes, `MINIR_*` env vars, and the `pyproject.toml`
   package/entry-point names.
2. **Replace the storage**: `plugin/minir/store.py` is self-contained (open/remember/forget/
   record_job/claim_jobs/complete_job/fail_job/layer_state/search/stats/close). Keep that method
   shape and `__init__.py` needs no edits; go further for a remote backend (HTTP client,
   background write queue, retries). Whatever you add, keep the two invariants the tests pin:
   no conversation content lands in the store, and a job's window is resolved from a content
   watermark rather than from its dispatch stamp.
3. **Swap the transcript**: `transcript.py` is what the layers reason over. If your host can hand
   you a durable, permission-scoped transcript, replace the buffer and make
   `LayerWorker.expire_orphans` re-queue instead of expiring (that is the only reason the jobs
   table has an expiry path at all).
4. **Rework injection**: `_layer_block` assembles the live AI self + window summary block; keep the
   read one indexed row and never call a remote backend from `prefetch()` (the host bounds it with a
   timeout and skips a provider whose previous call is still running).
5. **Retune the layer prompts**: the agent cores under `agents/` and `worker.LAYER_SECTIONS` are the whole semantic contract of a refresh — the section headings are
   validated, so changing them changes what the worker will accept.
6. **Add tools**: a schema dict + a branch in `handle_tool_call`. Return
   `json.dumps({...})` on success and `tool_error("...")` on failure.
7. **Extend the suite**: every contract change belongs in `tests/` in the same commit —
   the suite is the acceptance gate for this template.

## Pitfalls this template already handles

* **Threading** — `sync_turn` and `prefetch` run on different host
  threads, and the layer worker is a third one; `store.py` shares one connection
  (`check_same_thread=False`) under an RLock with WAL mode, `transcript.py` guards its
  buckets, and the worker claims one job at a time with a single `UPDATE`.
* **`hermes_home` is profile-scoped** — always use the `initialize()` kwarg, never
  hardcode `~/.hermes`, or multiplexed profiles cross-contaminate.
* **No transcripts on disk** — the schema holds conclusions, job bookkeeping and layer artifacts and
  nothing else; `tests/test_provider.py` asserts that a distinctive sentence handed to
  `sync_turn` is present in no row of any table, and the host verification probe repeats the
  check through the real loader.
* **Non-primary contexts don't write or count** — `sync_turn` and the turn counter return
  early for `agent_context` other than `primary` (subagent/cron/flush), per the host contract.
* **Failures never break a turn** — every hook swallows and logs its own errors, including the
  worker's model call.
* **Config lives in config.yaml, overridden by env** — the non-secret `get_config_schema()`
  fields are persisted by the *host* into `memory.minir.*` (`hermes memory setup` in the CLI,
  and the dashboard's provider panel through `web_routers/memory_providers.py`, which writes
  the provider block itself when the plugin does not override `save_config`). That deliberate
  non-override keeps the write atomic and under the host's config lock; every field also carries
  an `env_var` (`MINIR_PREFETCH_LIMIT`, `MINIR_CONTEXT_CADENCE`, `MINIR_DIALECTIC_CADENCE`,
  `MINIR_DIALECTIC_DEPTH`, `MINIR_REASONING_ENABLED`, `MINIR_SESSION_MODE`) and the env var wins
  over config.yaml. Override
  `save_config(values, hermes_home)` only if your values need a native format the host cannot
  write.
* **Everything injected is bounded** — stored conclusions and artifacts are truncated on write
  (4000 chars), the recall block is capped at 2000, and the layer block at 1200, so nothing
  unbounded reaches the prompt.
* **Counting is off the bounded-hook path** — the counters run on `on_turn_start` /
  `on_session_end`, which the host never abandons on a timeout. Plugin hooks stay cheap:
  `pre_llm_call` reads a cached review and returns one bounded block (once per turn id),
  `post_tool_call` aggregates counters in memory, and the summary/worker model calls run only on
  their own daemon threads or the compression path.
* **Layer cadence** — `context_cadence` (alias `base_interval`, default 20) and
  `dialectic_cadence` (alias `dialectic_interval`, default 10) are read when a session opens;
  changing them mid-session only affects sessions opened later, because moving a watermark
  retroactively would re-queue windows that already dispatched.

## Optional surfaces (not wired here)

* `ctx.register_skill(name, ...)` inside `register()` to ship a skill with the provider.
* A sibling `cli.py` (scanned by `plugins/memory` via `discover_plugin_cli_commands()`)
  for provider-specific `hermes` subcommands.
* The `hermes_agent.memory_providers` entry point is already declared in `pyproject.toml`;
  this plugin ships no `cli.py`, so `hermes <provider>` subcommands are unavailable.

## License

MIT — see `LICENSE`.
