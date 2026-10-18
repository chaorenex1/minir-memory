"""Minir memory plugin — a minimal Hermes memory-provider template.

A complete, dependency-free reference implementation of the ``MemoryProvider``
contract (``agent/memory_provider.py``): explicit notes go into a per-profile SQLite
database, recall is a keyword search over them, and four tools let the model search,
remember, forget, and inspect it.

**No transcripts are stored.** The layers read the host's own ``state.db`` (plus an in-memory
buffer for uncommitted turns); what reaches this provider's disk is what the base / dialectic
layers *derived* (``layer_state`` and the FTS-indexed ``conclusions``) plus deliberate notes.
Because the host store is the source of truth, a job queued before a restart can still be
completed; inherited jobs are expired only when the profile has no ``state.db``.

Fork this as a starting point:

* ``register(ctx)`` plus the lifecycle methods below are the contract surface.
* ``store.py`` is the storage layer — swap it for your backend (API client,
  vector index, knowledge graph) and keep this module's shape.
* ``turn_counter.py`` holds the base / dialectic-reasoning layer counters that turn
  ``on_turn_start`` ticks into queued jobs (a session + the time window to reason
  over); ``worker.py`` drains those jobs into merged per-layer artifacts that
  ``prefetch()`` injects. Delete the worker and the two layers become pure bookkeeping.
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
import threading
import time
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import (
    INDICATOR_GLYPH,
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)
from tools.registry import tool_error

from . import adviser
from . import history as host_history
from .adviser_mode import AdviserMode
from .adviser_prompts import MAIN_CONSULT_SECTION
from .prompting import (
    advice_handling_section,
    advice_language_line,
    advice_nit_report_section,
    advice_severity_section,
    advice_target_section,
    craft_rules_section,
    marker_glossary,
)
from .store import VALUE_MARKER_TYPES, MinirStore, format_when
from .transcript import TranscriptBuffer
from .turn_counter import (
    BASE_LAYER,
    DEFAULT_BASE_INTERVAL,
    DEFAULT_DIALECTIC_INTERVAL,
    DEFAULT_SESSION_MODE,
    DIALECTIC_LAYER,
    TRACKER,
    Trigger,
)
from .worker import LayerWorker, previous_content, project_artifact
# Imported (not just defined) so the engine loader's subclass scan finds it on this module: the
# engine surface is resolved by ``issubclass(ContextEngine)`` against the package's attributes.
from .context_engine import MinirContextEngine

#: ``MinirContextEngine`` is exported for the context-engine loader's subclass scan (and is
#: otherwise unused here, which is exactly why it is listed).
__all__ = ["MinirContextEngine", "MinirMemoryProvider", "register"]

logger = logging.getLogger(__name__)

_PROVIDER_LABEL = "Minir"
_STORE_DIRNAME = "minir-memory"   # under $HERMES_HOME
_DB_FILENAME = "memory.db"
_DEFAULT_PREFETCH_LIMIT = 5
_MAX_BLOCK_CHARS = 2000           # recall block injected into the prompt is truncated to this
_MAX_LAYER_CHARS = 1200           # layer artifacts injected next to it are capped separately
_MAX_TOOL_EXCERPT = 600           # text excerpt returned by the minir_search tool
_MAX_AUTHOR_CACHE = 64            # per-session last-seen author, bounded like the turn counter
_COMPRESSION_GATE_TTL = 300.0     # seconds a crashed compression may pause periodic scheduling
_PRE_COMPRESS_TIMEOUT = 120.0     # synchronous base -> dialectic budget before compress()
# Prefetch injects explicit memory only. Every derived artifact — the six base sections, the
# dialectic review (injected separately by ``_layer_block``), chunk summaries and compression
# summaries — stays explicit-recall through ``minir_search``.
_PREFETCH_KINDS = frozenset({"note"})

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


class MinirMemoryProvider(MemoryProvider):
    """Local-first memory: notes + layer conclusions in SQLite, FTS recall, no transcripts."""

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
        # Layer reasoning: the worker calls the host's auxiliary model to turn each queued job
        # into a layer artifact. On by default (the layers are the point of the provider);
        # ``reasoning_enabled: false`` keeps minir a pure store with no model calls at all.
        self._reasoning = _coerce_bool(
            os.environ.get("MINIR_REASONING_ENABLED"),
            _coerce_bool(self._config.get("reasoning_enabled"), True))

        self._store: Optional[MinirStore] = None
        self._session_id = ""
        self._agent_context = "primary"
        self._user_id = ""
        self._agent_identity = ""
        self._hermes_home = ""
        self._state_db = ""
        self._profile = "default"
        self._identity = ""
        self._preference = ""
        self._worker: Optional[LayerWorker] = None
        self._transcript = TranscriptBuffer()
        self._authors: dict[str, dict[str, Any]] = {}   # session -> last author seen on a turn
        self._lock = threading.Lock()              # guards the recall cache below
        self._cached: Optional[tuple[str, str, int]] = None  # (session_id, block, count)
        self._last_recall: Optional[RecallStatus] = None
        self._prefetch_thread: Optional[threading.Thread] = None
        self._metrics: dict[str, dict[str, Any]] = {}
        self._metrics_seen = 0
        self._adviser_consult_registered = False  # set by register() on the real host
        # Per-turn side channel: event ring + reviewer worker + llm_request delivery. Constructed
        # before initialize (the host may register hooks first); every path guards on the store.
        self._adviser_mode = AdviserMode(self)

    # -- contract -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "minir"

    def is_available(self) -> bool:
        """No credentials or external binaries required — always ready."""
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Open the store. ``hermes_home`` is profile-scoped and must be used as given."""
        hermes_home = kwargs.get("hermes_home") or self._fallback_home()
        self._hermes_home = str(hermes_home)
        self._state_db = str(Path(hermes_home) / "state.db")
        db_path = Path(hermes_home) / _STORE_DIRNAME / _DB_FILENAME
        self._store = MinirStore(db_path)
        global _ACTIVE_PROVIDER
        _ACTIVE_PROVIDER = self
        self._session_id = session_id or ""
        self._adviser_mode.session_started(self._session_id)
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        # Identity the host hands us: drives which subject each layer's artifact belongs to.
        self._user_id = str(kwargs.get("user_id") or "")
        self._agent_identity = str(kwargs.get("agent_identity") or "")
        self._profile = (host_history.profile_name(self._state_db, self._session_id)
                         or str(kwargs.get("profile") or kwargs.get("profile_name") or "default"))
        self._identity, self._preference = host_history.load_identity(self._hermes_home, self._profile)
        self._worker = LayerWorker(
            self._store, self._transcript, enabled=self._reasoning,
            agent_identity=self._agent_identity, user_id=self._user_id,
            state_db=self._state_db, identity=self._identity, preference=self._preference,
            depth=self._dialectic_depth)
        # Configure before opening the session: intervals/mode apply to sessions opened later
        # (an already-running session keeps the cadence it started with).
        TRACKER.configure(base_interval=self._base_interval,
                          dialectic_interval=self._dialectic_interval,
                          default_mode=self._session_mode)
        TRACKER.begin(self._session_id, mode=self._session_mode)
        with self._lock:
            self._cached = None
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
            {"key": "handoff_enabled",
             "description": "Let compression call the Task Handoff summary model",
             "type": "boolean", "default": "true", "choices": ["true", "false"],
             "env_var": "MINIR_HANDOFF_ENABLED"},
            {"key": "session_mode", "description": "Mode stamped on each newly opened session",
             "type": "text", "default": DEFAULT_SESSION_MODE, "env_var": "MINIR_SESSION_MODE"},
        ]

    # -- prompt + recall ------------------------------------------------------

    def system_prompt_block(self) -> str:
        """Session-static prompt text: memory, craft, and the gated adviser contract."""
        logger.info("Minir system_prompt_block (built once per session)")
        return "\n\n".join(part for part in (
            self._memory_prompt_block(), self._craft_prompt_block(),
            self._adviser_prompt_block()) if part)

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
            "searches the current session by default; scope=\"all\" searches every session. "
            "Search before asking the user to repeat history.\n"
            "- minir_remember: record one durable item with type (entities, trajectories, "
            "experiences, tools or projects) and content. Store what should outlive this turn, "
            "not transient state, and never store secrets or credentials.\n"
            "- minir_forget: delete one typed marker by note_id and type.\n"
            "- minir_stats: inspect the store and the published layer artifacts.\n"
            "\nWORKFLOW\n"
            "1. Search before asking the user to repeat history.\n"
            "2. Answer from the current conversation; reach for memory only to fill a gap or to "
            "avoid re-litigating a settled decision.\n"
            "3. When a durable fact, correction or lesson appears, store it once under the type "
            "that matches its definition.\n"
            "4. When a stored item is superseded or wrong, delete it with minir_forget rather "
            "than storing a contradiction."
        )

    @staticmethod
    def _craft_prompt_block() -> str:
        """Static craft contract; emitted regardless of adviser_mode and consult gating."""
        return craft_rules_section()

    def _adviser_prompt_block(self) -> str:
        """Dynamic adviser contract, gated on the configured/registered surfaces."""
        try:
            enabled = self._adviser_mode.enabled(self._session_id)
            adviser_only = enabled and self._adviser_mode.mode(self._session_id) == "adviser_only"
            consult = bool(self._adviser_consult_registered)
        except Exception:
            return ""
        if not (enabled or consult):
            return ""
        parts = [
            "# Advisers",
            "Advice from the main adviser is advice, never authority. You own the decision and "
            "the verification.",
        ]
        if enabled:
            parts.extend([
                "Live advice may arrive mid-turn as:\n"
                "- `[adviser — advisory only] ... [end adviser]`\n"
                "The adviser sees events, not your full brief. Do not restate or thank it.",
                advice_severity_section(),
                advice_target_section(heading="ADVICE TARGETS", grouped=True),
                advice_handling_section(),
                advice_nit_report_section(),
                advice_language_line(),
                "Dialectic candidates are internal to the adviser and never injected into your "
                "context. Do not wait for a turn-boundary advice block.",
            ])
        if adviser_only:
            brief = self._adviser_mode.mode_brief(self._session_id)
            if brief:
                parts.append(brief)
        if consult:
            parts.append(MAIN_CONSULT_SECTION)
        return "\n\n".join(parts)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Run recall in the background at turn end; ``prefetch()`` consumes it next turn."""
        if self._store is None or not (query or "").strip():
            return
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2.0)  # don't pile up threads on rapid turns
            if self._prefetch_thread.is_alive():
                logger.debug("Minir prefetch still running; skipping this batch")
                return
        session = session_id or self._session_id

        def _work() -> None:
            try:
                rows = self._search(query, session_id=session, kinds=_PREFETCH_KINDS)
                block = self._format_block(rows)
                with self._lock:
                    self._cached = (session, block, len(rows))
            except Exception as exc:  # a background recall must never surface anywhere
                logger.debug("Minir background recall failed: %s", exc)

        self._prefetch_thread = spawn_context_thread(_work, name="minir-prefetch")
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Formatted recall for the upcoming turn. Must stay fast.

        The host calls this on the critical path before the LLM request: consume
        the queued result when it matches, otherwise fall back to a bounded
        inline search (cheap for local SQLite). A remote backend should return
        only the cached block here and do all I/O in ``queue_prefetch``.

        The dialectic artifact is read live (one indexed row, no model call) and prepended:
        it is what the agent layer last concluded about how this assistant should work, which
        is rarely what a keyword query would have surfaced. No base-layer section is injected
        here; every base artifact stays recallable through ``minir_search``.

        Recall covers explicit notes and value markers only: the current session's notes plus
        durable notes. Every derived artifact (base sections, review, chunks, summaries) is
        explicit-recall through ``minir_search``, and another session's rows are reachable only
        by an explicit ``minir_search(scope="all")`` call — never by automatic injection.
        """
        session = session_id or self._session_id
        block, count = "", 0
        cached = self._take_cached(session)
        if cached is not None:
            block, count = cached
        elif self._store is not None and (query or "").strip() and not is_trivial_prompt(query):
            try:
                rows = self._search(query, session_id=session, kinds=_PREFETCH_KINDS)
                block, count = self._format_block(rows), len(rows)
            except Exception as exc:
                logger.debug("Minir prefetch search failed: %s", exc)
        state, state_items = self._layer_block(session)
        if state:
            block = f"{state}\n\n{block}" if block else state
            count += state_items
        self._last_recall = RecallStatus(provider_label=_PROVIDER_LABEL, count=count, glyph=INDICATOR_GLYPH) \
            if block else None
        return block

    def _layer_block(self, session: str) -> tuple[str, int]:
        """Last published dialectic artifact as one block; counts its bullet items.

        The base artifact is deliberately absent: no base section is injected into the prompt,
        only the agent's own working model is. Base artifacts remain reachable through
        ``minir_search``.
        """
        if self._store is None or self._worker is None:
            return "", 0
        try:
            wanted = (
                ("Agent model", DIALECTIC_LAYER, self._worker.dialectic_subject()),
            )
            lines: list[str] = []
            size = items = 0
            for title, layer, subject in wanted:
                state = self._store.layer_state(layer, subject) or self._store.newest_layer_state(layer)
                # An artifact published by another session loses its review, or the next turn
                # would follow stale advice. Only the current session's review is projected.
                body = project_artifact(layer, previous_content(layer, state, session)).strip()
                if not body:
                    continue
                chunk = f"### {title} (v{(state or {}).get('version', 0)})\n{body}"
                if size + len(chunk) > _MAX_LAYER_CHARS:
                    if not lines:
                        chunk = chunk[:_MAX_LAYER_CHARS]
                    else:
                        continue
                size += len(chunk)
                items += sum(1 for line in chunk.splitlines() if line.lstrip().startswith("-"))
                lines.append(chunk)
            if not lines:
                return "", 0
            return f"## {_PROVIDER_LABEL} layer state\n" + "\n".join(lines), items
        except Exception as exc:  # injection must never break a turn
            logger.debug("Minir layer state read failed: %s", exc)
            return "", 0

    def recall_status(self) -> Optional[RecallStatus]:
        """What the last ``prefetch()`` injected — never a stale prior count."""
        return self._last_recall

    # -- persistence ----------------------------------------------------------

    def sync_turn(
        self, user_content: str, assistant_content: str, *,
        session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        """Hand the completed turn to the layer worker (host calls this on a background worker).

        Nothing is written to disk: the turn goes into the in-memory transcript, and only what
        the layers *derive* from it is persisted. ``turn_author`` (``{"id", "name", "is_bot"}``)
        is retained as evidence metadata for the user side; the base layer always models this
        profile's single user, not a per-author artifact. The tick's cached author is the
        fallback for a host that does not pass it here.
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
        try:
            self._transcript.add_turn(session, user_content, assistant_content,
                                      author=author, author_id=author_id)
            # The turn's content exists now, so this is the earliest useful moment to drain the
            # jobs the tick queued for it (the worker coalesces and is a daemon thread).
            self._kick_worker()
        except Exception as exc:  # a memory failure must never break the host's turn worker
            logger.warning("Minir sync_turn failed: %s", exc)

    def _turn_author(self, turn_author: Optional[dict[str, Any]], session: str) -> tuple[str, str]:
        """``(name, id)`` of the user side of this turn: the host's argument, else the last tick."""
        source = turn_author if isinstance(turn_author, dict) and turn_author else self._authors.get(session) or {}
        return str(source.get("name") or "").strip(), str(source.get("id") or "").strip()

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[dict[str, Any]] = None) -> None:
        """Mirror built-in memory-tool writes so both stores stay searchable."""
        logger.info("Minir on_memory_write: action=%s target=%s chars=%d",
                    action, target, len(content or ""))
        if self._store is None or not (content or "").strip():
            return
        try:
            if action in {"add", "replace"}:
                self._store.remember(content.strip(), source=f"memory_tool/{target}")
            elif action == "remove":
                # ``metadata["previous_content"]`` may be absent; content itself is the entry.
                self._store.forget_by_content(content.strip())
        except Exception as exc:
            logger.debug("Minir memory mirror failed: %s", exc)

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
            if self._store.compression_gate_active(self._session_id):
                return  # compression owns this window; periodic dispatch resumes afterwards
            self._dispatch(TRACKER.tick(self._session_id, turn_number))
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
            turns = int((snapshot.get("layers") or {}).get(BASE_LAYER, {}).get("turns", 0))
            if now > start:
                self._store.record_job(BASE_LAYER, session_id, start, now, turns=turns)
            if self._worker is not None:
                self._worker.drain_session(session_id, timeout=_PRE_COMPRESS_TIMEOUT)
                self._kick_worker()  # let a timed-out drain finish in the background
            TRACKER.reset_layers(session_id)
        except Exception as exc:
            self._store.clear_compression_gate(session_id)
            logger.warning("Minir pre-compression layer refresh failed: %s", exc)

    def on_session_end(self, messages: Optional[list[dict[str, Any]]] = None) -> None:
        """Real session boundary: flush the un-reasoned tail once, then stop counting.

        The host queues this ahead of ``on_session_switch`` on one serialized worker, so the
        counter always reflects every turn of the ending session. ``tail()`` is watermark-based
        and therefore a no-op when the tail is already covered — a ``/new`` reaches this
        method, ``on_session_finalize`` and ``on_session_reset`` for the same boundary.
        """
        try:
            self._flush_boundary(self._session_id, close=True)
            self._adviser_mode.session_end(self._session_id)
        except Exception as exc:
            logger.warning("Minir on_session_end failed: %s", exc)

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
            pending.append(Trigger(layer=layer, session_id=session_id, from_ts=drained, to_ts=now,
                                   turns=int((counted.get(layer) or {}).get("turns", 0))))
        return pending

    def _dispatch(self, triggers: list[Trigger]) -> None:
        """Persist each due job (session + window) for the layer worker to pick up.

        Recording only — the drain is kicked where content exists (``sync_turn``, a boundary,
        shutdown). Draining straight from the tick (or from ``initialize``, which would race the
        first turn) hands the worker a window whose turn has not arrived yet and burns it empty.
        """
        if not triggers:
            return
        for trigger in triggers:
            if trigger.layer == DIALECTIC_LAYER:
                # Dialectic is a dependent job: the worker queues it with the exact window its
                # base job published. A tick/tail trigger here would race that base artifact.
                continue
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
            with self._lock:
                self._cached = None
            self._last_recall = None

    def shutdown(self) -> None:
        """Flush and close. Idempotent — the host may call it more than once."""
        with self._lock:
            self._cached = None
        if self._store is not None:
            try:  # a clean exit must not leave the last window unreasoned
                self._flush_boundary(self._session_id)
            except Exception as exc:
                logger.debug("Minir shutdown flush failed: %s", exc)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.kick(spawn=spawn_context_thread)
            worker.stop()  # bounded join: the store cannot close under a draining job
        self._adviser_mode.session_end(self._session_id)
        self._adviser_mode.stop()  # no review may touch the store after this point
        self.flush_metrics()
        # Only now that no drain is in flight: a queued boundary job must still find its window.
        self._transcript.clear()
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=10.0)
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
        return [_TOOL_SEARCH, _TOOL_REMEMBER, _TOOL_FORGET, _TOOL_STATS]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Dispatch one of this provider's tools. Must return a JSON string."""
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
            rows = self._search(query, limit=args.get("limit"),
                                session_id=str(args.get("session_id") or self._session_id),
                                scope=scope)
            results = []
            for row in rows:
                entry = {"kind": row["kind"], "when": format_when(row["when"]),
                         "source": row["author"], "session_id": row.get("session_id", ""),
                         "text": _excerpt(row["text"])}
                if row.get("marker_type"):
                    entry["marker_type"] = row["marker_type"]
                # Only a typed marker has a forgettable id. Never expose a conclusion id under
                # ``id``, and never advertise an untyped note (host mirror/legacy row) that
                # ``minir_forget`` cannot delete: the type check would reject it.
                if (row["kind"] == "note" and row.get("marker_type")
                        and row.get("note_id") is not None):
                    entry["note_id"] = row["note_id"]
                    entry["id"] = row["note_id"]
                results.append(entry)
            return json.dumps({"count": len(rows), "scope": scope, "results": results})
        if tool_name == "minir_remember":
            marker_type = str(args.get("type") or "").strip()
            if not marker_type:
                return tool_error("type is required")
            if marker_type not in VALUE_MARKER_TYPES:
                return tool_error(
                    f"type must be one of: {', '.join(VALUE_MARKER_TYPES)}")
            content = str(args.get("content") or "").strip()
            if not content:
                return tool_error("content is required")
            note_id = self._store.remember(
                content, source="tool", marker_type=marker_type, session_id=self._session_id)
            return json.dumps({"stored": True, "note_id": note_id, "type": marker_type,
                               "session_id": self._session_id})
        if tool_name == "minir_forget":
            marker_type = str(args.get("type") or "").strip()
            if not marker_type:
                return tool_error("type is required")
            if marker_type not in VALUE_MARKER_TYPES:
                return tool_error(
                    f"type must be one of: {', '.join(VALUE_MARKER_TYPES)}")
            try:  # tolerate "5" from models that stringify integers
                note_id = int(args.get("note_id"))
            except (TypeError, ValueError):
                return tool_error("note_id must be an integer")
            deleted = self._store.forget(note_id, marker_type=marker_type)
            return json.dumps({"deleted": deleted, "note_id": note_id, "type": marker_type})
        if tool_name == "minir_stats":
            with self._lock:
                metrics = {tool: dict(values) for tool, values in self._metrics.items()}
            return json.dumps({**self._store.stats(), "session_id": self._session_id,
                               "profile": self._profile,
                               "layers": TRACKER.snapshot(self._session_id),
                               "artifacts": self._store.all_layer_states(),
                               "buffered_messages": len(self._transcript.messages(self._session_id)),
                               "prefetch_limit": self._prefetch_limit,
                               "dialectic_depth": self._dialectic_depth,
                               "tool_metrics": metrics,
                               "reasoning_enabled": self._reasoning})
        return tool_error(f"Unknown tool: {tool_name}")

    # -- cold start -----------------------------------------------------------

    def cold_start(self, session_id: str = "") -> bool:
        """Queue the first *base* refresh for a freshly opened primary session.

        The dialectic review depends on a published base artifact, so this never queues it
        directly: the worker queues the matching dialectic job after the base job succeeds.
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
            # Without host history there is nothing for the base cards to model; identity files
            # alone are consumed by the dialectic prompt only when a base artifact exists.
            if host_history.latest_ts(self._state_db, session) <= 0:
                return False
            job_id = self._store.record_job(BASE_LAYER, session, started, time.time(), turns=0)
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
                scope: str = "session") -> list[dict[str, Any]]:
        try:
            requested = int(limit) if limit is not None else self._prefetch_limit
        except (TypeError, ValueError):
            requested = self._prefetch_limit
        session = session_id or self._session_id
        rows = self._store.search(query, limit=max(1, min(20, requested)),
                                  session_id=session, scope=scope, kinds=kinds)
        return rows

    def _take_cached(self, session: str) -> Optional[tuple[str, int]]:
        """Consume the queued recall for *session*; ``None`` when there is nothing usable."""
        with self._lock:
            cached, self._cached = self._cached, None
        if cached is None:
            return None
        cached_session, block, count = cached
        if cached_session != session or not block:
            return None
        return block, count

    def _format_block(self, rows: list[dict[str, Any]]) -> str:
        """Render note/marker rows as the compact recall block (empty when there are none)."""
        if not rows:
            return ""
        lines = [f"## {_PROVIDER_LABEL} recall"]
        size = len(lines[0])
        for row in rows:
            marker_type = str(row.get("marker_type") or "")
            label = (f"{marker_type} marker ({row['author'] or 'tool'})" if marker_type
                     else f"Note ({row['author'] or 'tool'})")
            label = f"{label} · {format_when(row['when'])}"
            text = " ".join(row["text"].split())
            line = f"- [{label}] {text}"
            size += len(line) + 1
            if size > _MAX_BLOCK_CHARS:
                lines.append("- […more results truncated]")
                break
            lines.append(line)
        return "\n".join(lines)


def _tool_schema(name: str, description: str, properties: Optional[dict[str, Any]] = None,
                 required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}


def _prop(description: str, type_: str = "string", **extra: Any) -> dict[str, Any]:
    return {"type": type_, **extra, "description": description}


_TOOL_SEARCH = _tool_schema(
    "minir_search",
    "Search Minir's long-term memory for stored value markers and derived conclusions — "
    "user/AI cards, adviser reviews, session summaries and notes. Minir keeps no "
    "transcripts; this searches the conclusions derived from them. The default scope is the "
    "current session plus durable cards and notes; set scope='all' to search across sessions.",
    {"query": _prop("What to search for."),
     "limit": _prop("Maximum results to return (default: the provider's prefetch limit).", "integer"),
     "scope": _prop("Recall scope: 'session' (default) or 'all' for cross-session search.",
                    "string", enum=["session", "all"]),
     "session_id": _prop("Optional session to search instead of the current one "
                         "(ignored when scope='all').")},
    ("query",))
_TOOL_REMEMBER = _tool_schema(
    "minir_remember",
    "Record a new value-output marker of a specified type as long-term memory. The type is one "
    "of entities, trajectories, experiences, tools or projects.",
    {"type": _prop("Value-marker type.", "string", enum=list(VALUE_MARKER_TYPES)),
     "content": _prop("The value-output marker to remember.")},
    ("type", "content"))
_TOOL_FORGET = _tool_schema(
    "minir_forget",
    "Delete an old value-output marker by its note_id and type. Use only the note_id returned "
    "for a typed marker result (kind='note' with marker_type) or by minir_remember; untyped "
    "notes and conclusions have no note_id and cannot be forgotten. The type must match the "
    "marker or nothing is deleted.",
    {"type": _prop("Value-marker type the marker was stored with.", "string",
                   enum=list(VALUE_MARKER_TYPES)),
     "note_id": _prop("Id of the marker to delete.", "integer")},
    ("type", "note_id"))
_TOOL_STATS = _tool_schema(
    "minir_stats",
    "Report Minir store statistics — note/marker/session/job counts, the base + dialectic layer "
    "counters for this session, the published layer artifacts (subject + version), and the "
    "database path.")


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
    ctx.register_hook("pre_tool_call", _hook(_on_pre_tool_call, provider))
    # Adviser mode observers: the event ring needs the assistant's intent (interim text and
    # successful API responses with tool calls), the tool outcome, and the final answer.
    ctx.register_hook("on_interim_message", _hook(_on_interim_message, provider))
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
    # Second surface of the same directory: the auxiliary ``adviser`` route + its in-process
    # agent loop (``adviser_consult``). Optional by design — a host without the auxiliary-task
    # API (or a config that refuses the key) must still get the memory provider, so a failure
    # here is logged and swallowed rather than raising out of ``register()``.
    try:
        adviser.register(ctx, provider)
        provider._adviser_consult_registered = True
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
            resolved._adviser_mode.session_started(session_id)
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
        if old_session_id:
            TRACKER.stop(old_session_id)
        TRACKER.begin(session_id)
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
        if resolved is not None:
            resolved._adviser_mode.session_end(session_id)
            resolved.flush_metrics()
        logger.info("Minir on_session_finalize: session=%s", session_id or "-")
    except Exception as exc:
        logger.warning("Minir on_session_finalize failed: %s", exc)


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
        if session_id and provider._session_id and session_id != provider._session_id:
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
        return brief or None
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
        resolved._adviser_mode.on_post_tool_call(
            session_id=session_id, turn_id=str(kwargs.get("turn_id") or ""),
            tool_name=tool_name, status=str(kwargs.get("status") or ""),
            args=kwargs.get("args"), result=kwargs.get("result"),
            error_message=str(kwargs.get("error_message") or ""),
            error=kwargs.get("error"), is_error=kwargs.get("is_error"))


def _on_pre_tool_call(tool_name: str = "", session_id: str = "",
                      provider: Optional[MinirMemoryProvider] = None,
                      **kwargs) -> Optional[dict[str, str]]:
    """``adviser_only`` enforcement: block mutating tools before they execute."""
    resolved = _resolve_provider(provider)
    if resolved is not None and resolved._adviser_mode.blocks_tool(session_id, tool_name):
        return {"action": "block",
                "message": (f"adviser_only mode: {tool_name} is a mutating tool. "
                            "Propose the exact change for the user to apply instead.")}
    return None


def _on_interim_message(session_id: str = "", turn_id: str = "", text: Any = None,
                        iteration: Any = None,
                        provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    """Record the assistant's mid-loop intent (the reviewer's missing half of a tool event)."""
    resolved = _resolve_provider(provider)
    if resolved is not None:
        resolved._adviser_mode.on_interim_message(
            session_id=session_id, turn_id=turn_id, text=text, iteration=iteration)


def _on_post_api_request(session_id: str = "", turn_id: str = "", api_request_id: str = "",
                         assistant_message: Any = None, assistant_tool_call_count: Any = 0,
                         provider: Optional[MinirMemoryProvider] = None, **kwargs) -> None:
    """Record intent and confirm that a request's staged advice actually left the process."""
    resolved = _resolve_provider(provider)
    if resolved is not None:
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
