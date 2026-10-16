# minir-memory

A minimal, dependency-free **Hermes memory-provider plugin template** — a working
reference implementation of the `MemoryProvider` contract that you can fork, rename,
and grow into a real provider.

It stores completed turns and explicit notes in a per-profile SQLite database, recalls
by keyword search, and exposes four tools (`minir_search`, `minir_remember`,
`minir_forget`, `minir_stats`). No API keys, no external binaries, stdlib only.

```
plugin.yaml            manifest (name/version/description + metadata)
__init__.py            register(ctx) + MinirMemoryProvider — the contract surface
store.py               SQLite storage layer (swap it for your backend)
turn_counter.py        base / dialectic layer turn counters + job windows
tests/                 pytest suite: contract, store, provider behavior, counting
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
| `sync_turn(user, assistant, **kw)` | after each turn, background worker | `messages`, `turn_author` are optional kwargs |
| `get_tool_schemas()` / `handle_tool_call()` | tool dispatch | handlers **must return a JSON string** |
| `shutdown()` | teardown | idempotent |
| `on_memory_write(...)` | built-in memory tool writes | mirror `add`/`replace`/`remove` if you want both stores searchable |
| `on_session_switch(...)` | `/resume`, `/branch`, `/reset`, `/new`, compression | rebind per-session state; `reset=True` means a new conversation, a non-empty `parent_session_id` marks a branch |
| `get_config_schema()` | `hermes memory setup` | env-var-only fields keep it simple (see below) |
| `on_turn_start(turn_number, message, **kw)` | before each user turn | **the counting tick** — the host's own per-session turn number; never fires for a subagent/cron turn |
| `on_session_end(messages)` | real session boundary | stop counting + flush the un-reasoned tail (LLM-bound work is queued on the host's memory worker) |
| `on_pre_compress(messages)` | before compression | same tail flush; must return a string (this one returns `""`) |
| `on_delegation`, `backup_paths` | optional | not implemented here — read the ABC before opting in |

The ABC lives in the Hermes install at `agent/memory_provider.py`; treat that file
as the source of truth, not this table.

### Plugin hooks this template registers

`register(ctx)` also subscribes to the *plugin* hook surface (`ctx.register_hook`), which the
memory-provider loader forwards to a real `PluginContext` — so one plugin directory can expose
both surfaces:

| Hook | Used for |
| --- | --- |
| `on_session_start(session_id=, model=, platform=)` | record the session the host opened |
| `on_session_reset(session_id=, old_session_id=, reason=)` | `/new`, `/reset`: stop the old counters, stamp the mode on the new session |
| `on_session_finalize(session_id=)` | process teardown: stop counting the session |
| `pre_llm_call` / `post_llm_call` / `post_tool_call` | per-turn / per-tool logging only |

Every hook `register()` subscribes to must also be listed under `provides_hooks:` in
`plugin.yaml` — `hermes plugins validate` fails the manifest otherwise (`hermes plugins doctor`
warns). `tests/test_contract.py` keeps the two lists in step.

These carry **no counting state**: the four hot ones (`pre_llm_call`, `post_llm_call`,
`post_tool_call`, `on_session_start`) run under the host's 30s `plugins.hook_callback_timeout`,
which *abandons* a slow callback and suppresses it for 60s — one slow turn would silently shift a
count. The plugin-level `on_session_end` is a per-turn event (not a session boundary), and
`on_session_finalize` results that are non-empty strings get sent to the chat as a message, so
every handler here returns `None`.

## Turn counting (base / dialectic-reasoning layers)

`turn_counter.py` keeps two counters per session — one per layer — and turns them into *jobs*:
"reason over session X's history in `(from_ts, to_ts]`". A job is one row in the store's `jobs`
table, status `pending`; the worker that consumes it (history lookup + reasoning) is out of
scope for this template.

* **Tick** — `on_turn_start` advances both counters with the host's turn number
  (`max(local + 1, host)`, so a `/resume` that rehydrates the host count moves forward instead of
  double-counting). Trivial prompts count too: this tick runs before the trivial-prompt gate that
  skips `sync_turn`.
* **Cadence** — a layer fires when it has accumulated `interval` turns *since its last trigger*
  (`turns - last_trigger_turn >= interval`), not when the count is a multiple: the periodic tick
  and the boundary flushes read the counter from different threads, and a watermark makes every
  trigger idempotent. `last_trigger_turn`/`last_trigger_ts` advance at dispatch time.
* **Windows are edge-to-edge** — `from_ts` of the next window is `to_ts` of the previous one (the
  first starts at the session's start time), so no turn falls between two windows or is reasoned
  over twice.
* **Boundaries** — `on_session_end` (and `on_pre_compress`) flush the dialectic layer's tail and
  then freeze the session. This is the spec's "no trigger on a multiple": when the counter already
  sits on a dispatched watermark the flush is a no-op, so a `/new` — which fires
  `on_session_finalize`, the queued `on_session_end` *and* `on_session_reset` for one boundary —
  queues that window exactly once. A stop never swallows a tail that arrives after it (the CLI
  freezes the session before the queued flush lands); only `tick` treats a stop as final.
* **`/branch` inherits** — a branch continues its parent's transcript, so it continues the
  parent's counters and watermarks; `/resume` opens a fresh state that the host's turn number
  rehydrates, and `/reset`/`/new` stop the old session and open the new one with `session_mode`.
* **Guardrails** — counters exist only for primary-context sessions, per-session state is capped
  (64 sessions, oldest closed evicted first), and the registry is process-wide
  (`turn_counter.TRACKER`) because the host can construct the provider twice — once through the
  memory-provider loader and once through general plugin discovery — while driving only one
  instance.

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
    base_interval: 20          # turns per base-layer job
    dialectic_interval: 10     # turns per dialectic-reasoning job
    session_mode: default      # mode stamped on each newly opened session
    capture_turns: true        # store completed turns
    prefetch_limit: 5          # memory items injected per turn
```

`memory.minir.*` is the block this plugin reads and the one `hermes memory setup` writes
(`memory_setup.py` persists the schema answers under `memory.<provider>`), so the wizard and a
hand-edited config.yaml are equivalent. Every key also has an env override —
`MINIR_BASE_INTERVAL`, `MINIR_DIALECTIC_INTERVAL`, `MINIR_SESSION_MODE`, `MINIR_CAPTURE_TURNS`,
`MINIR_PREFETCH_LIMIT` — and **the env var wins** when both are set. Layer intervals are read
when a session opens.

Restart Hermes (an already-running Gateway/TUI keeps the previous code in memory).
Data lands in `$HERMES_HOME/minir-memory/memory.db`; delete that file to reset.

The **directory name is what `memory.provider` selects** — `find_provider_dir(name)`
looks up `$HERMES_HOME/plugins/<name>/`. The `name:` field in `plugin.yaml` and the
provider's `name` property are labels and should match, but the directory is the
address. To install under a different name, rename all three together.

## Verify

```bash
# 1. behavior suite — hermetic, uses a shim when the host is absent
python -m pytest tests -q

# 2. authoritative contract run — real ABC from a Hermes source checkout
HERMES_WORKSPACE=/path/to/hermes/workspace python -m pytest tests -q
#    (or simply run pytest with the Hermes venv python, where the real modules already import)

# 3. end-to-end against the live install, in a throwaway HERMES_HOME
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

1. **Rename** `minir` → your name in: the installed directory, `plugin.yaml` `name:`,
   the `name` property, tool prefixes, and `MINIR_*` env vars.
2. **Replace the storage**: `store.py` is self-contained (open/add_turn/remember/
   forget/record_job/search/stats/close). Keep that method shape and `__init__.py` needs no
   edits; go further for a remote backend (HTTP client, background write queue, retries).
3. **Rework recall**: `_search` + `_format_block` decide what reaches the prompt. If
   your backend is remote, never call it from `prefetch()` — queue in `queue_prefetch()`
   and return the cached block from `prefetch()` (the host bounds `prefetch()` with a
   timeout and skips a provider whose previous call is still running).
4. **Add tools**: a schema dict + a branch in `handle_tool_call`. Return
   `json.dumps({...})` on success and `tool_error("...")` on failure.
5. **Extend the suite**: every contract change belongs in `tests/` in the same commit —
   the suite is the acceptance gate for this template.

## Pitfalls this template already handles

* **Threading** — `sync_turn`, `queue_prefetch`, and `prefetch` run on different host
  threads; `store.py` shares one connection (`check_same_thread=False`) under an RLock
  with WAL mode.
* **`hermes_home` is profile-scoped** — always use the `initialize()` kwarg, never
  hardcode `~/.hermes`, or multiplexed profiles cross-contaminate.
* **Non-primary contexts don't write or count** — `sync_turn` and the turn counter return
  early for `agent_context` other than `primary` (subagent/cron/flush), per the host contract.
* **Failures never break a turn** — every hook swallows and logs its own errors.
* **Config lives in config.yaml, overridden by env** — the five non-secret
  `get_config_schema()` fields are persisted by the *host* into `memory.minir.*`
  (`hermes memory setup` in the CLI, and the dashboard's provider panel through
  `web_routers/memory_providers.py`, which writes the provider block itself when the plugin
  does not override `save_config`). That deliberate non-override keeps the write atomic and
  under the host's config lock; every field also carries an `env_var`
  (`MINIR_CAPTURE_TURNS`, `MINIR_PREFETCH_LIMIT`, `MINIR_BASE_INTERVAL`,
  `MINIR_DIALECTIC_INTERVAL`, `MINIR_SESSION_MODE`) and the env var wins over config.yaml.
  Override `save_config(values, hermes_home)` only if your values need a native format the
  host cannot write.
* **Store content is bounded** — text is truncated on write (4000 chars) and recall
  blocks are capped (2000 chars) so nothing unbounded reaches the prompt.
* **Counting is off the bounded-hook path** — the counters run on `on_turn_start` /
  `on_session_end`, which the host never abandons on a timeout; the plugin-level
  `post_llm_call`/`post_tool_call`/`pre_llm_call` handlers only log.
* **Layer cadence** — `base_interval` (default 20) and `dialectic_interval` (default 10) are
  read when a session opens; changing them mid-session only affects sessions opened later,
  because moving a watermark retroactively would re-queue windows that already dispatched.

## Optional surfaces (not wired here)

* `ctx.register_skill(name, ...)` inside `register()` to ship a skill with the provider.
* A sibling `cli.py` (scanned by `plugins/memory` via `discover_plugin_cli_commands()`)
  for provider-specific `hermes` subcommands.
* Pip distribution with a `hermes_agent.memory_providers` entry point instead of a
  directory install.

## License

MIT — see `LICENSE`.
