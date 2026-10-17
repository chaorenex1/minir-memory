# minir-memory

A minimal, dependency-free **Hermes memory-provider plugin template** — a working
reference implementation of the `MemoryProvider` contract that you can fork, rename,
and grow into a real provider.

It keeps explicit notes and every derived conclusion in a per-profile SQLite database, recalls
them through an FTS5 trigram index (CJK-safe, with a token-scan fallback), and exposes four tools
(`minir_search`, `minir_remember`, `minir_forget`, `minir_stats`). On top of that it runs the
**base / dialectic reasoning layers**: the turn counter queues a job every *n* turns, a background
worker reasons over that window from the host's own history with the auxiliary model, and the
merged per-layer conclusion is injected into later turns.

**It stores no transcripts.** The worker reads `(from_ts, to_ts]` messages from
`$HERMES_HOME/state.db` read-only (plus the in-process buffer for turns not yet committed) and
persists only what the layers *derived* from them plus notes you asked to keep. Because the host
store is the source of truth, a job queued before a restart can still be completed; only when no
`state.db` exists are inherited jobs expired.

The same directory also ships a **context engine** for progressive, lossless context management:
on every dialectic beat it moves the oldest unprotected slice of the window into a compact carrier
row, restores the original text on demand from the host's own `state.db`, writes an optional
Task Handoff summary for the folded range, and queues the dialectic refresh.

No external binaries, stdlib only. Layer calls go through the host's auxiliary client, so there is
no key to configure; set `reasoning_enabled: false` to keep the provider a pure note store, and
`handoff_enabled: false` to keep compression off the summary model.

```
plugin.yaml            manifest (name/version/description + metadata)
__init__.py            register(ctx) + MinirMemoryProvider + the engine export
store.py               SQLite storage: notes, conclusions + FTS5, jobs, metrics
history.py             read-only host history, profile resolution, identity files
transcript.py          in-memory, bounded window of the session's messages (never persisted)
turn_counter.py        base / dialectic layer turn counters + job windows
worker.py              drains jobs into five-section/dialectic artifacts at depth 1-3
handoff.py             Task Handoff compression prompt + validation
cadence.py             the dialectic beat, shared by the provider and the engine
context_engine.py      progressive lossless context management (context.engine: minir)
prompts/               operator-supplied Task Handoff template
tests/                 pytest suite: contract, store, transcript, provider, counting, worker, engine
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
| `system_prompt_block()` | prompt assembly | **static** text only; dynamic recall goes through `prefetch()` |
| `queue_prefetch(query)` | after each turn, background worker | do the I/O here |
| `prefetch(query, session_id=)` | before each LLM request, **critical path** | consume the queued result; must stay fast |
| `recall_status()` | after `prefetch()` | drives the `🧠 Provider — recalled N memories` indicator; must reflect only the last prefetch |
| `sync_turn(user, assistant, **kw)` | after each turn, background worker | hands the turn to the layer worker (in memory); `messages`, `turn_author` are optional kwargs |
| `get_tool_schemas()` / `handle_tool_call()` | tool dispatch | handlers **must return a JSON string** |
| `shutdown()` | teardown | idempotent |
| `on_memory_write(...)` | built-in memory tool writes | mirror `add`/`replace`/`remove` if you want both stores searchable |
| `on_session_switch(...)` | `/resume`, `/branch`, `/reset`, `/new`, compression | rebind per-session state; `reset=True` means a new conversation, a non-empty `parent_session_id` marks a branch |
| `get_config_schema()` | `hermes memory setup` | env-var-only fields keep it simple (see below) |
| `on_turn_start(turn_number, message, **kw)` | before each user turn | **the counting tick** — the host's own per-session turn number; never fires for a subagent/cron turn |
| `on_session_end(messages)` | real session boundary | freeze the session + drain both layers (counted tail *and* any content no job covered) |
| `on_pre_compress(messages)` | before compression | same drain, without freezing; must return a string (this one returns `""`) |
| `on_delegation`, `backup_paths` | optional | not implemented here — read the ABC before opting in |

The ABC lives in the Hermes install at `agent/memory_provider.py`; treat that file
as the source of truth, not this table.

### Plugin hooks this template registers

`register(ctx)` also subscribes to the *plugin* hook surface (`ctx.register_hook`), which the
memory-provider loader forwards to a real `PluginContext` — so one plugin directory can expose
both surfaces:

| Hook | Used for |
| --- | --- |
| `on_session_start(session_id=, model=, platform=)` | record the session; queue one cold-start dialectic job, gated to the bound primary session |
| `on_session_reset(session_id=, old_session_id=, reason=)` | `/new`, `/reset`: stop the old counters, initialize the new session |
| `on_session_finalize(session_id=)` | process teardown: stop counting, flush tool metrics |
| `pre_llm_call` | inject the latest session-scoped Adviser Review into the user message, once per turn id |
| `post_llm_call` / `post_tool_call` | per-turn logging; `post_tool_call` also aggregates tool counters |

Every hook `register()` subscribes to must also be listed under `provides_hooks:` in
`plugin.yaml` — `hermes plugins validate` fails the manifest otherwise (`hermes plugins doctor`
warns). `tests/test_contract.py` keeps the two lists in step.

These carry **no counting state**: the hot ones (`pre_llm_call`, `post_llm_call`,
`post_tool_call`, `on_session_start`) run under the host's 30s `plugins.hook_callback_timeout`,
which *abandons* a slow callback and suppresses it for 60s. They therefore never call a model:
`on_session_start` only enqueues, `pre_llm_call` reads a cached review, and `post_tool_call`
increments an in-memory counter. The plugin-level `on_session_end` is a per-turn event (not a
session boundary), so the real boundary uses the memory-provider method. Every handler except
`pre_llm_call` returns `None`; `on_session_finalize` must stay empty because the host sends a
non-empty result to the chat. `register()` closes the hooks over the provider instance and falls
back to the process-wide active provider, since the host can construct the plugin twice.

## The two layers (base / dialectic)

`turn_counter.py` keeps two counters per session — one per layer — and turns them into *jobs*:
"reason over session X's history in `(from_ts, to_ts]`". A job is one row in the store's `jobs`
table; `worker.py` drains it from the host's `state.db` (plus the uncommitted buffer), calls the
auxiliary model, and publishes a merged artifact into `layer_state` and its sections into the
FTS-indexed `conclusions` table. `prefetch()` injects the newest base (user) and dialectic (agent)
artifacts ahead of recall, so the next turn sees who the user is and how this agent should work —
without the conversations it was derived from.

| Layer | Models | Artifact sections | Subject key |
| --- | --- | --- | --- |
| base | one participant's session + durable user card + AI self/identity | `## Session Summary`, `## User Representation`, `## User Peer Card`, `## AI Self Representation`, `## AI Identity Card` | `user:<author_id \| author \| user_id>` |
| dialectic | this AI's adviser review, constraints/preferences, second brain | `## Adviser Review`, `## Constraints`, `## Preferences`, `## Second Brain` | `agent:<agent_identity \| minir>` |

Each refresh is a **merge**: the model receives the previous artifact plus the window and returns
the complete updated artifact, so contradictions are resolved and duplicates collapse instead of
piling up. The dialectic layer runs `dialectic_depth` passes: d1 synthesizes, d2 audits its own
draft against the window, d3 reconciles the two. A failed or malformed audit falls back to the last
valid pass. `layer_state.version` counts the refreshes; the newest version is what gets injected
(bounded to 1200 chars) next to the recall block (bounded to 2000).

The model may be told who the assistant is from
`$HERMES_HOME/minir-memory/agents/<profile>/identity.md` and `preference.md`; the profile comes from
`sessions.profile_name`. Operator-authored identity wins over inference. The session-init dialectic
call is gated to the provider's bound primary session and is skipped for subagents/cron, which fire
the same plugin hook.

The reasoning pass is strict on purpose: the model must return the layer's fixed headings, in
order, with at least one bullet. Anything else **fails the job** and leaves the previous artifact
in place — a malformed artifact would otherwise replace a good one in every later prompt. A
failed job is retried once (the cached content is still there) and, if it fails again, the next
job of that layer re-covers the same content (the content watermark never moved).

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
* **Boundaries** — `on_session_end` (and `on_pre_compress`) do two things: flush the dialectic
  layer's counted tail (the spec's "no trigger on a multiple": when the counter already sits on a
  dispatched watermark the flush is a no-op, so a `/new` — which fires `on_session_finalize`, the
  queued `on_session_end` *and* `on_session_reset` for one boundary — queues that window exactly
  once) and then queue any layer whose *content* no job has covered. That second half is what
  keeps short sessions useful: with `base_interval: 20`, a 4-turn session would otherwise never
  reach the user model at all. A stop never swallows a tail that arrives after it (the CLI freezes
  the session before the queued flush lands); only `tick` treats a stop as final.
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

### Where the model call comes from

`worker.call_model` imports `agent.auxiliary_client.call_llm` lazily and passes a task key
(`minir_base_layer` / `minir_dialectic_layer`), so the layers can be pinned independently:

```yaml
auxiliary:
  minir_base_layer:      {provider: deepseek, model: deepseek-v4-flash, timeout: 60}
  minir_dialectic_layer: {provider: deepseek, model: deepseek-v4-pro,   timeout: 90}
```

Without those blocks the host's normal auto chain applies (main model first), exactly as
`plugins/memory/query_rewrite.py` does it. Every failure is contained: the worker is a daemon
thread, a layer refresh never raises into a turn, and `reasoning_enabled: false` makes
`sync_turn` a no-op so no window is buffered and no model is ever called.

## The context engine (progressive lossless context)

One directory, two surfaces: the same plugin is a **memory provider** (`memory.provider: minir`)
and a **context engine** (`context.engine: minir`). Only one context engine can be active, and the
host never activates one on its own — so installing minir as a provider does not take that slot;
`register()` deliberately registers the provider and its hooks only, and the engine is found by the
engine loader as the module-level `MinirContextEngine` subclass.

```yaml
context:
  engine: minir        # replaces the built-in ContextCompressor for this profile
```

### Progressive, not one big compaction

The built-in compressor waits for the token threshold and then folds the window in one destructive
pass. This engine instead moves the **oldest unprotected slice out on every dialectic beat** — the
same turn interval the memory provider refreshes its dialectic layer on — so pressure never builds
and the emergency path rarely fires:

| Knob | Where it comes from |
| --- | --- |
| how often a fold may happen | the dialectic interval, resolved exactly as the provider resolves it (`MINIR_DIALECTIC_INTERVAL` → `memory.minir.dialectic_interval` → 10) and compared against the beat: the provider's dialectic dispatch when it runs, the window's own turn count otherwise |
| how much one fold may move | whole turns only, starting at the first turn after `protect_first_n`, stopping before `protect_last_n`, at most `_MAX_FOLD_MESSAGES` (24) messages — incremental by construction |
| when it fires anyway | the token threshold (`threshold_percent`, default 0.75) — the safety net is kept |

Folded messages are replaced by one **carrier** row: a user-role message that names the turn range
it covers, lists the `message_uid`s that went with it and carries a one-line gist (at most 200
characters, so it orients the model without becoming a second copy of the slice). It is written
with an engine-minted `message_uid`, which the host writes through the commit, so a carrier is
recognisable after a restart and a later fold absorbs its id list instead of nesting. A carrier
deliberately does **not** imitate the host's `[CONTEXT COMPACTION …]` handoff: that text tells the
model the compacted work is dead reference material, while a fold means the opposite — the work is
live and only some turns moved out of the window.

### Where "lossless" comes from

Nothing is copied. Every folded message is still verbatim in the host's own
`$HERMES_HOME/state.db`, where each row carries a stable `message_uid`:

* **`select_context()`** re-injects folded originals into a request when the incoming message really
  matches them (stopwords never count, and a match is required), and
* the **`minir_context_recall` tool** lets the model pull them on demand.

The injection only *appends* to the last user message, so every byte before it still matches the
provider's prompt cache; a request that does not match is returned untouched (`None`), which costs
no cache at all. The carriers are the durable index.

On a fold, `compress()` may make one bounded auxiliary call using the Task Handoff template
(`prompts/session_handoff.md`, override with `MINIR_SUMMARY_TEMPLATE`). The validated handoff is
written to `conclusions` as `pending` (kind `handoff`, session-scoped) and reduced to a
200-character carrier gist; `on_turn_complete` promotes it to `active` only when the carrier is
actually in the persisted messages, so a pass the host discarded never becomes recallable. The
full template output stays available to `minir_search` after promotion. The fold also queues the
dialectic window ending at the `should_compress()` decision. `reasoning_enabled: false` disables
the summary call as well as the layer worker. This is the same shape as the host's
built-in compressor: a pass can be discarded on timeout, so the carrier the host commits remains
authoritative and an orphan handoff row is harmless.

### Safety

* **Fail-open.** Every hook returns "leave it alone" on any error: a broken engine is never worse
  than no engine.
* **Thread model.** The compression pass may run on a pooled daemon thread with different sessions
  concurrently; the engine holds a lock around its own state and touches no thread-local state.
* **`clone_for_agent()`** hands every agent (parent, subagents, gateway sessions) its own engine
  with the same config and no session state.
* **Legacy rows** without a `message_uid` are simply not recoverable by id; the engine never fails
  closed on them.

## Install & activate

Clone the repo **into a directory whose name equals the provider name** and point
`memory.provider` at it:

```bash
# Windows (git-bash); use $HERMES_HOME/plugins/minir on Linux/macOS
git clone <your-fork-url> "$LOCALAPPDATA/hermes/plugins/minir"
```

```yaml
# $HERMES_HOME/config.yaml
memory:
  provider: minir
  minir:                       # the provider's own block: memory.<provider name>
    context_cadence: 20        # turns per base-layer job (alias: base_interval)
    dialectic_cadence: 10      # turns per dialectic-reasoning job (alias: dialectic_interval)
    dialectic_depth: 2         # 1 synthesize, 2 self-audit, 3 reconcile
    reasoning_enabled: true    # run the layer reasoning passes (auxiliary model, no key needed)
    handoff_enabled: true      # let compression call the Task Handoff summary model
    session_mode: default      # mode stamped on each newly opened session
    prefetch_limit: 5          # memory items injected per turn
```

`memory.minir.*` is the block this plugin reads and the one `hermes memory setup` writes
(`memory_setup.py` persists the schema answers under `memory.<provider>`), so the wizard and a
hand-edited config.yaml are equivalent. Every key also has an env override —
`MINIR_CONTEXT_CADENCE`, `MINIR_DIALECTIC_CADENCE`, `MINIR_DIALECTIC_DEPTH`,
`MINIR_BASE_INTERVAL`, `MINIR_DIALECTIC_INTERVAL`, `MINIR_REASONING_ENABLED`,
`MINIR_HANDOFF_ENABLED`, `MINIR_SESSION_MODE`, `MINIR_PREFETCH_LIMIT` — and **the env var wins**
when both are set. Layer intervals, depth and `reasoning_enabled` are read when a session opens.

Every published conclusion is searchable: `notes`, the five base sections, the dialectic
review/constraints/preferences/second brain, and compression handoffs. The index uses FTS5
`trigram` so Chinese queries work; if the SQLite build lacks FTS5, `minir_search` falls back to
a token scan. Session summaries, reviews and handoffs only match their own `session_id`; user peer
cards, AI identity, second-brain rows and notes recall across sessions.

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
#    the counting loop, the plugin hooks, and the context engine's load + lossless recall
python scripts/verify_against_host.py

# 4. Hermes' own registration validator / discovery doctor
hermes plugins validate .
hermes plugins doctor .
```

`tests/conftest.py` reports which mode it ran in (`contract mode = host|partial|shim`).
Host mode is the one that counts — and `HERMES_WORKSPACE` set to a workspace whose modules do
not import now *fails* the run (with the import error) instead of silently testing the shim,
which cannot fail. The pytest config lives in `tests/pytest.ini` so the
run's rootdir stays inside `tests/` — the repo root is itself a Python package (the
plugin's `__init__.py`), and pytest would otherwise import the plugin as a test module
before running anything.

## Fork checklist

1. **Rename** `minir` → your name in: the installed directory, `plugin.yaml` `name:`, the
   provider's `name` property, the engine's `name` property (it must equal the directory name for
   `context.engine` to resolve), tool prefixes, and `MINIR_*` env vars.
2. **Replace the storage**: `store.py` is self-contained (open/remember/forget/
   record_job/claim_jobs/complete_job/fail_job/layer_state/search/stats/close). Keep that method
   shape and `__init__.py` needs no edits; go further for a remote backend (HTTP client,
   background write queue, retries). Whatever you add, keep the two invariants the tests pin:
   no conversation content lands in the store, and a job's window is resolved from a content
   watermark rather than from its dispatch stamp.
3. **Swap the transcript**: `transcript.py` is what the layers reason over. If your host can hand
   you a durable, permission-scoped transcript, replace the buffer and make
   `LayerWorker.expire_orphans` re-queue instead of expiring (that is the only reason the jobs
   table has an expiry path at all).
4. **Rework recall**: `_search` + `_format_block` decide what reaches the prompt. If
   your backend is remote, never call it from `prefetch()` — queue in `queue_prefetch()`
   and return the cached block from `prefetch()` (the host bounds `prefetch()` with a
   timeout and skips a provider whose previous call is still running).
5. **Retune the layer prompts**: `worker.BASE_SYSTEM_PROMPT` / `DIALECTIC_SYSTEM_PROMPT` and
   `LAYER_SECTIONS` are the whole semantic contract of a refresh — the section headings are
   validated, so changing them changes what the worker will accept.
6. **Add tools**: a schema dict + a branch in `handle_tool_call`. Return
   `json.dumps({...})` on success and `tool_error("...")` on failure.
7. **Extend the suite**: every contract change belongs in `tests/` in the same commit —
   the suite is the acceptance gate for this template.

## Pitfalls this template already handles

* **Threading** — `sync_turn`, `queue_prefetch`, and `prefetch` run on different host
  threads, and the layer worker is a third one; `store.py` shares one connection
  (`check_same_thread=False`) under an RLock with WAL mode, `transcript.py` guards its
  buckets, and the worker claims one job at a time with a single `UPDATE`.
* **`hermes_home` is profile-scoped** — always use the `initialize()` kwarg, never
  hardcode `~/.hermes`, or multiplexed profiles cross-contaminate.
* **No transcripts on disk** — the schema holds notes, job bookkeeping and layer artifacts and
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
  `MINIR_DIALECTIC_DEPTH`, `MINIR_REASONING_ENABLED`, `MINIR_HANDOFF_ENABLED`,
  `MINIR_SESSION_MODE`) and the env var wins over config.yaml. Override
  `save_config(values, hermes_home)` only if your values need a native format the host cannot
  write.
* **Everything injected is bounded** — stored notes and artifacts are truncated on write
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
* Pip distribution with a `hermes_agent.memory_providers` entry point instead of a
  directory install.

## License

MIT — see `LICENSE`.
