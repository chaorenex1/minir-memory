"""The dialectic-layer cadence, shared by both surfaces of this plugin.

The memory provider (``turn_counter.TRACKER``) and the context engine need the same answer to
"how many turns has this session seen?", and they are loaded as **two separate module instances**
— the memory loader imports this directory as ``_hermes_user_memory.minir__source_<sha16>`` and
the engine loader as ``_hermes_user_context_engine.minir`` — so a module-level counter is *not*
shared between them. The shared state therefore lives in the store, a file both instances open,
and this module is the one place that reads it.

Two sources, whichever is further along:

* **the queued job** — every dialectic dispatch writes ``jobs.turns`` with the counter's value at
  that moment, so ``MAX(turns)`` for a session *is* the newest dialectic beat. This is what the
  engine folds on: one fold per dialectic refresh, which is the coupling this plugin wants.
* **the window itself** — the newest carrier states the turn it folded through, plus the
  user-authored turns after it. That is the only source available when the memory provider is not
  the active one (no jobs exist), and it keeps counting **monotonically across folds**, because
  the turns a carrier covers are counted in the carrier's own stamp rather than disappearing with
  the messages that held them.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .turn_counter import DEFAULT_DIALECTIC_INTERVAL, DIALECTIC_LAYER

logger = logging.getLogger(__name__)

STORE_DIRNAME = "minir-memory"
DB_FILENAME = "memory.db"


def configured_interval(hermes_home: Any = "") -> int:
    """The dialectic turn interval, resolved exactly as the memory provider resolves it.

    The provider reads ``MINIR_DIALECTIC_INTERVAL`` (which wins) and then
    ``memory.minir.dialectic_interval``, so the engine reads the same two places — otherwise the
    two surfaces would fold on different beats the moment the interval is not the default.
    """
    try:
        override = str(os.environ.get(
            "MINIR_DIALECTIC_CADENCE",
            os.environ.get("MINIR_DIALECTIC_INTERVAL", ""))).strip()
        if override:
            return max(1, int(override))
    except (TypeError, ValueError):
        pass
    try:
        from hermes_cli.config import load_config

        block = (load_config().get("memory") or {}).get("minir") or {}
        value = block.get("dialectic_cadence",
                          block.get("dialecticCadence", block.get("dialectic_interval")))
        return max(1, int(value or DEFAULT_DIALECTIC_INTERVAL))
    except Exception as exc:  # no host config in reach: the provider's own default
        logger.debug("Minir interval resolution fell back to the default: %s", exc)
        return DEFAULT_DIALECTIC_INTERVAL


def configured_handoff(hermes_home: Any = "") -> bool:
    """Whether compression may call the summary model; env wins over ``memory.minir``."""
    override = str(os.environ.get("MINIR_HANDOFF_ENABLED", "")).strip().lower()
    if override in ("0", "false", "no", "off"):
        return False
    if override in ("1", "true", "yes", "on"):
        return True
    try:
        from hermes_cli.config import load_config

        block = (load_config().get("memory") or {}).get("minir") or {}
        value = block.get("handoff_enabled")
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("0", "false", "no", "off")
    except Exception:
        return True


def configured_reasoning(hermes_home: Any = "") -> bool:
    """Whether layer/compression model calls are allowed; same sources as the provider."""
    override = str(os.environ.get("MINIR_REASONING_ENABLED", "")).strip().lower()
    if override in ("0", "false", "no", "off"):
        return False
    if override in ("1", "true", "yes", "on"):
        return True
    try:
        from hermes_cli.config import load_config

        block = (load_config().get("memory") or {}).get("minir") or {}
        value = block.get("reasoning_enabled")
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("0", "false", "no", "off")
    except Exception:
        return True


def store_path(hermes_home: Any) -> Optional[Path]:
    """``$HERMES_HOME/minir-memory/memory.db`` for a profile, or ``None`` without a home."""
    home = str(hermes_home or "").strip()
    return Path(home) / STORE_DIRNAME / DB_FILENAME if home else None


def dialectic_beat(db_path: Optional[Path], session_id: str) -> int:
    """Turns the dialectic layer had counted at its newest dispatch for this session.

    Read-only and failure-tolerant by design: a missing database, a session the provider never
    saw, or a locked file all mean "no beat yet" rather than an error — this runs on a context
    engine's request path, which has to stay fail-open.
    """
    if db_path is None or not session_id:
        return 0
    try:
        if not Path(db_path).exists():
            return 0
        connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=2.0)
        try:
            row = connection.execute(
                "SELECT MAX(turns) AS turns FROM jobs WHERE session_id = ? AND layer = ?",
                (session_id, DIALECTIC_LAYER),
            ).fetchone()
        finally:
            connection.close()
    except Exception as exc:  # a cadence read must never break a request or a turn
        logger.debug("Minir cadence read failed: %s", exc)
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def window_turns(messages: Optional[Iterable[dict[str, Any]]], *,
                 is_carrier: Optional[Callable[[dict[str, Any]], bool]] = None,
                 carrier_end: Optional[Callable[[dict[str, Any]], Optional[int]]] = None) -> int:
    """Turns the window accounts for: carriers' stamps plus the user turns after them.

    ``carrier_end(message)`` returns the turn a carrier folded through; counting restarts from
    there, so a fold moves turns from "messages" to "stamp" without losing them from the count.
    """
    base = 0
    counted = 0
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        if is_carrier is not None and is_carrier(message):
            end = carrier_end(message) if carrier_end is not None else None
            if end:
                base = max(base, int(end))
                counted = 0
            continue
        if message.get("role") == "user":
            counted += 1
    return base + counted
