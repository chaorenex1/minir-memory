---
name: data-layout
description: "Use when reading or inspecting Minir's stored data: which directory holds what, and the tool or query for each store."
version: 1.0.0
author: Masia
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [minir, data, layout, sqlite, transcripts, knowledge, cards]
---

# Minir Data Layout

Everything the plugin writes lives under one root:

    $HERMES_HOME/minir-memory/

`$HERMES_HOME` is the active profile's home (`~/.hermes` on Linux/macOS,
`%LOCALAPPDATA%\hermes` on Windows). Profiles are separate homes, so nothing here is shared
across profiles except the paths themselves.

    $HERMES_HOME/
      state.db                          host conversation store; owned by Hermes, not by Minir
      minir-memory/
        memory.db                       SQLite: conclusions, layer artifacts, jobs, usage
        memory.db-wal, memory.db-shm    live WAL files; copy them with the .db for a snapshot
        sessions/<profile>/<layer>/     append-only JSONL transcripts, one file per session
        knowledge/<org>/                one markdown page per durable topic
        user/identity.md                operator-written user identity card
        user/preference.md              operator-written global user preference card
        agents/<profile>/identity.md    the agent's own identity card
        agents/<profile>/preference.md  the agent's own preference card
        run/<pid>-<rand>.lock           one lock per live process
        backups/, plugin-backup/, host-patches*   install artifacts, not runtime data

Read with the tools first. Direct file or SQL access is for inspection and for data the tools do
not expose; the store owns its schema and migrations, so never write `memory.db` by hand.

## memory.db — the SQLite store

Open it read-only when the gateway is running: `sqlite3 "file:...memory.db?mode=ro"`. The
`conclusions` table is also mirrored into an FTS5 trigram index, which is what `minir_search`
queries.

| Table | Holds | Read it with |
|---|---|---|
| `conclusions` | Every stored conclusion: explicit markers (`note`, `entities`, `experiences`, `tools`, `projects`, `trajectories`, `todo`), the published layer artifacts (`base.*`, `dialectic.*`, `window_summary` chunks, `session_summary`), keyed by `kind` + `key`, with `status` (`active`/`superseded`/`expired`) | `minir_search`; SQL on `kind`, `session_id`, `subject` |
| `layer_state` | The latest published artifact per `(layer, subject)`; `content` is the full markdown | SQL: `SELECT content FROM layer_state WHERE layer=? AND subject=?` |
| `base_refreshes` | One row per base window: window bounds, four card fields, `window_summary`; the card and summary columns are JSON-encoded strings | SQL + `json.loads` on each column |
| `dialectic_refreshes` | The dialectic layer's per-window rows, same shape | SQL + `json.loads` |
| `jobs` | Layer job ledger: `layer`, window, `status` (`pending`/`running`/`done`/`failed`/`expired`), attempts, error | SQL; the worker also logs each run |
| `session_extractions` | The idle-timer queue: `due_at`, `markers` (JSON), `status`, `pages`, `summary_chars` | `minir_stats` for counts; SQL for one session |
| `session_usage`, `turn_usage` | Token accounting: per session and per turn, including cache and reasoning buckets | `minir_stats`; SQL for a range |
| `adviser_sessions` | Whether adviser mode is on for a session, its mode and cadence | SQL |
| `adviser_reviews` | One row per review run: trigger, status, covered seq range, model, latency, error | SQL |
| `advisories` | Advice rows: severity, target, content, `status`, `decision`, delivery stamps | `minir_search` (they are conclusions-like rows); SQL for delivery state |
| `card_revisions` | Card file history: scope, part, path, hashes, diff, reviewer, status | SQL, newest first |
| `compression_gates` | The active compression boundary per `(session_id, run_id)` with `expires_at` | SQL |
| `metrics` | Tool call counters and latency (`tool`, `count`, `total_ms`, `max_ms`, `errors`) | SQL |

Typical queries:

    -- what this session concluded, newest first
    SELECT kind, key, status, content FROM conclusions
     WHERE session_id = ? ORDER BY updated_at DESC LIMIT 20;

    -- the published base artifact for the user subject
    SELECT content FROM layer_state WHERE layer = 'base' AND subject = 'user';

    -- anything still queued
    SELECT layer, session_id, status FROM jobs WHERE status IN ('pending','running');

## sessions/ — the transcript streams

    sessions/<profile>/<layer>/<session>.jsonl
    sessions/<profile>/<layer>/<session>.part1.jsonl   (later parts, never rewritten)

One JSON object per line. A stream is `(profile, layer, session)`: `profile` is the Hermes
profile, `layer` is who wrote the event (`host` for the live conversation, `base`,
`window_summary`, `dialectic`, `adviser`, `card_review`, `extraction`, `session_summary`, or a
custom `minir_agents` name). Rows carry `seq`, `ts`, `iso`, `kind` (`msg`, `run_start`,
`run_end`, `gap`), `role`, `profile`, `layer`, `session_id`, `text`, `fp`, and for agent runs
`model`, `provider`, `toolsets`, `usage`, `prompt_hash`. Tool events add `tool_calls`/`result`;
`reasoning` rows are `kind="msg"` with `role="reasoning"`.

Read it with `minir_aiagent_search` when the calling agent has that toolset (modes `messages`,
`search`, `around`, `stats`; layer and profile are fixed by the caller). Otherwise read the file
directly, e.g.:

    python -c "import json,sys; [print(json.loads(l)['role'], json.loads(l).get('text','')[:80]) for l in open(sys.argv[1],encoding='utf-8') if l.strip()]" <file>

A raw-text stream is `<session>.log`; the adviser reasoning trail lives in
`sessions/<profile>/reasoning/<session>.log` and is appended text, not JSONL.

One session's live host conversation is also in `$HERMES_HOME/state.db`; that copy is what
`minir_hermes_session_search` reads, and it is the authoritative one for the host's own messages.

## knowledge/ — durable pages

    knowledge/<org>/<CATEGORY>-<slug>.md

`org` is one of `entities`, `trajectories`, `experiences`, `events`, `tools`, `projects`.
`CATEGORY` is one of `KNOWLEDGE`, `VALUE`, `INTENT`, `METHOD`, `GOVERNANCE`. The file starts with
front matter (`schema: minir-knowledge/1`, `category`, `org`, `topic`, `epistemic`, `time`,
`revisions`, `id`, `objects`, `relations`, `summary`, `source_session`, `content_hash`) followed
by a `# topic` heading and the body.

Read it with `minir_kb_search` (ripgrep over the pages, filters `org` and `category`). Plain
`read_file` is fine for one page. The idle extraction pass is the only writer; do not edit a page
by hand without telling the user.

## user/ and agents/ — the cards

- `user/identity.md`, `user/preference.md`: written by the operator, shared by every agent in
  this home. `user/preference.md` is injected into the system prompt at session start (capped at
  4000 characters); `user/identity.md` is read by the layer worker as the user side.
- `agents/<profile>/identity.md`, `agents/<profile>/preference.md`: the agent's own cards, one
  set per profile. The card-review pass writes them and records each change in `card_revisions`.

Cards are markdown with optional front matter; the body is the text the prompts use. Read them
with `read_file`; `card_revisions` tells you what changed and when.

## run/ — process locks

`run/<pid>-<rand>.lock` is held by one live process. A lock whose owner is gone is treated as
stale on the next start, so leave the directory alone while a gateway or CLI process runs; never
delete a lock by hand to "unstick" a start.

## Rules

- Search conclusions with `minir_search`, pages with `minir_kb_search`, and the host's
  conversation with `minir_hermes_session_search`. SQL and file reads are for what those tools do
  not expose.
- Transcripts and page files are append-only history: do not rewrite, truncate or delete them.
- `memory.db` is owned by the plugin's store layer. Read-only connections only; a write can break
  a migration or a live WAL.
- Copy `memory.db`, `memory.db-wal` and `memory.db-shm` together when you need a consistent
  snapshot of a live store.
- No secret belongs in any of these files. If a key or token shows up, report its location and
  never copy the value into an answer.
