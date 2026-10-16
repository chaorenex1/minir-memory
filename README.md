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
tests/                 pytest suite: contract, store, provider behavior
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
| `on_session_switch(...)` | `/resume`, `/branch`, `/reset`, `/new`, compression | rebind per-session state; `reset=True` means a new conversation |
| `get_config_schema()` | `hermes memory setup` | env-var-only fields keep it simple (see below) |
| `on_session_end`, `on_pre_compress`, `on_delegation`, `on_turn_start`, `backup_paths` | optional | not implemented here — read the ABC before opting in |

The ABC lives in the Hermes install at `agent/memory_provider.py`; treat that file
as the source of truth, not this table.

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
```

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
Host mode is the one that counts. The pytest config lives in `tests/pytest.ini` so the
run's rootdir stays inside `tests/` — the repo root is itself a Python package (the
plugin's `__init__.py`), and pytest would otherwise import the plugin as a test module
before running anything.

## Fork checklist

1. **Rename** `minir` → your name in: the installed directory, `plugin.yaml` `name:`,
   the `name` property, tool prefixes, and `MINIR_*` env vars.
2. **Replace the storage**: `store.py` is self-contained (open/add_turn/remember/
   forget/search/stats/close). Keep that method shape and `__init__.py` needs no edits;
   go further for a remote backend (HTTP client, background write queue, retries).
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
* **Non-primary contexts don't write** — `sync_turn` returns early for
  `agent_context` other than `primary` (subagent/cron/flush), per the host contract.
* **Failures never break a turn** — every hook swallows and logs its own errors.
* **Config is env-var-only** — both `get_config_schema()` fields carry `env_var`
  (`MINIR_CAPTURE_TURNS`, `MINIR_PREFETCH_LIMIT`), so the plugin follows the ABC rule
  and needs no `save_config()`. To write config from the dashboard instead, override
  `save_config(values, hermes_home)`.
* **Store content is bounded** — text is truncated on write (4000 chars) and recall
  blocks are capped (2000 chars) so nothing unbounded reaches the prompt.

## Optional surfaces (not wired here)

* `ctx.register_skill(name, ...)` inside `register()` to ship a skill with the provider.
* A sibling `cli.py` (scanned by `plugins/memory` via `discover_plugin_cli_commands()`)
  for provider-specific `hermes` subcommands.
* Pip distribution with a `hermes_agent.memory_providers` entry point instead of a
  directory install.

## License

MIT — see `LICENSE`.
