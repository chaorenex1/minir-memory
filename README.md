# minir-memory

A minimal, dependency-free **Hermes memory-provider plugin template** — a working
reference implementation of the `MemoryProvider` contract that you can fork, rename,
and grow into a real provider.

It keeps explicit notes and every derived conclusion in a per-profile SQLite database, recalls
them through an FTS5 trigram index (CJK-safe, with a token-scan fallback), and exposes four tools
(`minir_search`, `minir_remember`, `minir_forget`, `minir_stats`). On top of that it runs the
**base / dialectic reasoning layers**: the turn counter queues a base job every *n* turns, a
background worker builds the user/AI cards from the host's own history, and a dependent dialectic
pass reviews those cards against that history. The review can correct the base artifact (a
pre-overwrite snapshot and an FTS index are kept) and emits severity-ranked advices, which are
injected into later turns for the main session or the user to decide.

**It stores no transcripts.** The worker reads `(from_ts, to_ts]` messages from
`$HERMES_HOME/state.db` read-only (plus the in-process buffer for turns not yet committed) and
persists only what the layers *derived* from them plus notes you asked to keep. Derived content
may quote or reference the messages it reasoned over — it is a derived artifact, not a redacted
one — but the provider never copies the conversation itself. Because the host store is the source
of truth, a job queued before a restart can still be completed; only when no `state.db` exists are
inherited jobs expired.

The same directory also ships a **context engine** for progressive, lossless context management:
on every dialectic beat it moves the oldest unprotected slice of the window into a compact carrier
row, restores the original text on demand from the host's own `state.db`, writes an optional
Task Handoff summary for the folded range, and queues the dialectic refresh.

No external binaries, stdlib only. Layer calls go through the host's auxiliary client, so there is
no key to configure; set `reasoning_enabled: false` to keep the provider a pure note store, and
`handoff_enabled: false` to keep compression off the summary model.

```
pyproject.toml         package metadata, pytest config, pip entry point
plugin/minir/          the deployable plugin directory (copy this to plugins/minir)
  plugin.yaml          manifest (name/version/description + metadata)
  __init__.py          register(ctx) + MinirMemoryProvider + the engine export
  store.py             SQLite storage: notes, conclusions + FTS5, base revisions/advices, jobs, metrics
  history.py           read-only host history, profile resolution, identity files
  transcript.py        in-memory, bounded window of the session's messages (never persisted)
  turn_counter.py      base / dialectic layer turn counters + job windows
  worker.py            drains base jobs, then the dependent dialectic review (base corrections + advices)
  handoff.py           Task Handoff compression prompt + validation
  cadence.py           the dialectic beat, shared by the provider and the engine
  context_engine.py    progressive lossless context management (context.engine: minir)
  agent_loop.py        one-shot in-process AIAgent runs — sync + async (Feishu-shaped)
  adviser.py           optional adviser route + the adviser toolset (adviser_consult)
  session_search.py    read-only multi-shape search over the host's session store (state.db)
  prompts/             operator-supplied Task Handoff template
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
| `on_session_end(messages)` | real session boundary | freeze the session + drain the base layer (counted tail *and* any content no job covered); the dialectic review follows the base job |
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
| `on_session_start(session_id=, model=, platform=)` | record the session; queue one cold-start *base* job when history exists, gated to the bound primary session |
| `on_session_reset(session_id=, old_session_id=, reason=)` | `/new`, `/reset`: stop the old counters, initialize the new session |
| `on_session_finalize(session_id=)` | process teardown: stop counting, flush tool metrics |
| `pre_llm_call` | inject pending dialectic advices into the user message; fall back to the legacy session review only when no advice ledger exists |
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

`turn_counter.py` keeps two counters per session. The base counter turns its cadence into *jobs*
("reason over session X's history in `(from_ts, to_ts]`"); the dialectic counter still advances
(the context engine reads its beat), but its tick triggers are filtered — the worker queues the
review itself, with the exact window the base job just published. A job is one row in the store's
`jobs` table; `worker.py` drains it from the host's `state.db` (plus the uncommitted buffer),
calls the auxiliary model, and publishes into `layer_state`/`conclusions`. Each base refresh also
appends one structured row to `base_refreshes` (session, time window, actual turn range, the six
outputs, `is_latest`); one base window has one job.
`prefetch()` injects only the newest dialectic review (`## Adviser Review`, bounded to 1200 chars)
ahead of recall. The six base cards are deliberately **not injected**; they remain recallable
through `minir_search`. Pending advices are the actionable channel and are injected by
`pre_llm_call` (bounded to 900 chars; blockers first), once per advice id.

| Layer | Models | Artifact sections | Subject key |
| --- | --- | --- | --- |
| base | this profile's user: window + previous user/AI cards, three staged JSON passes on `auxiliary.adviser` | `## Session Summary`, `## User Representation`, `## User Peer Card`, `## AI Self Representation`, `## AI Identity Card`, `## Value Markers` | `user:<user_id \| default>` |
| dialectic | the latest base artifact + history up to the window; reviews drift in all six fields and emits advices | `## Adviser Review` only (base cards stay on the base layer) | `agent:<agent_identity \| minir>` |

The dialectic job is **dependent**: a successful base refresh queues the matching review with the
same `(from_ts, to_ts]`, so the reviewer always sees a published base row. It runs an eight-key JSON
contract: the six base fields, `advices`, `value_markers`, and `base_changed`
(`{changed, fields}`). The pass count is adaptive — short windows get one synthesis, longer ones add
a self-audit and a reconcile, capped by `dialectic_depth`. A failed or malformed pass falls back to
the last valid one. `layer_state.version` counts refreshes; the newest review is what `prefetch`
injects.

A base refresh runs **three ordered passes** on the `auxiliary.adviser` route. Each pass returns
**one JSON object** (no Markdown); the worker validates the keys/arrays and renders the six fixed
sections for the artifact and the chunk chain. The adviser toolset is attached only on this route,
and the tool hint carries the window bounds: a pass may call `adviser_session_search` with
`after`/`before` for evidence inside `(from_ts, to_ts]`, but must not read later turns. The first
user message is the only allowed outside-window evidence, and only on the first refresh.

1. **user cards** — previous `## User Representation`/`## User Peer Card` + this window (+ the
   first user message as intent anchor on bootstrap) → JSON keys `user_representation`,
   `user_peer_card`;
2. **AI cards** — the user cards just derived + previous `## AI Self Representation`/
   `## AI Identity Card` + operator identity/preference files + this window → JSON keys
   `ai_self_representation`, `ai_identity_card`; the self section carries the action state
   (`[done]`/`[in-progress]`/`[blocked]`/`[next]`) and produced value
   (`[entity]`/`[trajectory]`/`[experience]`/`[tool]`/`[project]`);
3. **local summary + markers** — the user/AI cards + this window → JSON keys
   `session_chunk_summary` (local compression record) and `value_markers`, a five-key object
   (`entities`, `trajectories`, `experiences`, `tools`, `projects`). Markers record the
   session information worth keeping long-term; all five keys are always present and an empty
   array means “no item of this type”.

First refresh means no previous artifact for this user subject: it bootstraps from the window
(plus the first-message anchor) instead of merging. Later refreshes merge the previous cards, so
the durable peer and identity cards accumulate while the current-focus/self sections track the
latest state. `value_markers` are session-bound and cumulative: each pass receives the previous
markers and merges this window into them; a new session starts empty. One base window has one
job; an exact repeat dispatch returns the existing job instead of refreshing the same window
twice.

The six outputs are stored together in `base_refreshes` as JSON text (`value_markers` is a
five-key JSON object; the other five are JSON string arrays). The `layer_state` artifact,
`conclusions` sections and chunk chain render the same data as Markdown bullets for prompts and
recall.
`is_latest=1` marks the row with the greatest `window_to_ts` per `(session_id, subject)` — window
order, not write order, so a slow older window that finishes late is history, never "latest".
`window_from_turn`/`window_to_turn` record the session turns the window actually covered (one user
message is one turn), extending past the dispatch stamp when the newest job drained newer messages.
Older windows stay queryable as history (`base_refreshes()` / `latest_base_refresh()` in
`store.py`). A `(session, subject, from_ts)` retry updates the same row/chunk; the retry reuses the
chunk's turn range and never treats its own chunk as the previous window.

`base_refreshes` also has its own external-content FTS5 table over the six outputs
(`base_refreshes_fts`, trigram). `search_base_refreshes()` supports session/history filtering and
`latest_only`; like `minir_search`, it falls back to `LIKE` when FTS5 is unavailable **and when all
query terms are shorter than three characters** (common Chinese queries such as `工具`, which
trigram cannot match). The `conclusions_fts` index remains the recall index for the rendered
sections; `base.value_markers` is indexed there too.

The base layer's `## Session Summary` is **not an independent summary**: every base refresh writes
one `session_chunk_summary` (session + time range + turn range) to the canonical local-compression
chain, and the artifact's section projects **only the newest active chunk** — the current window
record. Older chunks stay as independent rows with their turn range and remain searchable by
`minir_search` inside their own session, even after a final summary supersedes them; they are not
re-projected into the live artifact. The previous projection is deliberately omitted from the next
base merge prompt (only the other five sections carry forward), so chunks do not recursively copy
the whole chain. The dialectic worker reads the active chain up to its review window and is
instructed to reconcile
contradictions across chunks (later evidence wins; stale claims and completed blockers are
removed). `base.session_summary` is not written as a competing FTS conclusion.

### Dialectic review, base corrections, and advices

The dialectic prompt receives the base artifact for *its* review window (a catch-up drain may
already have published a newer one), the previous review, the operator files, the chunk chain up
to that window, and the window's messages (tool rows included). It may call `adviser_session_search` with
`before=<window end>` to inspect earlier history for drift; it never reads later turns. The review
logic is explicit per field: user representation drift, peer-card gaps/constraints, AI action state,
and identity/preference conflicts that explain an underperforming action state. Advices are
forward-looking decisions for the main session or the user — never a restatement of established
facts — with `severity` (`nit`/`concern`/`blocker`), `target`, `evidence`, and `decision`.

When `base_changed.fields` is non-empty, the six corrected values overwrite the base row **in one
transaction**:

1. the current six fields + advices are snapshotted into `base_revisions` (with the affected
   `base_refresh_id`, window/turn ranges, `changed_fields`, `source_job_id`, `is_latest`);
2. the changed columns of `base_refreshes` are updated in place (window order and `is_latest` are
   unchanged);
3. the base `layer_state` artifact is rewritten from the corrected fields, and its
   `conclusions` sections are updated.

`base_revisions_fts` is an external-content trigram index over the six snapshot fields plus
`advices` (seven columns), so a previous model state and the advice that changed it are both
recallable with `search_base_revisions()`. Advices also go to the `advisories` delivery ledger
(severity/target/fingerprint/status); `pre_llm_call` drains pending rows, blockers first, and marks
them injected. A base correction never changes which window is `is_latest`; an operator-authored
identity still wins over any inference.

The model may be told who the assistant is from
`$HERMES_HOME/minir-memory/agents/<profile>/identity.md` and `preference.md`; the profile comes from
`sessions.profile_name`. Operator-authored identity wins over inference. Session init queues a
**base** refresh when history exists (the dialectic review follows it); the call is gated to the
provider's bound primary session and is skipped for subagents/cron, which fire the same plugin
hook.

Both layers are strict on purpose: base passes must return their staged JSON keys/arrays, and the
dialectic pass must return all eight keys with non-empty cards. Anything else **fails the job** and
leaves the previous artifact in place — a malformed artifact would otherwise replace a good one in
every later prompt. A failed job is retried once (the cached content is still there) and, if it
fails again, the next job of that layer re-covers the same content (the content watermark never
moved).

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
* **Boundaries** — `on_session_end` (and `on_pre_compress`) do two things: flush the base layer's
  counted tail (the spec's "no trigger on a multiple": when the counter already sits on a
  dispatched watermark the flush is a no-op, so a `/new` — which fires `on_session_finalize`, the
  queued `on_session_end` *and* `on_session_reset` for one boundary — queues that window exactly
  once) and then queue the base layer whose *content* no job has covered. That second half is what
  keeps short sessions useful: with `base_interval: 20`, a 4-turn session would otherwise never
  reach the user model at all. The dialectic review is never queued here: it follows a successful
  base job with the same window. A stop never swallows a tail that arrives after it (the CLI
  freezes the session before the queued flush lands); only `tick` treats a stop as final.
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

Base and dialectic passes run through `agent_loop.run` on the `auxiliary.adviser` route and always
attach the `adviser` toolset explicitly (`adviser.run` does not append it). Each pass is prompted
to call `adviser_session_search` with the session id and window from its prompt when it needs more
evidence, so the configured provider/model/toolsets are what a refresh uses:

```yaml
auxiliary:
  adviser:
    provider: deepseek
    model: deepseek-v4-flash
    toolsets: [file]        # 'adviser' is appended automatically
```

If the adviser route cannot run (host module absent, bad config, provider outage), the pass falls
back to `worker.call_model`, which imports `agent.auxiliary_client.call_llm` lazily. Each layer
uses its own task key (`minir_base_layer` / `minir_dialectic_layer`), so the two can still be
pinned independently:

```yaml
auxiliary:
  minir_base_layer:      {provider: deepseek, model: deepseek-v4-flash, timeout: 60}
  minir_dialectic_layer: {provider: deepseek, model: deepseek-v4-pro,   timeout: 90}
```

Without those blocks the host's normal auto chain applies (main model first), exactly as
`plugins/memory/query_rewrite.py` does it. Every failure is contained: the worker is a daemon
thread, a layer refresh never raises into a turn, and `reasoning_enabled: false` makes
`sync_turn` a no-op so no window is buffered and no model is ever called.

## The adviser auxiliary (optional agent loop)

`adviser.py` puts a second-opinion agent inside this plugin: an `auxiliary.adviser` model route
plus one `adviser_consult` tool that runs a **real agent loop** (its own LLM + tools) rather than a
one-shot completion. The base layer's three passes and the dependent dialectic review reuse the
same route and toolset (`worker._adviser_consult`); the loop is not adviser-specific — it lives in
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

`adviser.py` therefore only owns what the caller owns: the `auxiliary.adviser` block
(`provider`/`model`/`toolsets`), the adviser brief as the run's **system message**, and the tool
entry. `handle(args)` is the sync handler and `handle_async(args)` the async one. The tool is
registered as `handler=handle_async, is_async=True` — the host bridges that for sync callers
(`tools.registry.dispatch` → `_run_async`) and awaits it on a gateway loop, so one consult does not
stall other conversations. The pairing matters: an async handler with `is_async=False` would hand a
coroutine to the result normaliser.

```yaml
auxiliary:
  adviser:
    provider: ''            # '' / auto / main = the gateway's route; anything else is re-resolved
    model: ''               # '' = the gateway's model
    toolsets: []            # e.g. [file, web]; empty = no tools, one completion
```

`register()` adds both surfaces next to the memory provider — `ctx.register_auxiliary_task("adviser")`
for the config block/picker entry and `ctx.register_tool(...)` for `adviser_consult` — and the tools are
declared under `provides_tools:` in `plugin.yaml`, which is what `hermes plugins validate` checks.

### Session search (`adviser_session_search`)

The same `adviser` toolset also exposes `adviser_session_search`: read-only, stdlib-only recall over
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
`user`/`assistant` for `search`, all roles for a transcript read, and host-synthesized user rows
(`[Assistant Rules]`, `[System: …]`, `[minir fold …]`) hidden. Hidden session sources
(`kanban`, `subagent`, `tool`) and archived/hidden sessions are excluded unless `include_hidden` /
`include_archived` says otherwise; `include_inactive`, `include_compacted` and `include_synthetic`
open the other filters. `profile: work` reads `$HERMES_HOME/profiles/work/state.db` instead of the
active profile. Time bounds accept epoch seconds, ISO date/datetime, or `7d`/`24h`/`2w`; results
report `total`, `has_more` and `next_offset` so a caller can page without guessing.

The handler is synchronous (`is_async=False`): one bounded SQLite read with no model call. It returns
the same `{"success": true, "data": …}` / `{"success": false, "error": {"type", "message"}}` envelope
as `adviser_consult`, and never raises into the tool loop.

Registration is deliberately *optional*: if the host refuses the auxiliary key (another plugin owns
it), the warning is logged and the memory provider still loads, because the provider is why the
directory is imported at all. The key is process-wide, so a standalone `adviser` plugin cannot stay
installed next to this one — the second registration is refused (route) or ignored (tool name).

Why a private `AIAgent` instead of `ctx.subagent_lifecycle.launch()`: the adviser is a **peer** of the
caller (advice, not delegated work), it must not appear in the delegation tree, and it needs its own
model route rather than the delegation config. The cost is the private `run_agent` import that
`hermes plugins validate` reports as a catalog-rule-9 warning — the same trade the Feishu plugin
makes, and the reason this surface is one module you can delete wholesale.

One ordering trap, documented in `agent_loop.run`: `run_agent` bootstraps the managed environment and
*replaces* `sys.path`, so importing it after `hermes_cli.config` orphans that module's lazy
`hermes_yaml`/`ruamel` import. `run_agent` is imported first, inside the runner, every time.


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
pass. This engine instead moves the **oldest unprotected slice out on every dialectic-beat fold**
(the `dialectic_cadence` interval, which the engine and the provider both resolve) — the
provider's review itself follows each base refresh — so pressure never builds and the emergency
path rarely fires:

| Knob | Where it comes from |
| --- | --- |
| how often a fold may happen | the dialectic interval, resolved exactly as the provider resolves it (`MINIR_DIALECTIC_INTERVAL` → `memory.minir.dialectic_interval` → 10) and compared against the beat: the newest dialectic job's turn stamp (queued after a base refresh), the window's own turn count otherwise |
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
(`prompts/session_handoff.md`, override with `MINIR_SUMMARY_TEMPLATE`). Its input is the
**historical dialectic conclusions** (durable user/AI cards, plus any legacy
constraints/preferences/second-brain rows, previous summary) plus the **tail window's dialectic
conclusions** (current review and current-focus representation). Raw turns are used only as a
cold-start fallback when no tail conclusion exists. The validated output is this session's summary conclusion, written to
`conclusions` as `pending` (kind `session_summary`, session-scoped); a bounded copy (900 chars)
rides in the returned carrier, while the full text stays in the store;
`on_turn_complete` promotes it to `active` only when the carrier is actually in the persisted
messages, so a pass the host discarded never becomes recallable. The full template output stays
available to `minir_search` after promotion. The fold also queues the dialectic window ending at
the `should_compress()` decision. `reasoning_enabled: false` disables the summary call as well as
the layer worker. This is the same shape as the host's
built-in compressor: a pass can be discarded on timeout, so the carrier the host commits remains
authoritative and a pending summary row expires without polluting recall.

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

Clone the repo anywhere, then install the plugin **into a directory whose name equals the
provider name** and point `memory.provider` at it:

```bash
# Windows (git-bash); use $HERMES_HOME on Linux/macOS
git clone <your-fork-url> ~/src/minir-memory
mkdir -p "$LOCALAPPDATA/hermes/plugins"
cp -R ~/src/minir-memory/plugin/minir "$LOCALAPPDATA/hermes/plugins/minir"
```

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
    context_cadence: 20        # turns per base-layer job (alias: base_interval)
    dialectic_cadence: 10      # context-engine fold beat (alias: dialectic_interval); reviews follow base
    dialectic_depth: 2         # upper bound: 1 synthesize, 2 audit, 3 reconcile (adaptive by window size)
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

Every published conclusion is searchable: `notes`, the six base sections, the dialectic review,
local compression chunks (including superseded history), and compression handoffs. The index uses
FTS5 `trigram` so Chinese queries work; if the SQLite build lacks FTS5, `minir_search` falls back
to a token scan. Base revisions are a separate audit surface (`search_base_revisions`, trigram
over the six pre-overwrite fields plus advices); pending advices live in `advisories` and are
delivered through `pre_llm_call`, not injected as durable recall. Session summaries, reviews,
handoffs and chunks only match their own `session_id`; user peer cards, AI identity, value markers
and notes recall across sessions.

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
hermes plugins validate plugin/minir
hermes plugins doctor plugin/minir
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
* The `hermes_agent.memory_providers` entry point is already declared in `pyproject.toml`;
  this plugin ships no `cli.py`, so `hermes <provider>` subcommands are unavailable.

## License

MIT — see `LICENSE`.
