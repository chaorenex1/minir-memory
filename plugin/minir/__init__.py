"""Minir memory plugin — a minimal Hermes memory-provider template.

A complete, dependency-free reference implementation of the ``MemoryProvider``
contract (``agent/memory_provider.py``): explicit memories are conclusions in a per-profile
SQLite database, recall is a keyword search over them, and seven tools let the model search the
store, search the knowledge pages, remember, forget, keep a todo list, inspect the store, and run a
named custom agent.

**The conversation is an append-only event store, not a database table.** The layers read the host's
own ``state.db`` (plus an in-memory buffer for uncommitted turns) and persist what they *derived*
(``layer_state`` and the FTS-indexed ``conclusions``) plus the conclusions you asked it to keep.
The completed turns and
every layer run are also appended as JSONL events under
``$HERMES_HOME/minir-memory/sessions/<profile>/<layer>/``: one event per line, never rewritten or
deleted, and readable through ``TranscriptBuffer.read_events``. The host store remains the source of
truth, so a job queued before a restart can still be completed; inherited jobs are expired only
when the profile has no ``state.db``.

Fork this as a starting point:

* ``register(ctx)`` plus the lifecycle methods below are the contract surface.
* ``store.py`` is the storage layer — swap it for your backend (API client,
  vector index, knowledge graph) and keep this module's shape.
* ``turn_counter.py`` holds the base / dialectic-reasoning layer counters that turn
  ``on_turn_start`` ticks into queued jobs (a session + the time window to reason
  over); ``worker.py`` drains those jobs into merged per-layer artifacts that
  ``prefetch()`` injects. Delete the worker and the two layers become pure bookkeeping.
* ``scheduler.py`` owns every recurring decision: the turn-count task above and the two
  wall-clock tasks (card review, idle extraction), with one ledger and one log line per run.
* Delete optional methods you do not need instead of leaving empty overrides;
  the ABC already provides no-op defaults.

Activation (ONE external provider at a time, ``memory.provider`` in config.yaml)::

    memory:
      provider: minir

The plugin directory name must equal both the ``memory.provider`` value and the
``name`` property below (``minir``). See README.md for install/rename steps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from agent.memory_provider import (
    INDICATOR_GLYPH,
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)
from tools.registry import tool_error

from .adviser import adviser
from .adviser import adviser_command
from .agentrun import agent_history
from .agentrun import agent_watch
from .passes import card_review
from .prompting import content_valid
from .passes import extraction
from .agentrun import file_guard
from .adviser import fork as fork_dispatch
from .store import history as host_history
from .knowledge import kb_write
from .knowledge import knowledge
from .agentrun import minir_agents
from .runtime import scheduler
from .summary import session_summary
from .summary import window_summary
from .runtime import tools as plugin_tools
from .adviser.adviser_mode import AdviserMode
from .prompting.prompting import CONSISTENCY_TARGETS, marker_glossary
from .store.store import (CONCLUSION_KINDS, MinirStore, SessionScopeError, format_when,
                    is_session_bound)
# The same stored-text bound the store truncates at: a longer todo payload must be refused
# rather than silently cut into invalid JSON.
from .store.store import _MAX_CONTENT as _MAX_CONCLUSION_CHARS
from .store.transcript import HOST_LAYER, TranscriptBuffer, fingerprint
from .passes.turn_counter import (
    BASE_LAYER,
    DEFAULT_BASE_INTERVAL,
    DEFAULT_DIALECTIC_INTERVAL,
    DEFAULT_SESSION_MODE,
    DIALECTIC_LAYER,
    TRACKER,
    WINDOW_EPSILON,
    Trigger,
)
from .passes.layers import (
    CATEGORIES,
    EPISTEMIC,
    ORGS,
    RELATION_TYPES,
    artifact_from_columns,
)
from .passes.worker import LayerWorker
# Imported (not just defined) so the engine loader's subclass scan finds it on this module: the
# engine surface is resolved by ``issubclass(ContextEngine)`` against the package's attributes.
from .summary.context_engine import MinirContextEngine

# Module surface: a caller reaches a module as `minir.<module>`. The tests and the host patch
# series do, so every module is bound here by name even where its classes are imported above.
from .store import store, history, transcript, transcript_process, session_search
from .passes import worker, layers, turn_counter, cadence, extraction, card_review
from .summary import context_engine, compress_pass, session_summary, window_summary
from .prompting import prompting, rules, soul_rules, content_valid, transcript_prompt
from .adviser import (adviser, adviser_mode, adviser_guard, adviser_command, adviser_prompts, claims, fork,
                      fork_rules)
from .agentrun import agent_loop, agent_history, agent_watch, minir_agents, file_guard, rgtool
from .knowledge import knowledge, kb_write
from .runtime import tools, scheduler, accounting, host_bridge


#: ``MinirContextEngine`` is exported for the context-engine loader's subclass scan (and is
#: otherwise unused here, which is exactly why it is listed).
__all__ = ["MinirContextEngine", "MinirMemoryProvider", "register"]

logger = logging.getLogger(__name__)

_PROVIDER_LABEL = "Minir"
_STORE_DIRNAME = "minir-memory"   # under $HERMES_HOME
_DB_FILENAME = "memory.db"
_MAX_PREFERENCE_CHARS = 4_000     # system-prompt share of the operator's user preference card
_MAX_IDENTITY_CARD_CHARS = 1_200  # the user identity card is an orientation, not a dossier
_DEFAULT_PREFETCH_LIMIT = 5
_MAX_AI_SELF_CHARS = 400          # injected ai_self_representation cap
_MAX_WINDOW_CHARS = 2000          # injected window_summary cap; the goal ledger is protected
_MAX_MARKER_SNAPSHOT = 16_000     # session markers stored with the idle timer are capped here
_MAX_TOOL_EXCERPT = 600           # text excerpt returned by the minir_search tool
_MAX_AUTHOR_CACHE = 64            # per-session last-seen author, bounded like the turn counter
_COMPRESSION_GATE_TTL = 300.0     # seconds a crashed compression may pause periodic scheduling
_PRE_COMPRESS_TIMEOUT = 120.0     # synchronous base -> dialectic budget before compress()
_LAYER_DRAIN_SECONDS = 60.0       # background drain cadence when turns stop arriving
#: Wall-clock budget the ``pre_tool_call`` callback declares for the merge gate consult (90 s) and
#: the digest/parse around it. Host patch 0019 reads ``__hook_timeout__``; an unpatched host
#: ignores it and the callback keeps the global ``plugins.hook_callback_timeout`` (30 s default).
_GATE_CALLBACK_BUDGET = 120.0
# Final-answer injection. ``transform_llm_output_inject`` queues markdown for the answer the
# model is finishing; the host hook pops the whole session queue, so one turn cannot inherit the
# previous turn's block. Known types render in this declaration order, unknown types after them
# in first-queued order, and a stateful type keeps only its newest item because a second table
# in one answer is never useful.
INJECT_ORDER: tuple[str, ...] = ("todo",)
INJECT_STATEFUL = frozenset({"todo"})
_MAX_INJECT_ITEMS = 20
_MAX_INJECT_CHARS = 32 * 1024

# The host's todo item shape (``tools/todo_tool.py``): two levels. ``blocked`` is added here.
_TODO_STATUSES: tuple[str, ...] = ("pending", "in_progress", "blocked", "completed", "cancelled")
_TODO_OPEN_STATUSES = frozenset({"pending", "in_progress", "blocked"})
_TODO_PREFIX: dict[str, str] = {"pending": " ", "in_progress": "~", "blocked": "!",
                                "completed": "x", "cancelled": "-"}
_MAX_TODO_ITEMS = 256
_MAX_TODO_ID = 64
_MAX_TODO_CONTENT = 2000

#: A session is not "finished" until it has been quiet this long. The operator can resume it
#: inside the window; typing again cancels the pending extraction.
_DEFAULT_KNOWLEDGE_IDLE_HOURS = 24.0
_DEFAULT_SESSION_EXTRACTION = True

_ACTIVE_PROVIDER: Optional["MinirMemoryProvider"] = None
"""The provider instance ``initialize`` actually opened.

The host can construct this plugin twice (memory-provider load plus general plugin discovery)
while driving one instance. Plugin hooks close over the instance they were registered with; this
process-wide pointer lets them reach the live store/session when that is the other instance.
"""

_PROCESS_STARTED_AT = time.time()
"""When this process began. Without a host ``state.db`` a job queued before it can never be
drained: its window's messages lived in the memory of a process that is gone (see
``initialize``)."""

_STALE_CLAIM_SECONDS = 600.0
"""Age at which a ``running`` job is assumed to belong to a dead process.

Cannot key solely on this process's start time: a gateway and a CLI can share one
``$HERMES_HOME``, and a live gateway claim predates the later CLI process. Ten minutes is far
beyond any single layer job's model budget (observed well under a minute), so a fresh claim is
never stolen."""

_BOOL_WORDS = {**dict.fromkeys(("1", "true", "yes", "on"), True),
               **dict.fromkeys(("0", "false", "no", "off"), False)}


def _coerce_bool(value: Any, default: bool) -> bool:
    """bool/number -> truthiness; common true/false words parsed; anything else -> default."""
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return _BOOL_WORDS.get(value.strip().lower(), default) if isinstance(value, str) else default


def _load_config() -> dict[str, Any]:
    """``memory.minir`` block from config.yaml ({} on any error — never raise at import/load time)."""
    try:
        from hermes_cli.config import load_config
        block = (load_config().get("memory") or {}).get("minir")
    except Exception:
        block = None
    return dict(block) if isinstance(block, dict) else {}


def _excerpt(text: str) -> str:
    """Flatten and bound text for tool responses (see ``_MAX_TOOL_EXCERPT``)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _MAX_TOOL_EXCERPT else flat[:_MAX_TOOL_EXCERPT] + "…"



def _todo_line(item: dict[str, Any], *, indent: int) -> str:
    """One prompt line: ``[x] id content`` with the state as a one-character prefix."""
    status = str(item.get("status") or "pending")
    suffix = f"  ({status})" if status in ("in_progress", "blocked") else ""
    return (f"{' ' * indent}[{_TODO_PREFIX.get(status, ' ')}] "
            f"{item.get('id')} {item.get('content')}{suffix}")


def _todo_row(number: str, item: dict[str, Any]) -> str:
    """One markdown table row; a pipe in the text is escaped so the table stays a table."""
    content = " ".join(str(item.get("content") or "").split()).replace("|", chr(92) + "|")
    return (f"| {number} | {content} | {item.get('status') or 'pending'} | "
            f"{item.get('parent') or ''} |")


def _redact_text(text: str) -> tuple[str, bool]:
    """Scrub un-redacted channels (system prompt, reasoning) and say whether a redactor ran.

    Host ``messages`` are already put through ``_redact_for_provider``; ``pre_llm_call`` and
    ``on_stream_delta`` are not. Without a host redactor the text is recorded as-is with
    ``redacted: false`` — the file is permanent, so the flag is the honest part.
    """
    try:
        from agent.monitoring.redaction import redact_for_export
    except ImportError:
        return text, False
    try:
        return redact_for_export(text) or "", True
    except Exception:
        logger.debug("minir: host redactor failed", exc_info=True)
        return "[redaction unavailable]", True


def _env_int(name: str, default: int) -> int:
    """Integer env override; anything unparsable falls back to *default*."""
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    """String env override; empty/unset falls back to *default*."""
    return str(os.environ.get(name, "")).strip() or default


def _as_interval(value: Any, default: int) -> int:
    """Config value -> turn interval >= 1 (anything unparsable falls back to *default*)."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _as_positive_int(value: Any, default: int = 1) -> int:
    """Counted value -> integer >= 1 (a missing host counter still counts as one request)."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _endpoint(value: Any) -> tuple[str, str]:
    """``"kind:key"`` -> (kind, key); a key may itself contain colons."""
    text = str(value or "").strip()
    kind, sep, key = text.partition(":")
    if not sep or not kind.strip() or not key.strip():
        raise ValueError("endpoint must be 'kind:key'")
    return kind.strip(), key.strip()


class MinirMemoryProvider(MemoryProvider):
    """Local-first memory: conclusions in SQLite, FTS recall, no transcripts."""

    def __init__(self, config: Optional[dict[str, Any]] = None):
        self._config = dict(config) if config is not None else _load_config()
        try:
            default_limit = int(self._config.get("prefetch_limit") or _DEFAULT_PREFETCH_LIMIT)
        except (TypeError, ValueError):
            default_limit = _DEFAULT_PREFETCH_LIMIT
        self._prefetch_limit = max(1, min(20, _env_int("MINIR_PREFETCH_LIMIT", default_limit)))

        # Layer cadence: how many turns each layer accumulates before it reasons over the
        # history it has not seen yet (see turn_counter.py).
        base_cadence = self._config.get(
            "context_cadence", self._config.get("contextCadence", self._config.get("base_interval")))
        dialectic_cadence = self._config.get(
            "dialectic_cadence",
            self._config.get("dialecticCadence", self._config.get("dialectic_interval")))
        self._base_interval = max(1, _env_int(
            "MINIR_CONTEXT_CADENCE",
            _env_int("MINIR_BASE_INTERVAL", _as_interval(base_cadence, DEFAULT_BASE_INTERVAL))))
        self._dialectic_interval = max(1, _env_int(
            "MINIR_DIALECTIC_CADENCE",
            _env_int("MINIR_DIALECTIC_INTERVAL",
                     _as_interval(dialectic_cadence, DEFAULT_DIALECTIC_INTERVAL))))
        self._session_mode = _env_str("MINIR_SESSION_MODE", str(
            self._config.get("session_mode") or DEFAULT_SESSION_MODE))
        try:
            depth_value = self._config.get(
                "dialectic_depth", self._config.get("dialecticDepth", 2))
            depth = int(_env_str("MINIR_DIALECTIC_DEPTH", str(depth_value or 2)))
        except (TypeError, ValueError):
            depth = 2
        self._dialectic_depth = max(1, min(3, depth))
        # File-layer budgets (cards and knowledge are per file, never pooled) and the wall-clock
        # period of the global card review.
        self._card_token_budget = max(200, _env_int(
            "MINIR_CARD_TOKEN_BUDGET", int(self._config.get("card_token_budget") or 2000)))
        self._card_review_days = max(1, _env_int(
            "MINIR_CARD_REVIEW_DAYS", int(self._config.get("card_review_days") or 3)))
        self._knowledge_token_budget = max(200, _env_int(
            "MINIR_KNOWLEDGE_TOKEN_BUDGET",
            int(self._config.get("knowledge_token_budget") or 2000)))
        # Idle finalisation: at session end the value markers are captured and a timer armed.
        # A user turn inside the window cancels it; when it fires, the session summary and the
        # knowledge pages are extracted.
        self._session_extraction = _coerce_bool(
            os.environ.get("MINIR_SESSION_EXTRACTION"),
            _coerce_bool(self._config.get("session_extraction_enabled"),
                         _DEFAULT_SESSION_EXTRACTION))
        try:
            idle_hours = float(os.environ.get(
                "MINIR_KNOWLEDGE_IDLE_HOURS",
                self._config.get("knowledge_idle_hours", _DEFAULT_KNOWLEDGE_IDLE_HOURS)))
        except (TypeError, ValueError):
            idle_hours = _DEFAULT_KNOWLEDGE_IDLE_HOURS
        self._knowledge_idle_seconds = max(0.0, float(idle_hours)) * 3600.0
        # Layer reasoning: the worker calls the host's auxiliary model to turn each queued job
        # into a layer artifact. On by default (the layers are the point of the provider);
        # ``reasoning_enabled: false`` keeps minir a pure store with no model calls at all.
        self._reasoning = _coerce_bool(
            os.environ.get("MINIR_REASONING_ENABLED"),
            _coerce_bool(self._config.get("reasoning_enabled"), True))

        self._store: Optional[MinirStore] = None
        self._session_id = ""
        # The host's surface (cli, telegram, ...): it scopes host session search only. Transcript
        # streams are partitioned by profile, not by this.
        self._platform = "cli"
        # Cache budget: streams, events per stream, total bytes. ``_records`` used to grow with
        # the session count; the cache is a read source now, so all three are hard bounds.
        self._buffer_streams = _as_positive_int(
            os.environ.get("MINIR_BUFFER_STREAMS") or self._config.get("buffer_streams"), 20)
        self._buffer_events = _as_positive_int(
            os.environ.get("MINIR_BUFFER_EVENTS")
            or self._config.get("buffer_events_per_stream"), 4000)
        self._buffer_bytes = _as_positive_int(
            os.environ.get("MINIR_BUFFER_BYTES") or self._config.get("buffer_bytes"),
            256 * 1024 * 1024)
        self._agent_context = "primary"
        self._agent_identity = ""
        self._hermes_home = ""
        self._state_db = ""
        self._profile = "default"
        #: Content language for every generated artifact (cards, pages, knowledge writes).
        self._language = str(self._config.get("content_language") or "en")
        self._identity = ""
        self._preference = ""
        self._worker: Optional[LayerWorker] = None
        self._card_scheduler: Optional[card_review.CardReviewScheduler] = None
        self._extraction_scheduler: Optional[extraction.ExtractionScheduler] = None
        self._knowledge: Optional[knowledge.KnowledgePipeline] = None
        self._transcript = TranscriptBuffer(
            profile=self._profile,
            max_sessions=self._buffer_streams,
            buffer_events_per_stream=self._buffer_events,
            buffer_bytes=self._buffer_bytes,
            persist_dir=(os.path.join(self._hermes_home, "minir-memory", "sessions")
                         if str(self._hermes_home or "").strip() else None))
        #: Reasoning deltas for the live turn, aggregated into one row at ``sync_turn``.
        self._reasoning_deltas: dict[str, list[str]] = {}
        self._authors: dict[str, dict[str, Any]] = {}   # session -> last author seen on a turn
        self._lock = threading.Lock()              # guards the inject queue
        #: session_id -> [{"type", "content", "seq"}]; popped whole when the answer is rendered.
        self._injects: dict[str, list[dict[str, Any]]] = {}
        self._inject_seq = 0
        self._last_recall: Optional[RecallStatus] = None
        self._metrics: dict[str, dict[str, Any]] = {}
        self._metrics_seen = 0
        # Per-turn side channel: event ring + reviewer worker + llm_request delivery. Constructed
        # before initialize (the host may register hooks first); every path guards on the store.
        self._adviser_mode = AdviserMode(self)
        # ``/adviser``'s mode switch. It lives on the provider, not in a module-level singleton:
        # the plugin directory is imported once per surface (memory provider, context engine), and
        # a registry each import owned could be armed by one instance while the hook that injects
        # the directive reads the other. Both seams resolve the live provider instead.
        self._modes = adviser_command.ModeSwitch()
        # The fork dispatcher sits beside the adviser, not inside it: it answers the cheap
        # question (is there a rule for this branch?) and hands only the expensive one over.
        self._fork = fork_dispatch.ForkDispatcher(self)
        # One clock for every recurring decision: turn tasks tick on the host's turn thread,
        # time tasks (card review, idle extraction) run on this single daemon.
        self._scheduler = scheduler.Scheduler(
            record=lambda *args: self._store.record_scheduler_run(*args)
            if self._store is not None else 0)

    # -- contract -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "minir"

    def is_available(self) -> bool:
        """No credentials or external binaries required — always ready."""
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Open the store. ``hermes_home`` is profile-scoped and must be used as given.

        The host's ``user_id`` kwarg is accepted and ignored: this plugin has no multi-user
        dimension, so one profile serves one user and the profile name keys every layer row.
        """
        hermes_home = kwargs.get("hermes_home") or self._fallback_home()
        self._hermes_home = str(hermes_home)
        self._state_db = str(Path(hermes_home) / "state.db")
        db_path = Path(hermes_home) / _STORE_DIRNAME / _DB_FILENAME
        self._store = MinirStore(db_path)
        global _ACTIVE_PROVIDER
        _ACTIVE_PROVIDER = self
        self._session_id = session_id or ""
        # The host passes its surface on initialize; a bare caller gets ``cli``.
        self._platform = str(kwargs.get("platform") or self._platform or "cli")
        self._adviser_mode.session_started(self._session_id)
        # Rules are read once per session and again after every review commit; the files are the
        # authority, this is the cache the hot path reads. Loading here (not in the first hook)
        # keeps a pre_tool_call off the disk.
        self._fork.load()
        if (self._fork.mode() == "on" or self._fork.gate_mode() != "off") and not \
                self._adviser_mode.enabled(self._session_id):
            logger.warning(
                "Minir forks: %s with the adviser disabled — the static rules still act, but "
                "nothing is learned, no gate can answer and no consult is possible",
                self._fork.mode())
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        # ``user_id`` is part of the host's kwargs and is ignored on purpose: one profile is
        # one user, so the profile name is the subject key for every layer.
        self._agent_identity = str(kwargs.get("agent_identity") or "")
        self._profile = (host_history.profile_name(self._state_db, self._session_id)
                         or str(kwargs.get("profile") or kwargs.get("profile_name") or "default"))
        (identity_meta, self._identity), (preference_meta, self._preference) = (
            host_history.load_identity(self._hermes_home, self._profile))
        self._identity_source = str(identity_meta.get("source") or "")
        self._preference_source = str(preference_meta.get("source") or "")
        # The transcript streams are profile-scoped: the profile is the partition key for every
        # stream and every agent run. ``bind`` pins this session and, when this is the first
        # process after a clean shutdown, replays the last cache range of each stream.
        self._transcript = TranscriptBuffer(
            profile=self._profile,
            max_sessions=self._buffer_streams,
            buffer_events_per_stream=self._buffer_events,
            buffer_bytes=self._buffer_bytes,
            persist_dir=str(Path(self._hermes_home) / _STORE_DIRNAME / "sessions"),
            run_dir=str(Path(self._hermes_home) / _STORE_DIRNAME / "run"))
        self._transcript.bind(self._session_id, profile=self._profile)
        self._knowledge = knowledge.KnowledgePipeline(
            self._hermes_home,
            language=self._language,
            budget=self._knowledge_token_budget, autostart=self._reasoning,
            on_written=self._index_page_edges)
        self._worker = LayerWorker(
            self._store, self._transcript, enabled=self._reasoning,
            agent_identity=self._agent_identity,
            state_db=self._state_db, profile=self._profile,
            identity=self._identity, preference=self._preference,
            identity_source=self._identity_source,
            preference_source=self._preference_source,
            depth=self._dialectic_depth,
            content_language=self._language,
            hermes_home=self._hermes_home,
            on_value_markers=(self._knowledge.enqueue_from_refresh
                      if self._knowledge is not None else None))
        self._card_scheduler = card_review.CardReviewScheduler(
            self._store, self._run_card_review, subject=self._worker.user_subject(),
            period=float(self._card_review_days) * 24 * 3600,
            files_present=lambda: all(
                path.is_file() for path in card_review.card_paths(
                    self._hermes_home, self._profile).values()))
        # The idle timer must survive a process: rows are in SQLite, so a session that ended in
        # a previous process is picked up on the first tick here.
        self._extraction_scheduler = extraction.ExtractionScheduler(
            self._store, self._run_session_extraction)
        # Everything recurring is registered on one scheduler. A disabled task is simply not
        # registered; the scheduler inventories what it owns when it starts.
        self._scheduler.add_turn("layers", self._run_turn_layers)
        # A turn is the usual kick, but a session can sit idle with windows already queued (a
        # long tool call, a conversation resumed in another client): the queue would then wait
        # for a turn that never comes. The tick only starts a drain; a live one makes it a no-op.
        self._scheduler.add_time("layer_drain", _LAYER_DRAIN_SECONDS, self._drain_layers_tick,
                                 due=self._has_queued_layer_jobs, first_delay=45.0)
        if self._reasoning:
            self._card_scheduler.ensure_anchor()
            self._scheduler.add_time("card_review", card_review.TICK_SECONDS,
                                     self._card_scheduler.tick,
                                     due=self._card_scheduler.due)
        if self._reasoning and self._session_extraction:
            self._scheduler.add_time("session_extraction", extraction.TICK_SECONDS,
                                     self._extraction_scheduler.tick, first_delay=0.0)
        self._scheduler.add_time("conclusion_history_maintenance", 6 * 3600,
                                 self._maintain_conclusion_history, first_delay=60.0)
        self._scheduler.start()
        # Configure before opening the session: intervals/mode apply to sessions opened later
        # (an already-running session keeps the cadence it started with).
        TRACKER.configure(base_interval=self._base_interval,
                          dialectic_interval=self._dialectic_interval,
                          default_mode=self._session_mode)
        TRACKER.begin(self._session_id, mode=self._session_mode)
        # A job left *pending* by a previous process is still readable from host history, so it
        # is kept and drained normally. A job left *running* is not: its owner thread died with
        # that process. Re-queue those (retry, or fail once attempts are spent) instead of
        # leaving status='running' forever. Without a state.db the window's content is gone, so
        # expire both kinds there.
        if Path(self._state_db).exists():
            # Age, not this process's start: another live process (the gateway) can hold a
            # claim older than this CLI. Only a claim past the job budget is certainly dead.
            stale_before = time.time() - _STALE_CLAIM_SECONDS
            if self._worker.reclaim_orphans(before=stale_before):
                logger.info("Minir reclaimed layer jobs left running by a dead process")
        elif self._worker.expire_orphans(before=_PROCESS_STARTED_AT):
            logger.info("Minir expired layer jobs left open by a previous process")
        # The plugin hook only runs when the host rebuilds the system prompt; a cached-prompt
        # session would never enqueue its initial dialectic pass. Do it here too; has_open_job
        # makes the hook's later attempt a no-op.
        self.cold_start(self._session_id)
        self._record_system_prompt()
        # No other drain here on purpose: a drain started now would race the first turn and
        # could claim a job that was queued before the turn's messages existed, burning the
        # window as empty. Jobs are drained where content exists — sync_turn, a boundary, shutdown.
        logger.debug("Minir store ready at %s (profile=%s)", db_path, self._profile)

    @staticmethod
    def _fallback_home() -> str:
        """``initialize`` always passes ``hermes_home``; this is only a safety net."""
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Setup fields for ``hermes memory setup``.

        Every field carries ``env_var``, so this plugin follows the ABC's
        env-var-only rule and does not need to override ``save_config()``.
        """
        return [
            {"key": "prefetch_limit", "description": "Maximum memory items injected per turn",
             "type": "integer", "default": str(_DEFAULT_PREFETCH_LIMIT),
             "minimum": 1, "maximum": 20, "env_var": "MINIR_PREFETCH_LIMIT"},
            {"key": "block_builtin_memory",
             "description": "Stop the host loading its own MEMORY.md and USER.md (needs host patch 0020)",
             "type": "boolean", "default": "true", "choices": ["true", "false"],
             "env_var": "MINIR_BLOCK_BUILTIN_MEMORY"},
            {"key": "context_cadence",
             "description": "Turns the base layer accumulates before it reasons",
             "type": "integer", "default": str(DEFAULT_BASE_INTERVAL),
             "minimum": 1, "env_var": "MINIR_CONTEXT_CADENCE"},
            {"key": "dialectic_cadence",
             "description": "Periodic dialectic counter; adversarial reviews still follow each base refresh",
             "type": "integer", "default": str(DEFAULT_DIALECTIC_INTERVAL),
             "minimum": 1, "env_var": "MINIR_DIALECTIC_CADENCE"},
            {"key": "dialectic_depth",
             "description": "Dialectic passes per refresh: 1 synthesize, 2 self-audit, 3 reconcile",
             "type": "integer", "default": "2", "minimum": 1, "maximum": 3,
             "env_var": "MINIR_DIALECTIC_DEPTH"},
            {"key": "reasoning_enabled",
             "description": "Run the layer reasoning passes (uses the auxiliary model; off = store only)",
             "type": "boolean", "default": "true", "choices": ["true", "false"],
             "env_var": "MINIR_REASONING_ENABLED"},
            {"key": "content_language",
             "description": "Language for card and knowledge prose (field names stay English)",
             "type": "text", "default": "en", "env_var": "MINIR_CONTENT_LANGUAGE"},
            {"key": "buffer_streams",
             "description": "Transcript streams kept in memory (the current session is pinned)",
             "type": "integer", "default": "20", "minimum": 1,
             "env_var": "MINIR_BUFFER_STREAMS"},
            {"key": "buffer_events_per_stream",
             "description": "Events kept per transcript stream in memory (the file keeps all)",
             "type": "integer", "default": "4000", "minimum": 1,
             "env_var": "MINIR_BUFFER_EVENTS"},
            {"key": "buffer_bytes",
             "description": "Total transcript cache budget in bytes (oldest streams evict first)",
             "type": "integer", "default": "268435456", "minimum": 1024,
             "env_var": "MINIR_BUFFER_BYTES"},
            {"key": "card_token_budget",
             "description": "Hard per-file budget for the four global card files",
             "type": "integer", "default": "2000", "minimum": 200,
             "env_var": "MINIR_CARD_TOKEN_BUDGET"},
            {"key": "card_review_days",
             "description": "Days between global card reviews",
             "type": "integer", "default": "3", "minimum": 1,
             "env_var": "MINIR_CARD_REVIEW_DAYS"},
            {"key": "knowledge_token_budget",
             "description": "Hard per-file budget for one knowledge page",
             "type": "integer", "default": "2000", "minimum": 200,
             "env_var": "MINIR_KNOWLEDGE_TOKEN_BUDGET"},
            {"key": "session_extraction_enabled",
             "description": "Summarize a session and extract its knowledge after it goes idle",
             "type": "boolean", "default": "true", "choices": ["true", "false"],
             "env_var": "MINIR_SESSION_EXTRACTION"},
            {"key": "knowledge_idle_hours",
             "description": "Quiet hours after session end before the final summary and knowledge extraction",
             "type": "number", "default": "24", "minimum": 0,
             "env_var": "MINIR_KNOWLEDGE_IDLE_HOURS"},
            {"key": "session_mode", "description": "Mode stamped on each newly opened session",
             "type": "text", "default": DEFAULT_SESSION_MODE, "env_var": "MINIR_SESSION_MODE"},
            # -- fork dispatch (adviser_mode.forks) -------------------------------------------
            # Off by default: with every key at its default the provider behaves exactly as it
            # did before this layer existed.
            {"key": "forks",
             "description": "Fork dispatch: off, shadow (classify and record only) or on",
             "type": "text", "default": "off", "choices": ["off", "shadow", "on"],
             "env_var": "MINIR_FORKS"},
            {"key": "fork_rules_max",
             "description": "Cap on learned fork rules; the least recently hit are dropped",
             "type": "integer", "default": "500", "minimum": 1,
             "env_var": "MINIR_FORK_RULES_MAX"},
            {"key": "fork_compress_chars",
             "description": "Result size above which a compress rule may distill the output",
             "type": "integer", "default": "4000", "minimum": 400,
             "env_var": "MINIR_FORK_COMPRESS_CHARS"},
            {"key": "fork_retry_limit",
             "description": "Identical failures after which the retry guard refuses the call",
             "type": "integer", "default": "2", "minimum": 1,
             "env_var": "MINIR_FORK_RETRY_LIMIT"},
            {"key": "fork_ttl_days",
             "description": "Days a learned fork rule lives before the review expires it",
             "type": "integer", "default": "14", "minimum": 0,
             "env_var": "MINIR_FORK_TTL_DAYS"},
            {"key": "gates",
             "description": "Integration gate: off, shadow (judge and record, never block) or "
                            "merge (block on no-go; needs host patch 0019)",
             "type": "text", "default": "off", "choices": ["off", "shadow", "merge"],
             "env_var": "MINIR_GATES"},
            {"key": "gate_timeout_s",
             "description": "Adviser budget for one gate consult (the callback declares 120 s)",
             "type": "integer", "default": "90", "minimum": 1, "maximum": 120,
             "env_var": "MINIR_GATE_TIMEOUT_S"},
            {"key": "advise_tool",
             "description": "Expose minir_advise, the pull consult for plan/merge/ship",
             "type": "boolean", "default": "true", "choices": ["true", "false"],
             "env_var": "MINIR_ADVISE_TOOL"},
        ]

    # -- prompt + layer state -------------------------------------------------

    def system_prompt_block(self) -> str:
        """Session-static prompt text: the memory contract and the two user cards.

        Craft rules and the adviser contract do not ride this block: the craft routing lives in
        the skills the host loads by name, and the adviser-side vocabulary lives in that agent's
        own rules and review prompt.
        """
        logger.info("Minir system_prompt_block (built once per session)")
        return "\n\n".join(part for part in (
            self._memory_prompt_block(), self._cards_prompt_block()) if part)

    def _cards_prompt_block(self) -> str:
        """The two global user cards: identity and standing preferences.

        Both live under ``minir-memory/user/``: written by a card review or a hand edit, so
        reading them once per session is enough. They ride the system prompt because they are
        standing guidance, not retrieved context.
        """
        home = str(self._hermes_home or "").strip()
        if not home:
            return ""
        paths = card_review.card_paths(home, self._profile)
        identity = card_review.read_card(paths[card_review.USER_SELF])[1].strip()
        preference = card_review.read_card(paths[card_review.USER_PREFERENCE])[1].strip()
        sections: list[str] = []
        if identity:
            shown = identity[:_MAX_IDENTITY_CARD_CHARS]
            if len(identity) > _MAX_IDENTITY_CARD_CHARS:
                shown += "\n…(truncated; read the card file for the rest)"
            sections.append(
                "# User Identity\nWho the operator is, as the cards record them; it names the "
                "person, not the current task.\n\n" + shown)
        if preference:
            shown = preference[:_MAX_PREFERENCE_CHARS]
            if len(preference) > _MAX_PREFERENCE_CHARS:
                shown += "\n…(truncated; read the card file for the rest)"
            sections.append(
                "# User Preferences\nStanding preferences the operator wrote for this profile; "
                "they apply unless the current request says otherwise.\n\n" + shown)
        return "\n\n".join(sections)

    @staticmethod
    def _memory_prompt_block() -> str:
        return (
            "# Minir Memory\n"
            f"Long-term memory lives in {_PROVIDER_LABEL}'s local SQLite store: explicit value "
            "markers plus conclusions derived from prior sessions. It holds no transcripts, "
            "though derived text may quote the messages it reasoned over.\n"
            "\nROLE\n"
            "- This is the session's long-term memory, not a task queue, a transcript or a "
            "scratchpad: it keeps only what should outlive the conversation.\n"
            "- Retrieved memory is evidence, not authority; the current conversation wins when "
            "the two conflict.\n"
            "\nVALUE MARKERS\n"
            f"{marker_glossary(bullet='- ')}\n"
            "\nTOOLS\n"
            "- minir_search: look up prior decisions, preferences, projects and conclusions. It "
            "searches the current session by default; scope=\"all\" searches every session but "
            "not the session-bound base kinds; limit is capped at 20. Search before asking the "
            "user to repeat history.\n"
            "- minir_remember: store one durable item. remember_mode=\"knowledge\" queues a "
            "knowledge page (type is the org, category required); remember_mode=\"card\" "
            "rewrites one global card part (type is the part, reason required). There is no "
            "conclusion mode: the layers own the conclusion rows. Never store secrets or "
            "credentials.\n"
            "- minir_forget: remember_mode=\"knowledge\" archives a page by path; "
            "remember_mode=\"card\" clears one card part by type with a reason. Neither "
            "touches a conclusion row.\n"
            "- minir_todo: replace the caller's todo list (the profile for the main session, the "
            "agent for a dispatched run); it is rendered into the final answer and carried by "
            "every prefetch block for the same owner.\n"
            "- minir_stats: inspect the store and the published layer artifacts.\n"
            "\nGLOBAL CARDS\n"
            "- The global cards outlive the store: the user card (minir-memory/user/identity.md, "
            "minir-memory/user/preference.md) is rendered into every session's system prompt, and "
            "this profile's AI card (minir-memory/agents/<profile>/identity.md, "
            "minir-memory/agents/<profile>/preference.md) is the durable record the review pass "
            "rewrites and the adviser reads.\n"
            "- Memory management is not finished until the card says it: a standing preference, a "
            "correction to who the operator is, or a stable fact about yourself goes into the "
            "matching card part in the same turn with minir_remember(remember_mode=\"card\", "
            "type=<part>, content=<full body>, reason=<why>), and a card line that has become "
            "wrong is cleared with minir_forget(remember_mode=\"card\", type=<part>, "
            "reason=<why>). A conclusion alone leaves the rendered card stale.\n"
            "- Card content is that part's whole new body, not a patch: the other parts stay as "
            "they are, and the write is global, not scoped to this session.\n"
            "\nWORKFLOW\n"
            "1. Search before asking the user to repeat history.\n"
            "2. Answer from the current conversation; reach for memory only to fill a gap or to "
            "avoid re-litigating a settled decision.\n"
            "3. When a durable fact, correction or lesson appears, store it once under the type "
            "that matches its definition.\n"
            "4. When a stored page or card line is superseded or wrong, rewrite it rather "
            "than storing a contradiction."
        )

    def prefetch(self, query: str = "", *, session_id: str = "") -> str:
        """The live layer state and the todo list; nothing else rides the turn.

        ``ai_self_representation`` and ``window_summary`` come from this session's live
        ``base_refreshes`` row (one indexed read, no model call). Explicit memory and knowledge
        pages stay reachable through ``minir_search`` and ``minir_kb_search``; another session's
        rows require an explicit ``scope="all"`` search. ``query`` stays in the signature because
        the host passes it; injection no longer depends on it.
        """
        session = session_id or self._session_id
        state, state_items = self._layer_block(session)
        stale, stale_items = self._stale_block(session)
        todo, todo_items = self._todo_block()
        block = "\n\n".join(part for part in (state, stale, todo) if part)
        count = state_items + stale_items + todo_items if block else 0
        self._last_recall = (RecallStatus(provider_label=_PROVIDER_LABEL, count=count,
                                          glyph=INDICATOR_GLYPH) if block else None)
        return block

    def _index_page_edges(self, rel_path: str) -> None:
        """A written page owns its edges: derive them from the file and upsert them."""
        if self._store is None or not str(rel_path or "").strip():
            return
        meta = knowledge.read_front_matter(self._knowledge.root / rel_path)
        self._store.upsert_page_edges(knowledge.page_edges(rel_path, meta))

    def _maintain_conclusion_history(self) -> dict[str, int]:
        """Fold, archive, clean and look for merge candidates; runs on the one scheduler."""
        archive = Path(self._hermes_home) / _STORE_DIRNAME / "conclusion" / "archive"
        counts = dict(self._store.maintain_conclusion_history(archive))
        try:
            merge = self._store.generate_merge_candidates(
                page_topics=knowledge.page_topics(self._knowledge.root))
            counts["merged"] = int(merge.get("merged") or 0)
            counts["advised"] = int(merge.get("advised") or 0)
        except Exception as exc:
            logger.warning("Minir merge candidates failed: %s", exc)
        return counts

    def _stale_block(self, session: str) -> tuple[str, int]:
        """Open base-refresh failures, so a silent gap never reads as 'nothing happened'."""
        if self._store is None or self._worker is None:
            return "", 0
        try:
            rows = self._store.open_layer_failures(
                "base", self._worker.user_subject(), limit=3)
        except Exception as exc:
            logger.debug("Minir layer failure read failed: %s", exc)
            return "", 0
        if not rows:
            return "", 0
        lines = ["## Minir memory gaps",
                 "These windows were never written to memory. Absence of a memory is not "
                 "evidence that the work did not happen."]
        for row in rows:
            start = time.strftime("%m-%d %H:%M", time.localtime(
                float(row.get("window_from_ts") or 0)))
            end = time.strftime("%m-%d %H:%M", time.localtime(
                float(row.get("window_to_ts") or 0)))
            error = " ".join(str(row.get("error") or "").split())[:160]
            lines.append("- %s → %s: base refresh failed (%s)" % (start, end, error))
        return "\n".join(lines), len(lines) - 2

    def _layer_block(self, session: str) -> tuple[str, int]:
        """The live base row's two columns, capped; counts bullet lines.

        The AI card is a short orientation card. The window summary is the task ledger: its
        goal sections (Objective, Current State) are kept whole and only the other six are
        truncated, marked ``[summary truncated]``.
        """
        if self._store is None or not session:
            return "", 0
        try:
            row = self._store.latest_base_refresh(session)
            if not row:
                return "", 0
            columns = artifact_from_columns(row)
            ai_self = str(columns.get("ai_self_representation") or "").strip()
            if len(ai_self) > _MAX_AI_SELF_CHARS:
                ai_self = ai_self[:_MAX_AI_SELF_CHARS]
            window = window_summary.for_prompt(
                str(columns.get("window_summary") or "").strip(), _MAX_WINDOW_CHARS)
            sections = []
            if ai_self:
                sections.append(f"### AI self representation\n{ai_self}")
            if window:
                sections.append(f"### Window summary\n{window}")
            if not sections:
                return "", 0
            block = f"## {_PROVIDER_LABEL} layer state\n" + "\n\n".join(sections)
            bullets = sum(1 for line in block.splitlines() if line.lstrip().startswith("-"))
            return block, bullets
        except Exception as exc:  # injection must never break a turn
            logger.debug("Minir layer state read failed: %s", exc)
            return "", 0

    # -- todo list -------------------------------------------------------------

    def _todo_owner(self) -> tuple[str, str]:
        """Who owns the todo list right now: ``(name, session)``.

        The main session's name is its profile. A dispatched run binds its own layer and run id
        (``agent_loop.layer_context``), so its list is keyed by the agent's name and the run's own
        session id: the provider serving the call belongs to the session that dispatched the run,
        and keying on that session alone would overwrite the parent's list.
        """
        name = agent_history.current_layer().strip() or str(self._profile or "").strip()
        session = agent_history.current_run().strip() or str(self._session_id or "").strip()
        return name, session

    @staticmethod
    def _todo_key(name: str, session: str) -> str:
        """The one row per owner that holds the whole list."""
        return f"todo:{name or 'unknown'}:{session}"

    def _todo_items(self, owner: tuple[str, str]) -> list[dict[str, Any]]:
        """The stored list for one owner; ``[]`` when it has none or the row is unreadable."""
        name, session = owner
        if self._store is None or not session:
            return []
        try:
            row = self._store.get_conclusion("todo", self._todo_key(name, session))
            if not row:
                return []
            payload = json.loads(str(row.get("content") or ""))
        except (TypeError, ValueError):
            return []
        items = payload.get("todos") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _todo_lines(items: list[dict[str, Any]]) -> list[str]:
        """Two-level render for prompt injection: one prefix per state, children indented."""
        open_count = sum(1 for item in items
                         if str(item.get("status") or "pending") in _TODO_OPEN_STATUSES)
        lines = [f"[Todos] {open_count} open / {len(items)} total"]
        for item in items:
            if item.get("parent"):
                continue
            lines.append(_todo_line(item, indent=2))
            for child in items:
                if child.get("parent") == item.get("id"):
                    lines.append(_todo_line(child, indent=4))
        return lines

    def _todo_block(self) -> tuple[str, int]:
        """The todo list as a prefetch block; never routed through query recall."""
        items = self._todo_items(self._todo_owner())
        if not items:
            return "", 0
        return "\n".join(self._todo_lines(items)), len(items)

    @staticmethod
    def _todo_table(items: list[dict[str, Any]]) -> str:
        """Markdown table for the final answer plus the one-line status summary."""
        rows = ["| # | 任务 | 状态 | 父项 |", "| --- | --- | --- | --- |"]
        for index, item in enumerate((item for item in items if not item.get("parent")), start=1):
            rows.append(_todo_row(str(index), item))
            children = [child for child in items if child.get("parent") == item.get("id")]
            for child_index, child in enumerate(children, start=1):
                rows.append(_todo_row(f"{index}.{child_index}", child))
        counts = {status: sum(1 for item in items
                              if str(item.get("status") or "pending") == status)
                  for status in _TODO_STATUSES}
        summary = (f"Todo: {counts['completed']}/{len(items)} done, "
                   f"{counts['in_progress']} in progress, {counts['pending']} pending.")
        if counts["blocked"]:
            summary += f" {counts['blocked']} blocked."
        if counts["cancelled"]:
            summary += f" {counts['cancelled']} cancelled."
        return "\n".join(rows) + "\n\n" + summary

    def _validate_todos(self, raw: list[Any]) -> tuple[list[dict[str, Any]], str]:
        """Validate the host-shaped list; returns ``(items, error)`` with one of them empty."""
        if len(raw) > _MAX_TODO_ITEMS:
            return [], f"todo_too_large: at most {_MAX_TODO_ITEMS} items"
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                return [], "todos must be objects with id and content"
            item_id = str(entry.get("id") or "").strip()
            if not item_id or len(item_id) > _MAX_TODO_ID:
                return [], f"todo id must be 1..{_MAX_TODO_ID} characters"
            if item_id in seen:
                return [], f"duplicate todo id: {item_id}"
            seen.add(item_id)
            content = str(entry.get("content") or "").strip()
            if not content or len(content) > _MAX_TODO_CONTENT:
                return [], (f"todo {item_id}: content must be 1..{_MAX_TODO_CONTENT} "
                            "characters")
            status = str(entry.get("status") or "pending").strip()
            if status not in _TODO_STATUSES:
                return [], (f"todo {item_id}: status must be one of: "
                            f"{', '.join(_TODO_STATUSES)}")
            item: dict[str, Any] = {"id": item_id, "content": content, "status": status}
            parent = str(entry.get("parent") or "").strip()
            if parent:
                item["parent"] = parent
            items.append(item)
        by_id = {item["id"]: item for item in items}
        for item in items:
            parent = item.get("parent")
            if not parent:
                continue
            target = by_id.get(parent)
            if target is None:
                return [], f"todo {item['id']}: parent {parent} is not in this list"
            if parent == item["id"] or target.get("parent"):
                return [], (f"todo {item['id']}: parent {parent} must be a top-level item "
                            "(two levels only)")
        return items, ""

    def _handle_todo(self, args: dict[str, Any]) -> str:
        """Replace the caller's todo list and queue the table for this answer.

        The caller is the main session (name: the profile) or a dispatched run (name: the agent,
        session: the run's own id). Both parts come from the run context the host binds, so a
        child never edits the list of the session that dispatched it.
        """
        name, session = self._todo_owner()
        if not session:
            return tool_error("no session id: the todo list is per run")
        raw = args.get("todos")
        if not isinstance(raw, list):
            return tool_error("todos must be a list")
        items, error = self._validate_todos(raw)
        if error:
            return tool_error(error)
        key = self._todo_key(name, session)
        owner = {"name": name, "session_id": session}
        if not items:
            row = self._store.get_conclusion("todo", key)
            deleted = bool(row is not None
                           and self._store.forget_conclusion("todo", int(row["id"])))
            return json.dumps({"stored": False, "deleted": deleted, "count": 0, "open": 0,
                               **owner})
        payload = json.dumps({"todos": items}, ensure_ascii=False)
        if len(payload) > _MAX_CONCLUSION_CHARS:
            return tool_error(f"todo_too_large: {len(payload)} chars "
                              f"(limit {_MAX_CONCLUSION_CHARS})")
        conclusions_id = self._store.save_conclusion(
            "todo", payload, key=key, session_id=session, subject="tool")
        # The answer being composed gets the table; the next turn's prefetch gets the counts.
        self.transform_llm_output_inject("todo", self._todo_table(items), session_id=session)
        open_count = sum(1 for item in items
                         if str(item.get("status")) in _TODO_OPEN_STATUSES)
        return json.dumps({"stored": True, "conclusions_id": conclusions_id,
                           "count": len(items), "open": open_count, **owner})

    # -- final-answer injection ------------------------------------------------

    def transform_llm_output_inject(self, inject_type: str, content_md: str,
                                    *, session_id: str = "") -> None:
        """Queue markdown to append to this session's final answer.

        The host's ``transform_llm_output`` hook pops the whole queue when the answer is
        rendered, so a block lands once and can never attach to a later turn. A stateful type
        replaces its earlier item: two versions of the todo table in one answer would
        contradict each other.
        """
        session = str(session_id or self._session_id or "").strip()
        if not session:
            logger.debug("Minir inject dropped: no session id for %s", inject_type)
            return
        text = (content_md or "").strip()
        if not text:
            return
        kind = str(inject_type or "").strip() or "general"
        with self._lock:
            queue = self._injects.setdefault(session, [])
            if kind in INJECT_STATEFUL:
                queue[:] = [item for item in queue if item["type"] != kind]
            queued = sum(len(item["content"]) for item in queue)
            if len(queue) >= _MAX_INJECT_ITEMS or queued + len(text) > _MAX_INJECT_CHARS:
                logger.warning("Minir inject refused for %s: %d items / %d chars queued",
                               session, len(queue), queued)
                return
            self._inject_seq += 1
            queue.append({"type": kind, "content": text, "seq": self._inject_seq})

    def _take_injects(self, session: str) -> str:
        """Render and clear this session's queue; ``""`` when nothing is queued."""
        with self._lock:
            queue = self._injects.pop(str(session or ""), [])
        if not queue:
            return ""
        order = {name: index for index, name in enumerate(INJECT_ORDER)}
        ordered = sorted(queue, key=lambda item: (order.get(item["type"], len(INJECT_ORDER)),
                                                 int(item["seq"])))
        return "\n\n".join(item["content"] for item in ordered)

    def recall_status(self) -> Optional[RecallStatus]:
        """What the last ``prefetch()`` injected — never a stale prior count."""
        return self._last_recall

    # -- persistence ----------------------------------------------------------

    def sync_turn(
        self, user_content: str, assistant_content: str, *,
        session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record the completed turn and hand it to the layer worker.

        The host's ``messages`` is the run's whole conversation, so the event store appends only
        the tail after its ``fp`` anchor (``transcript.ingest``); a compaction that removed the
        anchor writes a ``gap`` row instead of replaying silently. The in-memory window still
        takes the user/assistant pair, because the worker reads timestamp windows.

        ``turn_author`` (``{"id", "name", "is_bot"}``) is retained as evidence metadata for the
        user side; the base layer always models this profile's single user, not a per-author
        artifact.
        """
        if self._store is None or self._agent_context not in ("", "primary"):
            return  # host contract: non-primary contexts (subagent/cron/flush) do not write memory
        user_content = (user_content or "").strip()
        if not user_content or is_trivial_prompt(user_content):
            return
        if not self._reasoning:
            return  # nothing consumes the transcript when the layers are off
        session = session_id or self._session_id
        author, author_id = self._turn_author(turn_author, session)
        self._cancel_session_extraction(session)
        try:
            if messages:
                self._transcript.ingest(HOST_LAYER, session, messages, profile=self._profile)
            else:
                # A host that passes no slice still gets the pair on disk; no fingerprint is
                # needed because there is nothing to dedupe against.
                self._transcript.append_event(HOST_LAYER, session, "user", text=user_content,
                                              profile=self._profile)
                self._transcript.append_event(HOST_LAYER, session, "assistant",
                                              text=assistant_content or "",
                                              profile=self._profile)
            self._flush_reasoning(session)
            self._transcript.add_turn(session, user_content, assistant_content,
                                      author=author, author_id=author_id)
            # The turn's content exists now, so this is the earliest useful moment to drain the
            # jobs the tick queued for it (the worker coalesces and is a daemon thread).
            self._kick_worker()
        except Exception as exc:  # a memory failure must never break the host's turn worker
            logger.warning("Minir sync_turn failed: %s", exc)

    def _flush_reasoning(self, session: str) -> None:
        """Append the turn's aggregated reasoning row, redacted; nothing when never opted in."""
        parts = self._reasoning_deltas.pop(str(session or ""), None)
        if not parts:
            return
        text, redacted = _redact_text("".join(parts))
        self._transcript.append_reasoning(HOST_LAYER, session, text, profile=self._profile,
                                          redacted=redacted)

    def _record_system_prompt(self) -> None:
        """Record the system text this provider contributes to the host prompt.

        The host keeps no system rows in ``state.db`` and passes no system message in its
        ``messages`` slice, so the ``system`` role has no host source. The honest row is the block
        this plugin injects; ``source="plugin_system"`` says so, and the ``hash`` keeps one copy
        per distinct prompt. A host slice that does carry a ``system`` row is recorded by
        ``ingest`` as well.
        """
        if self._store is None or not self._session_id:
            return
        try:
            text = (self.system_prompt_block() or "").strip()
            if not text:
                return
            digest = fingerprint("system", text)[3:]
            rows = self._transcript.read_events(
                self._profile, HOST_LAYER, self._session_id, roles=("system",), limit=20)["rows"]
            if any(str(row.get("hash") or "") == digest for row in rows):
                return
            self._transcript.append_event(
                HOST_LAYER, self._session_id, "system", text=text, profile=self._profile,
                redacted=True, chars=len(text), hash=digest, source="plugin_system",
                fp=fingerprint("system", text))
        except Exception:
            logger.debug("Minir system prompt recording failed", exc_info=True)

    def _turn_author(self, turn_author: Optional[dict[str, Any]], session: str) -> tuple[str, str]:
        """``(name, id)`` of the user side of this turn: the host's argument, else the last tick."""
        source = turn_author if isinstance(turn_author, dict) and turn_author else self._authors.get(session) or {}
        return str(source.get("name") or "").strip(), str(source.get("id") or "").strip()

    # -- turn counting (base / dialectic-reasoning layers) ---------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Per-turn tick: advance both layer counters and queue the jobs that came due.

        The host passes its own session turn number (already incremented for this turn), so
        the counters follow the conversation instead of a second, drifting count. Fires for
        every user turn — including a trivial prompt that ``sync_turn`` skips — and, being a
        memory-provider hook, never for a subagent or cron turn.

        The author trio describes who wrote *this* turn; it is cached per session because the
        turn's content (and so the durable turn record) only arrives at ``sync_turn``.
        """
        if self._store is None or self._agent_context not in ("", "primary"):
            return
        try:
            self._cache_author(kwargs)
            # A real user turn is the one signal that this session is live again. Cancel a
            # pending idle extraction before the compression gate can suppress the rest.
            self._cancel_session_extraction(self._session_id)
            if self._store.compression_gate_active(self._session_id):
                return  # compression owns this window; periodic dispatch resumes afterwards
            self._scheduler.on_turn(self._session_id, turn_number)
        except Exception as exc:  # counting must never break a turn
            logger.warning("Minir on_turn_start failed: %s", exc)

    def _cache_author(self, kwargs: dict[str, Any]) -> None:
        """Remember the last author seen for the bound session (bounded, like the counter)."""
        author_id = str(kwargs.get("author_id") or "")
        author_name = str(kwargs.get("author_name") or "")
        if not (author_id or author_name):
            return
        session = self._session_id
        if session not in self._authors and len(self._authors) >= _MAX_AUTHOR_CACHE:
            self._authors.pop(next(iter(self._authors)))
        self._authors[session] = {"id": author_id, "name": author_name}

    def on_pre_compress(self, messages: Optional[list[dict[str, Any]]] = None) -> str:
        """Compression boundary: pause periodic scheduling and refresh both layers synchronously.

        The base job is queued for the unreasoned window, drained inline, and its dependent
        dialectic review follows before returning. The counters are moved to the current turn so
        the next periodic tick does not immediately repeat the same window. The context engine
        clears the gate after it has produced the summary.
        """
        try:
            self._prepare_compression(self._session_id)
        except Exception as exc:
            logger.warning("Minir on_pre_compress failed: %s", exc)
        return ""

    def _prepare_compression(self, session_id: str) -> None:
        if self._store is None or not session_id or self._agent_context not in ("", "primary"):
            return
        # Compression is a hard boundary: in-flight adviser advice is stale once the window is
        # rewritten, and the gate below suppresses new reviews until the engine releases it.
        self._adviser_mode.session_reset(session_id)
        if not self._reasoning or self._worker is None or not self._worker.enabled:
            return  # reasoning disabled: there is no worker that could drain a job
        now = time.time()
        self._store.set_compression_gate(
            session_id, expires_at=now + _COMPRESSION_GATE_TTL)
        try:
            start = max(self._store.last_processed_ts(BASE_LAYER, session_id),
                        self._store.latest_chunk_end(session_id))
            snapshot = TRACKER.snapshot(session_id) or {}
            counted = snapshot.get("layers") or {}
            turns = int((counted.get(BASE_LAYER) or {}).get("turns", 0))
            # The wall clock is coarser (~15 ms on Windows) than a turn, so a boundary that
            # follows the previous drain within one tick would look like it covers nothing.
            # The content's own timestamp decides whether there is anything left to reason over.
            self._transcript.flush(session_id)  # the window is about to be reasoned over
            end = max(now, self._transcript.latest_ts(session_id),
                      host_history.latest_ts(self._state_db, session_id))
            if end > start:
                self._store.record_job(BASE_LAYER, session_id, start, end, turns=turns)
            # The review gets its own window, queued after the base job so the FIFO drain
            # publishes the artifact it reads. Its start is its own dispatch watermark: the
            # layers never share one, and reusing the base edge would skip the turns between.
            review_start = float((counted.get(DIALECTIC_LAYER) or {}).get("last_trigger_ts")
                                 or start)
            if end > review_start:
                self._store.record_job(
                    DIALECTIC_LAYER, session_id, review_start, end,
                    turns=int((counted.get(DIALECTIC_LAYER) or {}).get("turns", 0)))
            if self._worker is not None:
                self._worker.drain_session(session_id, timeout=_PRE_COMPRESS_TIMEOUT)
                self._kick_worker()  # let a timed-out drain finish in the background
            # The window being compressed is now reasoned over: publish its calibrated fields
            # before the counters reset. The summary this engine writes afterwards is separate.
            self.publish_base_conclusions(session_id)
            TRACKER.reset_layers(session_id)
        except Exception as exc:
            self._store.clear_compression_gate(session_id)
            logger.warning("Minir pre-compression layer refresh failed: %s", exc)

    def on_session_end(self, messages: Optional[list[dict[str, Any]]] = None) -> None:
        """Real session boundary: flush the un-reasoned tail once, finalise usage, arm the timer.

        The host queues this ahead of ``on_session_switch`` on one serialized worker, so the
        counter always reflects every turn of the ending session. ``tail()`` is watermark-based
        and therefore a no-op when the tail is already covered — a ``/new`` reaches this
        method, ``on_session_finalize`` and ``on_session_reset`` for the same boundary.

        Finalisation itself never depends on the flush: a failed boundary drain must not lose the
        session's token accounting or its idle timer.
        """
        session = self._session_id
        try:
            self._flush_boundary(session, close=True)
        except Exception as exc:
            logger.warning("Minir on_session_end flush failed: %s", exc)
        self._drain_boundary(session)
        self._finish_session(session)

    def _drain_boundary(self, session: str) -> None:
        """Wait for the boundary drain before the session's one calibrated publish.

        ``_flush_boundary`` records the jobs and kicks a background drain; publishing against a
        refresh row that does not exist yet would write nothing and burn the session's single
        shot. Bounded like the compression drain, and never fatal to finalisation.
        """
        if self._worker is None or not session:
            return
        try:
            self._worker.drain_session(session, timeout=_PRE_COMPRESS_TIMEOUT)
        except Exception as exc:
            logger.warning("Minir boundary drain failed: %s", exc)

    def publish_base_conclusions(self, session_id: str) -> int:
        """Publish this session's calibrated base fields as conclusions (once per session)."""
        if self._store is None or self._worker is None or not session_id:
            return 0
        try:
            written = self._store.publish_base_conclusions(
                session_id, user_subject=self._worker.user_subject(),
                ai_subject=self._worker.dialectic_subject())
        except Exception as exc:
            logger.warning("Minir base conclusion publish failed: %s", exc)
            return 0
        if written:
            logger.info("Minir published %d base conclusions for %s", written, session_id)
        return written

    def _finish_session(self, session: str) -> None:
        """Boundary finalisation: per-turn usage, adviser state, then the idle timer."""
        if not session:
            return
        self.publish_base_conclusions(session)
        try:
            totals = self._finalize_session_usage(session)
            logger.info("Minir session %s final usage: turns=%s api_calls=%s tokens=%s",
                        session, totals.get("turns", 0), totals.get("api_calls", 0),
                        totals.get("total_tokens", 0))
        except Exception as exc:
            logger.warning("Minir usage finalisation failed: %s", exc)
        try:
            self._adviser_mode.session_end(session)
        except Exception as exc:
            logger.debug("Minir adviser session end failed: %s", exc)
        self._schedule_session_extraction(session)

    # -- session finalisation: usage + idle knowledge extraction --------------

    def record_api_usage(self, session_id: str, turn_id: str, usage: Any, *,
                         api_calls: Any = 1, model: str = "") -> None:
        """Fold one ``post_api_request`` usage payload into its turn's running total.

        Keyed by the host's ``turn_id``, so the final per-turn consumption survives the many
        API calls one turn can make. Calls for another session (a delegate, a cron run) are not
        this provider's accounting and are ignored.
        """
        if self._store is None or not session_id or not turn_id:
            return
        if self._session_id and str(session_id) != self._session_id:
            return
        payload = usage if isinstance(usage, dict) else {}
        if not payload:
            return

        def number(*keys: str) -> int:
            for key in keys:
                value = payload.get(key)
                if value is None:
                    continue
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
            return 0

        prompt = number("prompt_tokens", "input_tokens")
        completion = number("completion_tokens", "output_tokens")
        try:
            self._store.record_turn_usage(
                str(session_id), str(turn_id), prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=number("total_tokens") or (prompt + completion),
                cache_read_tokens=number("cache_read_tokens"),
                cache_write_tokens=number("cache_write_tokens"),
                reasoning_tokens=number("reasoning_tokens"),
                api_calls=_as_positive_int(api_calls), model=model)
        except Exception as exc:  # accounting must never break an API response
            logger.debug("Minir turn usage write failed: %s", exc)

    def _finalize_session_usage(self, session: str) -> dict[str, Any]:
        """Close the turn rows and store the session total beside the host's own aggregate."""
        if self._store is None or not session:
            return {}
        totals = self._store.finalize_session_usage(session)
        host = host_history.session_totals(self._state_db, session)
        self._store.record_session_usage(session, totals, host=host)
        return {**totals, "host_total_tokens": int(host.get("total_tokens") or 0)}

    def _session_marker_snapshot(self, session: str) -> dict[str, Any]:
        """The markers this session produced, plus what the final summary agent will need."""
        snapshot: dict[str, Any] = {"notes": [], "value_markers": [], "summary": "",
                                    "turn_usage": [], "window_from_ts": 0.0,
                                    "window_to_ts": 0.0}
        if self._store is None or not session:
            return snapshot
        try:
            notes = self._store.session_markers(session)
            # Bound each marker: its text can be large, and the snapshot is evidence for one
            # extraction run, not an archive of the conclusions table.
            snapshot["notes"] = [
                {"conclusions_id": note.get("conclusions_id"), "kind": note.get("kind"),
                 "content": str(note.get("content") or "")[:400],
                 "created_at": note.get("created_at")}
                for note in notes[-50:]]
        except Exception as exc:
            logger.debug("Minir marker snapshot failed: %s", exc)
        try:
            base = self._store.latest_base_refresh(session) or {}
            snapshot["window_from_ts"] = float(base.get("window_from_ts") or 0.0)
            snapshot["window_to_ts"] = float(base.get("window_to_ts") or 0.0)
            value_markers: Any = []
            raw = str(base.get("value_markers") or "").strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        value_markers = parsed.get("value_markers")
                        if value_markers is None:
                            value_markers = parsed.get("units")
                    else:
                        value_markers = parsed
                except ValueError:
                    value_markers = []
            if isinstance(value_markers, list):
                snapshot["value_markers"] = value_markers[:16]
            summary = self._store.newest_conclusion("session_summary", session_id=session)
            snapshot["summary"] = str((summary or {}).get("content") or "")[:4_000]
            snapshot["turn_usage"] = self._store.turn_usage(session, limit=50)
        except Exception as exc:
            logger.debug("Minir base artifact snapshot failed: %s", exc)
        return snapshot

    @staticmethod
    def _encode_marker_snapshot(snapshot: dict[str, Any]) -> str:
        """Serialize the marker snapshot, shrinking its lists rather than cutting JSON mid-string."""
        notes = list(snapshot.get("notes") or [])
        value_markers = list(snapshot.get("value_markers") or [])
        usage = list(snapshot.get("turn_usage") or [])
        payload = json.dumps(snapshot, ensure_ascii=False)
        for note_cap, unit_cap, usage_cap in ((30, 16, 20), (20, 12, 10), (10, 6, 5), (0, 0, 0)):
            if len(payload) <= _MAX_MARKER_SNAPSHOT:
                break
            snapshot["notes"] = notes[-note_cap:] if note_cap else []
            snapshot["value_markers"] = value_markers[:unit_cap]
            snapshot["turn_usage"] = usage[:usage_cap]
            payload = json.dumps(snapshot, ensure_ascii=False)
        return payload

    def _schedule_session_extraction(self, session: str) -> None:
        """Arm the idle timer with this session's marker snapshot; cancelled by a later turn."""
        if self._store is None or not session:
            return
        if self._agent_context not in ("", "primary"):
            return  # a subagent/cron session is not a conversation the operator returns to
        if not self._reasoning or not self._session_extraction:
            return
        try:
            snapshot = self._session_marker_snapshot(session)
            if not (snapshot.get("notes") or snapshot.get("value_markers")):
                # No value markers, nothing to time: the session was not durable enough to
                # spend a summary + extraction run on.
                logger.debug("Minir extraction skipped for %s: no value markers", session)
                return
            window_to = max(float(snapshot.get("window_to_ts") or 0.0),
                            host_history.latest_ts(self._state_db, session), time.time())
            payload = self._encode_marker_snapshot(snapshot)
            self._store.schedule_extraction(
                session, due_at=time.time() + self._knowledge_idle_seconds,
                markers=payload,
                window_from_ts=float(snapshot.get("window_from_ts") or 0.0),
                window_to_ts=window_to,
                turns=len(snapshot.get("turn_usage") or []))
            logger.info("Minir extraction armed for %s in %.0fs", session,
                        self._knowledge_idle_seconds)
        except Exception as exc:
            logger.warning("Minir extraction schedule failed: %s", exc)

    def _cancel_session_extraction(self, session: str) -> None:
        """A user typed in the session: the idle timer must not fire."""
        if self._store is None or not session:
            return
        try:
            removed = self._store.cancel_extraction(session)
            if removed:
                logger.info("Minir extraction cancelled for %s (session is live)", session)
        except Exception as exc:
            logger.debug("Minir extraction cancel failed: %s", exc)

    def _session_record(self, session: str) -> str:
        """The latest compression record, or the base layer's window summary when none exists."""
        if self._store is None:
            return ""
        row = self._store.latest_compression_summary(session)
        text = str((row or {}).get("content") or "").strip()
        if text:
            return text
        base = self._store.latest_base_refresh(session) or {}
        return str(artifact_from_columns(base).get("window_summary") or "").strip()

    def _run_session_extraction(self, row: dict[str, Any]) -> dict[str, int]:
        """Run one due session: the final summary from the compression record, then pages."""
        if self._store is None or self._knowledge is None:
            raise RuntimeError("extraction pipeline is not initialized")
        session = str(row.get("session_id") or "")
        try:
            snapshot = json.loads(str(row.get("markers") or "{}"))
        except (TypeError, ValueError):
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        usage = (self._store.session_usage(session)
                 or {"turns": len(snapshot.get("turn_usage") or [])})
        window_end = float(row.get("window_to_ts") or 0.0)
        session_record = self._session_record(session)
        summary_text = session_summary.summarize_agent(
            session_id=session, session_record=session_record, markers=snapshot, usage=usage,
            window_end=window_end)
        if not summary_text and session_record:
            # The record is a real audited compression summary; using it beats losing the record.
            summary_text = session_record
        if summary_text:
            self._store.save_conclusion(
                "session_summary", summary_text, key=f"session_summary:{session}:final",
                session_id=session, subject="session",
                evidence=json.dumps(["session_record"] if session_record else [],
                                    ensure_ascii=False),
                status="active")
        pages = extraction.extract_pages(
            session_id=session, summary=summary_text,
            markers=snapshot, usage=usage, hermes_home=self._hermes_home,
            state_db=self._state_db,
            transcript=self._transcript, profile=self._profile, window_end=window_end)
        written = 0
        results = knowledge.run_value_markers(
            pages, root=self._knowledge.root, session_id=session,
            source=f"session_extraction:{session}",
            language=self._language,
            budget=self._knowledge_token_budget)
        for item in results:
            if item["status"] in {"created", "updated"}:
                written += 1
            else:
                logger.debug("Minir extraction page %s: %s", item["topic"], item["status"])
        return {"pages": written, "summary_chars": len(summary_text)}

    def _flush_boundary(self, session_id: str, *, close: bool = False) -> None:
        """Boundary drain: the counted dialectic tail, plus whatever content no job covered.

        The counter's watermark counts *turns*, but a turn's messages are stored after its
        dispatch stamp, so the layer that fired on that turn has nothing to reason over yet
        and its next window starts too late. The content watermark (``processed_ts``) is the
        authority here: a layer that still owns unreasoned content gets a job even when the
        counter says its cadence is settled. A short session — the common case — would
        otherwise never contribute to either artifact.
        """
        triggers = TRACKER.tail(session_id, close=close)
        self._transcript.flush(session_id)
        self._dispatch(triggers + self._unreasoned_content(session_id, {t.layer for t in triggers}))
        self._kick_worker()

    def _unreasoned_content(self, session_id: str, covered: set[str]) -> list[Trigger]:
        """One boundary job per layer whose stored content no completed job has reasoned over."""
        if self._store is None or not session_id or self._agent_context not in ("", "primary"):
            return []
        snapshot = TRACKER.snapshot(session_id) or {}
        counted = snapshot.get("layers") or {}
        now = time.time()
        pending: list[Trigger] = []
        for layer in (BASE_LAYER, DIALECTIC_LAYER):
            if layer in covered:
                continue  # the counted tail already owns this layer's window
            if self._store.has_open_job(layer, session_id):
                continue  # a queued job reads to ``now`` when it runs: it will cover this content
            drained = self._store.last_processed_ts(layer, session_id)
            # The buffer is not the only source: after a restart or bucket eviction the tail
            # exists only in state.db. A boundary must still queue it.
            buffered_tail = self._transcript.has_after(session_id, drained)
            stored_tail = host_history.latest_ts(self._state_db, session_id) > drained
            if not (buffered_tail or stored_tail):
                continue
            pending.append(Trigger(layer=layer, session_id=session_id, from_ts=drained,
                                   to_ts=max(now, drained + WINDOW_EPSILON),
                                   turns=int((counted.get(layer) or {}).get("turns", 0))))
        return pending

    def _run_turn_layers(self, session_id: str, turn_number: Any) -> str:
        """The one turn task: advance both counters and persist the windows that came due."""
        triggers = TRACKER.tick(session_id, turn_number)
        self._dispatch(triggers)
        return f"queued={len(triggers)}"

    def _dispatch(self, triggers: list[Trigger]) -> None:
        """Persist each due job (session + window) for the layer worker to pick up.

        Both layers dispatch independently: a tick that makes both due records the base job
        first and the review second, so the single worker thread (oldest-first FIFO) always
        publishes the base artifact before the review that reads it. That ordering is the
        whole dependency: the review is not queued by the base job, and a review whose base
        never published degrades to a no-op instead of waiting.

        Recording only — the drain is kicked where content exists (``sync_turn``, a boundary,
        shutdown). Draining straight from the tick (or from ``initialize``, which would race the
        first turn) hands the worker a window whose turn has not arrived yet and burns it empty.
        """
        if not triggers:
            return
        ordered = sorted(triggers, key=lambda trigger: 0 if trigger.layer == BASE_LAYER else 1)
        for trigger in ordered:
            job_id: Optional[int] = None
            # Tell the transcript where the next window will start: a message stamped at or
            # before this edge would fall outside both this window and the next one.
            self._transcript.mark_edge(trigger.session_id, trigger.to_ts)
            if self._store is not None:
                try:
                    job_id = self._store.record_job(trigger.layer, trigger.session_id,
                                                    trigger.from_ts, trigger.to_ts, trigger.turns)
                except Exception as exc:
                    logger.warning("Minir job record failed: %s", exc)
            logger.info("Minir %s-layer job queued (#%s): session=%s turns=%d window=%s..%s",
                        trigger.layer, job_id, trigger.session_id or "-", trigger.turns,
                        format_when(trigger.from_ts), format_when(trigger.to_ts))

    def _has_queued_layer_jobs(self) -> bool:
        """Due check for the layer-drain tick: only spawn a drain when the queue has work."""
        return self._store is not None and self._store.queued_job_count() > 0

    def _drain_layers_tick(self) -> str:
        """Start a background drain between turns; a drain already running is left alone."""
        self._kick_worker()
        return "kicked"

    def _kick_worker(self) -> None:
        """Start a background drain unless one is already running (never on the tick path)."""
        if self._worker is not None:
            self._worker.kick(spawn=spawn_context_thread)

    # -- session lifecycle ----------------------------------------------------

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        """Rebind per-session state after /resume, /branch, /reset, /new or compression."""
        previous_id = self._session_id
        new_session_id = new_session_id or ""
        if new_session_id and new_session_id != previous_id:
            if reset:
                # New conversation: flush the old tail (a no-op when on_session_end already
                # did, which is the usual /new path), freeze it, and open the next session.
                # The old session's messages stay buffered: its boundary job was only just
                # queued, and dropping them here would empty the very window it has to read.
                self._flush_boundary(previous_id, close=True)
                TRACKER.stop(previous_id)
                TRACKER.begin(new_session_id, mode=self._session_mode)
            elif parent_session_id:
                # /branch: the branch carries the transcript forward, so it continues the
                # parent's counters instead of re-reasoning history the parent already covered.
                # The messages are carried over too — the branch inherits the conversation.
                self._transcript.extend(new_session_id, self._transcript.messages(previous_id))
                TRACKER.inherit(previous_id, new_session_id)
            else:
                # /resume: a different session id continues from a persisted transcript; the
                # host's turn number rehydrates the count on the first tick.
                TRACKER.begin(new_session_id, mode=self._session_mode)
        self._session_id = new_session_id or self._session_id
        if previous_id and reset:
            self._adviser_mode.session_reset(previous_id)
        self._adviser_mode.session_started(self._session_id)
        if reset:
            self._last_recall = None

    def shutdown(self) -> None:
        """Flush and close. Idempotent — the host may call it more than once."""
        if self._store is not None:
            try:  # a clean exit must not leave the last window unreasoned
                self._flush_boundary(self._session_id)
            except Exception as exc:
                logger.debug("Minir shutdown flush failed: %s", exc)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.kick(spawn=spawn_context_thread)
            worker.stop()  # bounded join: the store cannot close under a draining job
        # A process exit is also an end of the bound session: close its token accounting and
        # arm the idle timer (idempotent when on_session_end already did both).
        try:
            self._finalize_session_usage(self._session_id)
        except Exception as exc:
            logger.debug("Minir shutdown usage finalisation failed: %s", exc)
        self._schedule_session_extraction(self._session_id)
        self._adviser_mode.session_end(self._session_id)
        self._adviser_mode.stop()  # no review may touch the store after this point
        self._scheduler.stop()     # no scheduled job may touch the store after this point
        if self._knowledge is not None:
            self._knowledge.stop()      # drain queued value markers while the filesystem still exists
        self._transcript.close()        # last writer join: no JSON may be half-written
        self.flush_metrics()
        # Only now that no drain is in flight: a queued boundary job must still find its window.
        self._transcript.clear()
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception as exc:
                logger.debug("Minir store close failed: %s", exc)
        global _ACTIVE_PROVIDER
        if _ACTIVE_PROVIDER is self:
            _ACTIVE_PROVIDER = None

    # -- tools ----------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """The same schemas the ``minir`` registry toolset registers (one source in tools.py)."""
        return list(plugin_tools.SCHEMAS)

    _STEP_RE = re.compile(r"^[ \t]*\d+[.)]\s+\S")

    @classmethod
    def _looks_like_method(cls, content: str) -> bool:
        """A numbered procedure is the only shape allowed to fall back to METHOD."""
        return sum(1 for line in str(content or "").splitlines()
                   if cls._STEP_RE.match(line)) >= 2

    def _remember_knowledge(self, org: str, content: str, args: dict[str, Any]) -> str:
        """Queue one page unit for the writer; the tool never writes a page itself."""
        if org not in ORGS:
            return tool_error(f"type must be one of: {', '.join(ORGS)}")
        category = str(args.get("category") or "").strip().upper()
        if category not in CATEGORIES:
            # An omitted category is only recoverable for a numbered procedure; a stated but
            # unknown one is a caller error, and every other content must name its category.
            if category or not self._looks_like_method(content):
                return tool_error(f"category must be one of: {', '.join(CATEGORIES)}")
            category = "METHOD"
        lines = content.strip("\n").splitlines()
        if len(lines) < 3 or len(lines) > 8:
            return tool_error("knowledge content must be 3..8 lines")
        if len(content.strip("\n")) > 1200:
            return tool_error("knowledge content must be at most 1200 characters")
        epistemic = str(args.get("epistemic") or "").strip().lower()
        if category == "KNOWLEDGE" and epistemic not in EPISTEMIC:
            return tool_error(f"KNOWLEDGE requires epistemic in {', '.join(EPISTEMIC)}")
        summary = " ".join(str(args.get("summary") or lines[0]).split())[:120]
        topic = " ".join(str(args.get("topic") or summary).split())
        if len(topic) < 2:
            return tool_error("topic must be at least 2 characters")
        try:
            path = (knowledge.normalize_path(args.get("path")) if str(args.get("path") or "").strip()
                    else knowledge.resolve_path(org, category, topic))
        except knowledge.KnowledgeError as exc:
            return tool_error(str(exc))
        unit = {
            "path": path,
            "category": category, "org": org, "topic": topic, "summary": summary,
            "epistemic": epistemic if category == "KNOWLEDGE" else "",
            "time": str(args.get("time") or ""), "objects": [], "relations": [],
            "content": "\n".join(lines),
            "anchor": {"from_ts": time.time(), "to_ts": time.time(), "message_ids": []},
        }
        if self._knowledge is None:
            return tool_error("knowledge pipeline is not initialized")
        queued = self._knowledge.enqueue_from_refresh(
            self._session_id, [unit], from_ts=unit["anchor"]["from_ts"],
            to_ts=unit["anchor"]["to_ts"])
        self._knowledge.start()  # a reasoning-off profile still needs its writer
        return json.dumps({"queued": bool(queued), "type": org, "category": category,
                           "topic": topic, "remember_mode": "knowledge"})

    def _remember_card(self, part: str, content: str, args: dict[str, Any]) -> str:
        """Overlay one global card part; the other three files are preserved."""
        if part not in card_review.PARTS:
            return tool_error(f"type must be one of: {', '.join(card_review.PARTS)}")
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return tool_error("reason is required for a card write")
        revisions = card_review.write_cards(
            self._store, self._hermes_home, self._profile, {part: content},
            source="operator_edit", parts=(part,), reason=reason)
        return json.dumps({"stored": True, "type": part, "revisions": revisions,
                           "remember_mode": "card"})

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Dispatch one of this provider's tools. Must return a JSON string."""
        if tool_name == content_valid.TOOL_NAME:
            # A local count: no store, no model, so it must answer before initialization too.
            return content_valid.handle(args or {})
        if tool_name == kb_write.TOOL_NAME:
            return kb_write.handle(
                args or {}, hermes_home=self._hermes_home, language=self._language,
                source_session=str(getattr(self, "_session_id", "") or ""))
        if tool_name == plugin_tools.ADVISE_TOOL:
            # A read-only consult: no store write, so it answers before the store opens too.
            if not _coerce_bool(self._config.get("advise_tool"), True):
                return tool_error("minir_advise is off (adviser_mode.advise_tool: false)")
            if not self._adviser_mode.enabled(self._session_id):
                return tool_error("the adviser is off; there is nobody to consult")
            if agent_watch.is_child(self._session_id):
                # A dispatched run asking the adviser is the adviser asking itself one remove
                # later: the review already owns that judgement.
                return tool_error("minir_advise is not available to a dispatched child run")
            return json.dumps(
                self._fork.advise(self._session_id, str((args or {}).get("phase") or ""),
                                  str((args or {}).get("question") or "").strip()),
                ensure_ascii=False)
        if self._store is None:
            return tool_error("Minir is not initialized")
        args = args or {}
        if tool_name == "minir_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return tool_error("query is required")
            scope = str(args.get("scope") or "session").strip().lower()
            if scope not in {"session", "all"}:
                return tool_error("scope must be 'session' or 'all'")
            kinds: Optional[frozenset[str]] = None
            kind_filter = str(args.get("type") or "").strip()
            if kind_filter:
                if kind_filter not in CONCLUSION_KINDS:
                    return tool_error(
                        f"type must be one of: {', '.join(CONCLUSION_KINDS)}")
                kinds = frozenset({kind_filter})
            explicit_session = str(args.get("session_id") or "").strip()
            session_id = explicit_session or str(self._session_id or "")
            # A session-bound kind must not fall back to the provider's current session when
            # the caller named none: that would silently read another conversation's ruler.
            # The empty id reaches store.search, which raises session_scope_required.
            if kinds and is_session_bound(kinds) and not explicit_session:
                session_id = ""
            try:
                as_of = float(args["as_of"]) if args.get("as_of") not in (None, "") else None
            except (TypeError, ValueError):
                return tool_error("as_of must be an epoch timestamp")
            try:
                expand = int(args.get("expand") or 0)
            except (TypeError, ValueError):
                return tool_error("expand must be 0, 1 or 2")
            if not 0 <= expand <= 2:
                return tool_error("expand must be 0, 1 or 2")
            try:
                rows = self._search(query, limit=args.get("limit"),
                                    session_id=session_id, scope=scope, kinds=kinds,
                                    as_of=as_of, expand=expand,
                                    include_superseded=bool(args.get("include_superseded")))
            except SessionScopeError as exc:
                return tool_error(str(exc))
            results = [{
                "conclusions_id": row["conclusions_id"],
                "kind": row["kind"],
                "key": row.get("key") or "",
                "text": _excerpt(row["text"]),
                "when": format_when(row["when"]),
                "author": row["author"],
                "subject": row.get("subject") or "",
                "session_id": row.get("session_id") or "",
                "score": row.get("score") or 0.0,
                "rank": row.get("rank") or 0.0,
                "version": row.get("version"),
                "version_status": row.get("version_status") or "",
                "via": row.get("via") or "",
            } for row in rows]
            return json.dumps({"count": len(rows), "scope": scope, "results": results})
        if tool_name == "minir_kb_search":
            pattern = str(args.get("query") or "").strip()
            if not pattern:
                return tool_error("query is required")
            try:
                limit = int(args.get("limit") or knowledge.DEFAULT_SEARCH_LIMIT)
            except (TypeError, ValueError):
                return tool_error("limit must be an integer")
            try:
                payload = knowledge.search_pages(
                    pattern, knowledge.knowledge_dir(self._hermes_home),
                    org=str(args.get("org") or "").strip(),
                    category=str(args.get("category") or "").strip(),
                    limit=max(1, min(limit, 25)), hermes_home=self._hermes_home)
            except ValueError as exc:
                return tool_error(str(exc))
            if payload is None:
                return tool_error("minir_kb_search needs ripgrep; it is not installed or it "
                                  "refused the pattern (Rust regex: no lookaround, no "
                                  "backreferences)")
            return json.dumps(payload)
        if tool_name == "minir_remember":
            mode = str(args.get("remember_mode") or "").strip().lower()
            if not mode:
                return tool_error("remember_mode is required: knowledge or card")
            if mode not in ("knowledge", "card"):
                return tool_error("remember_mode must be knowledge or card")
            kind = str(args.get("type") or "").strip()
            if not kind:
                return tool_error("type is required")
            content = str(args.get("content") or "").strip()
            if not content:
                return tool_error("content is required")
            if mode == "knowledge":
                return self._remember_knowledge(kind, content, args)
            return self._remember_card(kind, content, args)
        if tool_name == "minir_forget":
            mode = str(args.get("remember_mode") or "").strip().lower()
            if not mode:
                return tool_error("remember_mode is required: knowledge or card")
            if mode not in ("knowledge", "card"):
                return tool_error("remember_mode must be knowledge or card")
            if mode == "knowledge":
                # ``type`` takes no part here: a page is addressed by its path.
                path = str(args.get("path") or "").strip()
                if not path:
                    return tool_error("path is required for knowledge mode")
                result = knowledge.archive_page(
                    knowledge.knowledge_dir(self._hermes_home), path,
                    reason=str(args.get("reason") or ""))
                return json.dumps(result)
            kind = str(args.get("type") or "").strip()
            if kind not in card_review.PARTS:
                return tool_error(
                    f"type must be one of: {', '.join(card_review.PARTS)}")
            reason = str(args.get("reason") or "").strip()
            if not reason:
                return tool_error("reason is required for a card forget")
            revisions = card_review.write_cards(
                self._store, self._hermes_home, self._profile, {kind: ""},
                source="operator_edit", parts=(kind,), reason=reason)
            return json.dumps({"cleared": True, "type": kind, "revisions": revisions})
        if tool_name == "minir_link":
            try:
                src = _endpoint(args.get("src"))
                dst = _endpoint(args.get("dst"))
            except ValueError as exc:
                return tool_error(str(exc))
            rel = str(args.get("rel") or "").strip()
            if rel not in RELATION_TYPES:
                return tool_error(f"rel must be one of: {', '.join(RELATION_TYPES)}")
            linked = self._store.link(src, rel, dst,
                                      weight=float(args.get("weight") or 1.0))
            return json.dumps({"linked": linked, "src": ":".join(src), "rel": rel,
                               "dst": ":".join(dst),
                               "error": "" if linked else "endpoint not found"})
        if tool_name == "minir_unmerge":
            kind = str(args.get("kind") or "").strip()
            key = str(args.get("key") or "").strip()
            reason = str(args.get("reason") or "").strip()
            if not kind or not key or not reason:
                return tool_error("kind, key and reason are required")
            try:
                return json.dumps(self._store.unmerge(kind, key, reason))
            except ValueError as exc:
                return tool_error(str(exc))
        if tool_name == "minir_todo":
            return self._handle_todo(args)
        if tool_name == "minir_stats":
            with self._lock:
                metrics = {tool: dict(values) for tool, values in self._metrics.items()}
            return json.dumps({**self._store.stats(), "session_id": self._session_id,
                               "conclusions_by_kind":
                                   self._store.conclusions_by_kind(self._session_id),
                               "profile": self._profile,
                               "layers": TRACKER.snapshot(self._session_id),
                               "scheduler": self._scheduler.snapshot(),
                               "scheduler_history": self._store.scheduler_runs(limit=20),
                               "adviser_mode": {
                                   **self._store.adviser_mode_stats(self._session_id),
                                   "usage": (self._adviser_mode.usage_stats(self._session_id)
                                             if self._adviser_mode else {}),
                               },
                               "targets": self._store.review_stats(
                                   self._session_id,
                                   window=self._review_stats_window(),
                                   targets=CONSISTENCY_TARGETS),
                               "artifacts": self._store.all_layer_states(),
                               "transcript": self._transcript.stats(),
                               "buffered_messages": len(self._transcript.messages(self._session_id)),
                               "prefetch_limit": self._prefetch_limit,
                               "dialectic_depth": self._dialectic_depth,
                               "session_usage": self._store.session_usage(self._session_id),
                               "turn_usage": self._store.turn_usage(self._session_id),
                               "tool_metrics": metrics,
                               "reasoning_enabled": self._reasoning})
        if tool_name == "minir_agents":
            return minir_agents.run(self, args)
        return tool_error(f"Unknown tool: {tool_name}")

    # -- three-day card review ------------------------------------------------

    def _review_stats_window(self) -> int:
        """The review-statistics window; the provider may be built without adviser config."""
        section = self._config.get("adviser_mode")
        if not isinstance(section, dict):
            return 8
        try:
            return max(1, min(int(section.get("review_stats_window") or 8), 50))
        except (TypeError, ValueError):
            return 8

    def _run_card_review(self, since: float) -> list[int]:
        """Run one global card review; the scheduler owns its anchor and retries."""
        if self._worker is None:
            raise card_review.CardReviewError("no layer worker")
        revisions = card_review.run_review(
            self._store, self._hermes_home, self._profile,
            complete=self._worker.review_cards, since=since,
            plugin_yaml=self._worker._plugin_yaml,
            language=self._language,
            transcript=self._transcript)
        return revisions

    # -- cold start -----------------------------------------------------------

    def cold_start(self, session_id: str = "") -> bool:
        """Queue the first *base* refresh for a freshly opened primary session.

        The first base job runs even when the host has no messages yet: its job is to write a
        session card baseline (the global card files, the previous session card, or the role
        preset), which the first real window then extends. The review is not queued here — the
        dialectic watermark starts at the current turn, so only base fires at session start and
        the review fires on its own cadence once there is a published base artifact to read.

        The plugin hook fires for subagent/cron sessions too, so callers must gate on the
        provider's bound session and ``agent_context``. Returns True when a job was queued.
        """
        if self._store is None or not self._reasoning:
            return False
        session = session_id or self._session_id
        if not session:
            return False
        if self._agent_context not in ("", "primary"):
            return False
        if session != self._session_id:
            return False
        snapshot = TRACKER.snapshot(session) or {}
        if not snapshot or snapshot.get("closed"):
            return False
        if self._store.has_open_job(BASE_LAYER, session):
            return False
        try:
            started = float(snapshot.get("started_at") or time.time())
            now = max(time.time(), started + WINDOW_EPSILON)
            job_id = self._store.record_job(BASE_LAYER, session, started, now, turns=0)
            TRACKER.mark_dispatched(session, BASE_LAYER, now)
            logger.info("Minir cold-start base job queued (#%s) for session=%s", job_id, session)
            self._kick_worker()
            return True
        except Exception as exc:
            logger.debug("Minir cold start failed: %s", exc)
            return False

    # -- tool metrics ---------------------------------------------------------

    def record_metric(self, tool_name: str, duration_ms: Any, *, error: bool = False) -> None:
        """Aggregate a tool call in memory; flush periodically, never on every call."""
        name = str(tool_name or "unknown")[:80]
        try:
            duration = max(0.0, float(duration_ms or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        with self._lock:
            row = self._metrics.setdefault(name, {"count": 0, "total_ms": 0.0,
                                                  "max_ms": 0.0, "errors": 0})
            row["count"] += 1
            row["total_ms"] += duration
            row["max_ms"] = max(row["max_ms"], duration)
            row["errors"] += 1 if error else 0
            self._metrics_seen += 1
            flush = self._metrics_seen % 20 == 0
        if flush:
            self.flush_metrics()

    def flush_metrics(self) -> None:
        if self._store is None:
            return
        with self._lock:
            entries, self._metrics = self._metrics, {}
        if not entries:
            return
        try:
            self._store.record_metrics(entries)
        except Exception as exc:
            logger.debug("Minir metrics flush failed: %s", exc)

    # -- internals ------------------------------------------------------------

    def _search(self, query: str, limit: Optional[Any] = None, *, session_id: str = "",
                kinds: Optional[frozenset[str]] = None,
                scope: str = "session", as_of: Optional[float] = None, expand: int = 0,
                include_superseded: bool = False) -> list[dict[str, Any]]:
        try:
            requested = int(limit) if limit is not None else self._prefetch_limit
        except (TypeError, ValueError):
            requested = self._prefetch_limit
        session = session_id or self._session_id
        return self._store.search(query, limit=max(1, min(20, requested)),
                                  session_id=session, scope=scope, kinds=kinds,
                                  as_of=as_of, expand=expand,
                                  include_superseded=include_superseded)


def _skill_description(path: Path) -> str:
    """The frontmatter ``description`` of a bundled skill, without a YAML dependency."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "description":
            return value.strip().strip("\"'")
    return ""


def _register_skills(ctx) -> None:
    """Publish every bundled ``skills/<name>/SKILL.md`` as ``minir:<name>`` (best effort)."""
    register_skill = getattr(ctx, "register_skill", None)
    if not callable(register_skill):
        return
    root = Path(__file__).resolve().parent / "skills"
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        skill_md = child / "SKILL.md"
        if not (child.is_dir() and skill_md.is_file()):
            continue
        try:
            register_skill(child.name, skill_md, description=_skill_description(skill_md))
        except Exception as exc:  # a skill must never fail the provider registration
            logger.warning("Minir skill %s registration failed: %s", child.name, exc)


def register(ctx) -> None:
    """Entry point the Hermes loader calls: the provider plus its plugin-hook observers.

    One directory serves **two** surfaces — ``memory.provider: minir`` and ``context.engine:
    minir`` — but each loader imports it as its own module instance and only *this* call runs in
    both. It therefore registers the memory provider and nothing else: the context engine is
    picked up by the engine loader as the module-level :class:`MinirContextEngine` subclass below,
    so a provider install never claims the (single, user-selected) context-engine slot. See
    ``context_engine.py``.

    The counting loop itself lives on the *memory-provider* surface (``on_turn_start`` is
    the only hook the host hands a per-session turn number, and it is never fired for a
    subagent or cron turn). The plugin hooks below cannot carry it — the host runs the
    hot ones (``pre/post_llm_call``, ``post_tool_call``) under a 30s timeout that *abandons*
    a slow callback and suppresses it for 60s, and ``on_session_end`` there is a per-turn
    event, not a session boundary — so they are used for what they are: session identity
    and per-turn logs.
    """
    provider = MinirMemoryProvider()
    ctx.register_memory_provider(provider)
    # The plugin's own toolset: the four minir_* tools registered under ``minir`` so any agent
    # this plugin runs can be granted them (the provider surface alone only reaches the main
    # agent's ``memory`` toolset). The lookup is per call, because the host may initialize the
    # other instance it constructed.
    try:
        plugin_tools.register(ctx, provider, resolve=lambda: _resolve_provider(provider))
    except Exception as exc:
        logger.warning("Minir toolset registration failed: %s", exc)
    # The bundled skills (read-only, namespaced ``minir:<name>``) travel with the plugin.
    _register_skills(ctx)
    # ``/adviser`` is registered by the sibling ``minir-adviser`` plugin, not here. The host
    # refuses an exclusive plugin at discovery, so this module is imported only when the provider
    # loads — a command registered here is unknown for the whole window between process start and
    # the first turn. That plugin loads at discovery and answers through ``_minir_adviser_command``
    # below, which is why the switch stays a provider attribute.
    # The hooks close over the provider instance: only it knows the bound session, the agent
    # context (primary vs subagent) and the cached review. Module-level handlers with no state
    # would inject another session's review or start a model job for every subagent.
    ctx.register_hook("on_session_start", _hook(_on_session_start, provider))
    ctx.register_hook("on_session_reset", _hook(_on_session_reset, provider))
    ctx.register_hook("on_session_finalize", _hook(_on_session_finalize, provider))
    ctx.register_hook("pre_llm_call", _hook(_on_pre_llm_call, provider))
    ctx.register_hook("post_llm_call", _hook(_on_post_llm_call, provider))
    ctx.register_hook("post_tool_call", _hook(_on_post_tool_call, provider))
    # ``adviser_only`` is enforced before execution; returning action=block wins the gate.
    # The callback declares a longer budget for the synchronous merge gate; host patch 0019 is
    # what reads it, and without the patch the attribute is inert (the global timeout applies).
    pre_tool_call = _hook(_on_pre_tool_call, provider)
    pre_tool_call.__hook_timeout__ = _GATE_CALLBACK_BUDGET
    ctx.register_hook("pre_tool_call", pre_tool_call)
    # Result distillation: additive and fail-open, so a timeout there just keeps the original.
    ctx.register_hook("transform_tool_result", _hook(_on_transform_tool_result, provider))
    # Verify-on-stop gate: a live blocker may keep a code-editing turn going once.
    ctx.register_hook("pre_verify", _hook(_on_pre_verify, provider))
    # Final-answer injection: the host hands the rendered answer here, the last point where a
    # queued block (the todo table) can still join it.
    ctx.register_hook("transform_llm_output", _hook(_on_transform_llm_output, provider))
    # Adviser mode observers: the event ring needs the assistant's intent (interim text and
    # successful API responses with tool calls), the tool outcome, and the final answer.
    ctx.register_hook("on_interim_message", _hook(_on_interim_message, provider))
    ctx.register_hook("on_stream_delta", _hook(_on_stream_delta, provider))
    ctx.register_hook("post_api_request", _hook(_on_post_api_request, provider))
    ctx.register_hook("api_request_error", _hook(_on_api_request_error, provider))
    # Adviser mode's mid-loop delivery seam. The memory-provider context forwards register_* to
    # a real PluginContext; a bare test/older host may not expose it, so registration is optional.
    register_middleware = getattr(ctx, "register_middleware", None)
    if callable(register_middleware):
        try:
            register_middleware("llm_request", _hook(_on_llm_request, provider))
        except Exception as exc:
            logger.warning("Minir adviser-mode middleware registration failed: %s", exc)
    # The engine loader also calls register() with a Noop collector. That collector has no
    # auxiliary-task API; the adviser surface belongs to the memory-plugin collector only.
    if not hasattr(ctx, "register_auxiliary_task"):
        return
    # Second surface of the same directory: the auxiliary ``adviser`` route and the read-only
    # host session search. Optional by design — a host without the auxiliary-task
    # API (or a config that refuses the key) must still get the memory provider, so a failure
    # here is logged and swallowed rather than raising out of ``register()``.
    try:
        adviser.register(ctx, provider)
    except Exception as exc:
        logger.warning("Minir adviser registration failed: %s", exc)


# -- plugin hooks -------------------------------------------------------------
# Handlers must return None: ``pre_llm_call`` injects a returned string into the user
# message, and the host sends a non-empty ``on_session_finalize`` result to the chat.

def _resolve_provider(provider: Optional[MinirMemoryProvider]) -> Optional[MinirMemoryProvider]:
    """Prefer the initialized instance the host is driving over a hook's own construction."""
    if provider is not None and getattr(provider, "_store", None) is not None:
        return provider
    active = _ACTIVE_PROVIDER
    return active if active is not None and active._store is not None else provider


def _minir_adviser_command(raw_args: str, *, session_id: str = "",
                           deliver: Optional[Callable[[str], bool]] = None) -> Optional[str]:
    """Cross-plugin seam for the ``minir-adviser`` plugin's ``/adviser`` command.

    That plugin is its own directory, imported as its own package, so it cannot import this
    module. It finds this function by scanning ``sys.modules`` for the name: keep the name and
    the signature stable, they are the contract between the two directories.

    Returns the line to print, or None while no provider is initialized. The mode switch lives on
    the provider (one instance per import, and the hook reads the initialized one), so before the
    first turn there is no state to switch and the caller has to answer for itself.
    """
    provider = _ACTIVE_PROVIDER
    if provider is None or getattr(provider, "_store", None) is None:
        return None
    return adviser_command.command_handler(lambda: provider, inject=deliver)(
        raw_args, session_id=session_id)


def _minir_adviser_verb(raw_args: str) -> str:
    """Which verb the arguments name: ``arm`` / ``clear`` / ``status`` / ``unknown``.

    Lives here so the companion plugin keeps no copy of the accepted spellings: before a provider
    is initialized it still has to tell "switch to hermes-lead" (defer and start the turn) from
    "report" (answer now).
    """
    return adviser_command.parse(raw_args)[0]


def _minir_adviser_kickoff() -> str:
    """The ready-check request the command injects when it arms.

    The companion needs it in the window before a provider exists: choosing the mode and loading
    the skill both happen on that injected turn, where the provider initializes.
    """
    return adviser_command.KICKOFF_TEXT


def _hook(handler, provider: MinirMemoryProvider):
    """Bind a plugin hook/middleware handler to this provider.

    Some host payloads (``llm_request``, ``post_api_request``) carry their own ``provider``
    key. Passing it through together with the closure's provider raises ``TypeError: got
    multiple values for keyword argument 'provider'``, and the host then drops the callback —
    adviser advice is never injected and post-request intent is never recorded. The host's
    object is not the memory provider this plugin binds to, so discard it.
    """
    def callback(**kwargs):
        kwargs.pop("provider", None)
        return handler(provider=provider, **kwargs)
    return callback


def _on_session_start(session_id: str = "", model: str = "", platform: str = "",
                      provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    """Record the session, then queue one cold-start dialectic job when it is primary.

    The hook also fires for subagent/cron sessions. ``provider.cold_start`` rejects anything
    that is not the provider's bound primary session, so a delegate does not spawn a model job.
    """
    try:
        TRACKER.begin(session_id)
        resolved = _resolve_provider(provider)
        if resolved is not None:
            resolved._adviser_mode.session_started(session_id, platform=platform)
            resolved.cold_start(session_id)
        logger.info("Minir on_session_start: session=%s model=%s platform=%s",
                    session_id or "-", model or "-", platform or "-")
    except Exception as exc:
        logger.warning("Minir on_session_start failed: %s", exc)


def _on_session_reset(session_id: str = "", old_session_id: str = "",
                      reason: str = "", provider: Optional[MinirMemoryProvider] = None,
                      **kwargs) -> None:
    """``/new``/``/reset``: stop the old counters, stamp the mode onto the new session.

    The provider's ``on_session_switch(reset=True)`` already ran (the host fires it first),
    so this is the idempotent second half: ``begin`` never resets progress it did not create.
    """
    try:
        resolved = _resolve_provider(provider)
        modes = getattr(resolved, "_modes", None) or adviser_command.MODES
        if old_session_id:
            TRACKER.stop(old_session_id)
            # A mode belongs to the conversation that armed it; the new session starts in dispatch.
            modes.clear(old_session_id)
        modes.clear(session_id)
        TRACKER.begin(session_id)
        if resolved is not None and old_session_id:
            # The session that ended is the old one; the new one has no calibration yet.
            resolved.publish_base_conclusions(old_session_id)
        logger.info("Minir on_session_reset: %s -> %s (reason=%s)",
                    old_session_id or "-", session_id or "-", reason or "-")
    except Exception as exc:
        logger.warning("Minir on_session_reset failed: %s", exc)


def _on_session_finalize(session_id: str = "", provider: Optional[MinirMemoryProvider] = None,
                         **kwargs) -> None:
    """Process teardown for one session: stop counting it. Returns nothing on purpose."""
    try:
        TRACKER.stop(session_id)
        resolved = _resolve_provider(provider)
        (getattr(resolved, "_modes", None) or adviser_command.MODES).clear(session_id)
        if resolved is not None:
            resolved.publish_base_conclusions(session_id)
            resolved._adviser_mode.session_end(session_id)
            resolved.flush_metrics()
            # Bounded flush: the host is tearing this session down, so the queued rows must
            # reach the file, but a slow disk must not hold the process.
            resolved._transcript.flush(session_id, timeout=5.0)
        logger.info("Minir on_session_finalize: session=%s", session_id or "-")
    except Exception as exc:
        logger.warning("Minir on_session_finalize failed: %s", exc)


def _on_transform_llm_output(response_text: str = "", session_id: str = "",
                             provider: Optional[MinirMemoryProvider] = None,
                             **kwargs) -> Optional[str]:
    """Append the session's queued blocks to the final answer; ``None`` keeps the text as is.

    The queue is popped before either check: the host calls this once per turn, and a block
    queued for this answer must not survive into the next one even when the model produced no
    text. Returning the block alone would delete the model's answer, so the text always leads.
    """
    resolved = _resolve_provider(provider)
    if resolved is None or resolved._store is None:
        return None
    try:
        block = resolved._take_injects(session_id or resolved._session_id)
    except Exception as exc:  # an injection must never break the answer
        logger.warning("Minir final-answer injection failed: %s", exc)
        return None
    if not block or not str(response_text or "").strip():
        return None
    return response_text.rstrip() + "\n\n" + block


def _on_pre_llm_call(session_id: str = "", turn_id: str = "",
                     provider: Optional[MinirMemoryProvider] = None,
                     **kwargs) -> Optional[str]:
    """Inject only the ``adviser_only`` policy brief.

    Returning a string is the host's injection channel. The brief is session-scoped and skipped
    for a child session or a provider bound elsewhere. Dialectic advices are candidates for the
    main adviser now, never boundary injections.
    """
    logger.info("Minir pre_llm_call: session=%s turn=%s", session_id or "-", turn_id or "-")
    provider = _resolve_provider(provider)
    if provider is None or provider._store is None:
        return None
    try:
        if kwargs.get("parent_session_id"):
            return None
        # A watched background run is observed under its own session id, so the recorder call
        # and the ``adviser_only`` brief below are what this adapter is here for.
        watched = bool(session_id) and agent_watch.is_child(session_id)
        if (session_id and provider._session_id and session_id != provider._session_id
                and not watched):
            return None
        # The adviser-mode event ring needs the user's goal for this turn; the advice delivery
        # below is the existing turn-boundary channel (both sources share the ledger).
        provider._adviser_mode.on_pre_llm_call(
            session_id=session_id, turn_id=turn_id,
            user_message=kwargs.get("user_message"))
        # ``adviser_only`` is a policy mode, not a different reviewer: the brief tells the main
        # agent to propose changes instead of executing them, and ``pre_tool_call`` enforces it.
        # Dialectic advices are candidates for the main adviser now; they are never injected at
        # the turn boundary. The legacy review fallback is retired with the same cutover.
        brief = provider._adviser_mode.mode_brief(session_id)
        # Turn-boundary channel: advice below the mid-turn floor (nits) rides this turn's user
        # message once. It is marked injected, so the next boundary does not repeat it.
        aside = provider._adviser_mode.aside_block(session_id)
        # The orchestration mode leads: ``/adviser multagents`` armed it in an earlier turn, and
        # this block is what tells the model which contract the rest of the message serves.
        modes = getattr(provider, "_modes", None) or adviser_command.MODES
        mode = modes.directive(session_id or provider._session_id)
        return "\n\n".join(part for part in (mode, brief, aside) if part) or None
    except Exception as exc:  # injection must never break a turn
        logger.debug("Minir review injection skipped: %s", exc)
        return None


def _on_post_llm_call(session_id: str = "", turn_id: str = "", model: str = "",
                      provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    logger.info("Minir post_llm_call: session=%s turn=%s model=%s",
                session_id or "-", turn_id or "-", model or "-")
    resolved = _resolve_provider(provider)
    if resolved is not None:
        resolved._adviser_mode.on_post_llm_call(
            session_id=session_id, turn_id=turn_id,
            assistant_response=kwargs.get("assistant_response"))


def _on_post_tool_call(tool_name: str = "", session_id: str = "",
                       duration_ms: Any = None,
                       provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    logger.info("Minir post_tool_call: %s in %sms (session=%s)",
                tool_name or "-", duration_ms, session_id or "-")
    resolved = _resolve_provider(provider)
    if resolved is not None:
        resolved.record_metric(tool_name, duration_ms,
                               error=bool(kwargs.get("error") or kwargs.get("is_error")))
        # The retry guard needs the failure, not the advice ring: fed before the adviser call so
        # it also counts when adviser mode would return early.
        resolved._fork.observe_tool(
            session_id, tool_name, kwargs.get("args"),
            error=kwargs.get("error_message") or ("" if not (
                kwargs.get("error") or kwargs.get("is_error")) else "failed"))
        resolved._adviser_mode.on_post_tool_call(
            session_id=session_id, turn_id=str(kwargs.get("turn_id") or ""),
            tool_name=tool_name, status=str(kwargs.get("status") or ""),
            args=kwargs.get("args"), result=kwargs.get("result"),
            error_message=str(kwargs.get("error_message") or ""),
            error=kwargs.get("error"), is_error=kwargs.get("is_error"))


def _on_transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None,
                              session_id: str = "", status: str = "",
                              provider: Optional[MinirMemoryProvider] = None,
                              **kwargs) -> Optional[str]:
    """Distill an oversized result when a compress rule covers this call.

    ``transform_tool_result`` is fail-open (a timeout drops the replacement and keeps the
    original), so this path may do arithmetic but must never be the thing a decision depends on.
    """
    resolved = _resolve_provider(provider)
    if resolved is None:
        return None
    try:
        return resolved._fork.transform(
            session_id or resolved._session_id, tool_name, args, str(result or ""),
            status=status)
    except Exception as exc:  # a broken replacement must never cost the tool result
        logger.warning("Minir transform_tool_result failed: %s", exc)
        return None


def _on_pre_tool_call(tool_name: str = "", session_id: str = "",
                      provider: Optional[MinirMemoryProvider] = None,
                      **kwargs) -> Optional[dict[str, Any]]:
    """``adviser_only`` enforcement, the operator gate, then the fork decisions.

    Order is priority: a scope refusal outranks the operator claim gate, which outranks the
    retry guard, which outranks the integration gate, which outranks a rule rewrite. A rule can
    never block (``normalize_rule`` drops ``block``), and a learned rule can never reach the
    ``block`` branch — only the two fixed guards and the operator-authorized gate can.
    """
    resolved = _resolve_provider(provider)
    if resolved is None:
        return None
    args = kwargs.get("args")
    if file_guard.aux_active():
        reason = file_guard.check(tool_name, args or {}, resolved._hermes_home)
        if reason:
            return {"action": "block", "message": reason}
    reason = resolved._adviser_mode.block_reason(session_id, tool_name, args=args)
    if reason:
        return {"action": "block", "message": reason}
    fork = resolved._fork
    guard = fork.retry_guard(tool_name, args, session=session_id)
    if guard is not None:
        return {"action": "block", "message": f"jev: {guard.reason}"}
    if fork.is_integration(tool_name, args):
        gate = fork.resolve_gate(session_id, tool_name, args)
        if gate is not None:
            return {"action": "block", "message": f"jev gate: {gate.reason}"}
    rewrite = fork.resolve_now("arg", tool_name, args=args, hook="pre_tool", session=session_id)
    if rewrite is not None and rewrite.decision == "rewrite":
        patch = dict(rewrite.payload.get("args") or {})
        if patch:
            # The host shallow-merges these keys into the original arguments.
            return {"action": "modify", "args": patch}
    return None


def _on_pre_verify(session_id: str = "", changed_paths: Any = None,
                   provider: Optional[MinirMemoryProvider] = None, **kwargs) -> Optional[dict]:
    """Keep a code-editing turn going once when a live blocker is pending.

    Opt-in (``adviser_mode.verify_continue``). The host bounds the number of nudges; this
    adapter additionally consumes the blocker, so one advice produces at most one continue.
    """
    resolved = _resolve_provider(provider)
    if resolved is None:
        return None
    return resolved._adviser_mode.verify_continue(
        session_id, changed_paths or kwargs.get("changed_paths"))


def _on_interim_message(session_id: str = "", turn_id: str = "", text: Any = None,
                        iteration: Any = None,
                        provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    """Record the assistant's mid-loop intent (the reviewer's missing half of a tool event)."""
    resolved = _resolve_provider(provider)
    if resolved is not None:
        resolved._adviser_mode.on_interim_message(
            session_id=session_id, turn_id=turn_id, text=text, iteration=iteration)


def _on_stream_delta(session_id: str = "", turn_id: str = "", delta: Any = None,
                     kind: str = "text", provider: Optional[MinirMemoryProvider] = None,
                     **kwargs) -> None:
    """Keep a bounded reasoning excerpt and, at turn end, one reasoning event row.

    The host only delivers reasoning deltas when ``plugins.stream_reasoning_deltas`` is on, and
    the plugin's own ``include_reasoning`` gates the review prompt. The transcript row is the
    permanent record; the excerpt is only ever a prompt input.
    """
    if str(kind or "") != "reasoning":
        return
    resolved = _resolve_provider(provider)
    if resolved is not None:
        resolved._adviser_mode.on_stream_delta(session_id, str(delta or ""))
        key = str(session_id or "")
        parts = resolved._reasoning_deltas.setdefault(key, [])
        parts.append(str(delta or ""))
        total = sum(len(part) for part in parts)
        while parts and total > 200_000:  # keep the tail, never the whole thought
            total -= len(parts.pop(0))


def _on_post_api_request(session_id: str = "", turn_id: str = "", api_request_id: str = "",
                         assistant_message: Any = None, assistant_tool_call_count: Any = 0,
                         usage: Any = None, model: str = "",
                         provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    """Record intent, confirm delivery, and fold this request's token usage into its turn."""
    resolved = _resolve_provider(provider)
    if resolved is not None:
        # One hook call is one API request; the payload's api_call_count is the turn's running
        # counter, not a delta, so adding it would square-count a multi-call turn.
        resolved.record_api_usage(session_id, turn_id, usage, api_calls=1, model=model)
        resolved._adviser_mode.on_post_api_request(
            session_id=session_id, turn_id=turn_id, api_request_id=api_request_id,
            assistant_message=assistant_message,
            assistant_tool_call_count=assistant_tool_call_count)


def _on_api_request_error(session_id: str = "", api_request_id: str = "",
                          provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    """A failed attempt did not deliver its advice: return staged rows to pending."""
    resolved = _resolve_provider(provider)
    if resolved is not None:
        resolved._adviser_mode.on_api_request_error(
            session_id=session_id, api_request_id=api_request_id)


def _on_llm_request(request: Any = None, provider: Optional[MinirMemoryProvider] = None,
                    **kwargs) -> Optional[dict[str, Any]]:
    """``llm_request`` middleware: the only mid-tool-loop advice injection seam in Hermes."""
    resolved = _resolve_provider(provider)
    if resolved is None:
        return None
    return resolved._adviser_mode.handle_llm_request(request=request, **kwargs)
