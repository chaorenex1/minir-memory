"""Adviser prompt sections loaded from ``prompts/adviser``.

The core, the two mode blocks, the boundary reminder and the review budget are assets, not Python
strings: editing the prompt does not touch this module. A packaged asset that cannot be read
fails the run. An operator override under ``$HERMES_HOME/minir-memory/prompts/adviser`` (or
``MINIR_PROMPTS_DIR``) wins when it exists; an unreadable override falls back to the packaged
copy with a warning.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from string import Template
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

PACKAGE_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts" / "adviser"
OVERRIDE_ENV = "MINIR_PROMPTS_DIR"


class AdviserPromptError(RuntimeError):
    """An adviser prompt asset is missing or unreadable."""


def _override_dir(override_dir: Optional[Path | str], hermes_home: str) -> Optional[Path]:
    if override_dir:
        return Path(override_dir)
    env = str(os.environ.get(OVERRIDE_ENV, "")).strip()
    if env:
        return Path(env)
    home = str(hermes_home or "").strip()
    return Path(home) / "minir-memory" / "prompts" / "adviser" if home else None


def prompt_text(name: str, *, override_dir: Optional[Path | str] = None,
                hermes_home: str = "") -> str:
    """One prompt section: the operator override if it exists, then the packaged file."""
    packaged = PACKAGE_PROMPTS_DIR / f"{name}.md"
    if not packaged.exists():
        raise AdviserPromptError(f"adviser prompt {name!r} missing from {PACKAGE_PROMPTS_DIR}")
    override = _override_dir(override_dir, hermes_home)
    if override is not None:
        path = override / f"{name}.md"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeError) as exc:
                logger.warning("Minir adviser prompt override %s unreadable (%s); "
                               "using the packaged file", path, exc)
    try:
        return packaged.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise AdviserPromptError(f"cannot read adviser prompt {packaged}: {exc}") from exc


def prompt_section(name: str, *, hermes_home: str = "", **values: object) -> str:
    """A prompt section with ``$NAME`` placeholders filled when values are supplied."""
    text = prompt_text(name, hermes_home=hermes_home)
    if not values:
        return text
    try:
        return Template(text).substitute(
            {key: str(value) for key, value in values.items()})
    except (KeyError, ValueError) as exc:
        raise AdviserPromptError(
            f"adviser prompt {name!r} has a bad placeholder: {exc}") from exc


def consult_prompt(*, hermes_home: str = "") -> str:
    """The shared core plus the synchronous output contract."""
    return "\n\n".join([
        prompt_text("core", hermes_home=hermes_home),
        prompt_text("consult", hermes_home=hermes_home),
    ])


def boundary_guidance(turn: int, every: int = 50, *, hermes_home: str = "") -> str:
    """The 50-turn boundary reminder; empty on every other turn."""
    try:
        step = max(1, int(every))
        value = int(turn)
    except (TypeError, ValueError):
        return ""
    if not (value > 0 and value % step == 0):
        return ""
    return prompt_text("boundary", hermes_home=hermes_home)


def review_prompt(*, hermes_home: str = "", budget: str = "",
                  rules: Sequence[str] = ()) -> str:
    """The core, the review contract, the live budget and the appended rule bodies."""
    parts = [
        prompt_text("core", hermes_home=hermes_home),
        prompt_text("review", hermes_home=hermes_home),
    ]
    if budget:
        parts.append(budget)
    parts.extend(str(rule).strip() for rule in rules if str(rule).strip())
    return "\n\n".join(parts)
