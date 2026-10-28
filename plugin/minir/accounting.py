"""Per-route token accounting into the host's own ledger (``session_model_usage``).

The host records an auxiliary call only when the call runs inside a published
``agent.aux_accounting`` context — a live turn. Every layer pass is a background agent loop, so
its tokens never reached the ledger: per-layer cost had to be reconstructed from prompt caps,
and the only rows the ledger held were a handful of one-shot calls that happened to run inside
a turn.

Each ``agent_loop.run`` now bills its own provider-reported usage under a task named after the
layer, keyed to the conversation the pass was built from:

* ``minir:base`` / ``minir:dialectic`` — the two passes of the ``auxiliary.minir_dialectic`` route
* ``minir:knowledge`` — idle-session page extraction
* ``adviser`` — the live reviewer (same task name the host's own aux rows use)

One row per ``(session, model, provider, task)`` accumulates in ``session_model_usage``, so a
month of layer passes reads back as one query instead of an estimate. The write is strictly
best-effort: accounting must never fail a reasoning pass, and a bare checkout without the host
modules simply records nothing.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)

#: Ledger task names. The layer passes share the dialectic route, so the layer is part of the
#: name; the adviser keeps the host's own ``adviser`` bucket so both writers land in one row.
LAYER_TASK_PREFIX = "minir:"
KNOWLEDGE_TASK = "minir:knowledge"
ADVISER_TASK = "adviser"


def layer_task(layer: str) -> str:
    """Ledger task for one layer pass (``base`` -> ``minir:base``)."""
    return f"{LAYER_TASK_PREFIX}{layer or 'route'}"

#: ``(state_db path, conversation session id)`` this thread's route calls bill to.
_scope: ContextVar[Optional[tuple[str, str]]] = ContextVar("minir_accounting_scope", default=None)
_dbs: dict[str, Any] = {}
_lock = threading.Lock()


def current() -> Optional[tuple[str, str]]:
    """The ``(state_db, session)`` this thread's route calls bill to, or ``None``."""
    return _scope.get()


@contextmanager
def accounting_scope(state_db: str, session_id: str) -> Iterator[None]:
    """Bill the route calls made in this thread to *session_id*.

    An empty session or store opts out: a global pass (the three-day card review) has no
    conversation to charge, and inventing one would put a phantom row in the host's session list.
    """
    path = str(state_db or "").strip()
    session = str(session_id or "").strip()
    token = _scope.set((path, session) if path and session else None)
    try:
        yield
    finally:
        _scope.reset(token)


def _open_db(path: str) -> Any:
    """The process-wide SessionDB for *path* (the host registry shares one per file)."""
    with _lock:
        cached = _dbs.get(path)
    if cached is not None:
        return cached
    db = None
    try:
        db = _acquire(path)
    except Exception as exc:  # host module absent, path unreadable, schema missing
        logger.debug("Minir usage ledger unavailable (%s)", exc)
        return None
    with _lock:
        _dbs.setdefault(path, db)
        return _dbs[path]


def _acquire(path: str) -> Any:
    try:
        from hermes_state_registry import acquire

        return acquire(Path(path))
    except ImportError:
        from hermes_state import SessionDB

        return SessionDB(Path(path))


def _count(usage: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def counts(usage: Optional[Mapping[str, Any]]) -> dict[str, int]:
    """Canonical buckets from a turn result's usage map (input/output/cache/reasoning)."""
    data = usage if isinstance(usage, Mapping) else {}
    cache_read = _count(data, "cache_read_tokens")
    cache_write = _count(data, "cache_write_tokens")
    input_tokens = _count(data, "input_tokens")
    if not input_tokens:
        # Chat-shaped results report prompt_tokens inclusive of the cached prefix.
        input_tokens = max(0, _count(data, "prompt_tokens") - cache_read - cache_write)
    return {
        "input_tokens": input_tokens,
        "output_tokens": _count(data, "output_tokens", "completion_tokens"),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": _count(data, "reasoning_tokens"),
    }


def _cost(model: str, provider: str, base_url: str, counts_: dict[str, int]) -> Optional[float]:
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

        result = estimate_usage_cost(model, CanonicalUsage(**counts_),
                                     provider=provider or None, base_url=base_url or None)
        return float(result.amount_usd) if result.amount_usd is not None else None
    except Exception:
        return None


def record(state_db: str, session_id: str, task: str, *, model: str = "", provider: str = "",
           usage: Optional[Mapping[str, Any]] = None, api_calls: int = 1,
           base_url: str = "") -> bool:
    """Add one route call's usage to the host ledger under *task*; ``True`` when written."""
    path = str(state_db or "").strip()
    session = str(session_id or "").strip()
    label = str(task or "").strip()
    if not (path and session and label):
        return False
    buckets = counts(usage)
    if not any(buckets.values()):
        return False
    db = _open_db(path)
    if db is None:
        return False
    kwargs = {
        "model": str(model or "") or None,
        "billing_provider": str(provider or "") or None,
        "billing_base_url": str(base_url or "") or None,
        "estimated_cost_usd": _cost(model, provider, base_url, buckets),
        "api_call_count": max(1, int(api_calls or 1)),
        **buckets,
    }
    try:
        from agent.runtime_session_store import call_off_event_loop
    except ImportError:
        pass
    else:
        try:  # a worker-backed store refuses synchronous RPC from inside a running loop
            call_off_event_loop(db.record_auxiliary_usage, session, label, **kwargs)
            return True
        except Exception as exc:
            logger.debug("Minir usage ledger write deferred (%s); writing inline", exc)
    try:
        db.record_auxiliary_usage(session, label, **kwargs)
        return True
    except Exception as exc:
        logger.debug("Minir usage ledger write failed (%s)", exc)
        return False
