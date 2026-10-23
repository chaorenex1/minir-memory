"""Config and store-path helpers shared by the provider and the context engine.

The two surfaces are loaded as separate module instances, so this module only resolves settings
from the environment/config and computes the profile store path. It owns no counters.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

STORE_DIRNAME = "minir-memory"
DB_FILENAME = "memory.db"


def configured_reasoning() -> bool:
    """Whether model passes may run; env wins over ``memory.minir``.

    The provider reads the same key for its layer worker. The engine is a separate module
    instance, so it needs its own reader for the one call compression may make.
    """
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
