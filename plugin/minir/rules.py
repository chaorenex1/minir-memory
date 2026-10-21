"""Layer rule files: markdown read at call time and handed to a pass as text.

A rule file is the contract it describes. Nothing here parses front-matter, slices sections or
budgets tokens: the body is passed exactly as written, so editing a rule takes effect on the next
pass and the text the model reads is the text on disk.

A packaged file that cannot be read fails the pass. An operator override under
``$HERMES_HOME/minir-memory/rules`` is optional; an unreadable override falls back to the packaged
copy with a warning, so one bad override cannot stop every pass.
"""

from __future__ import annotations

import logging
import math
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PACKAGE_RULES_DIR = Path(__file__).resolve().parent / "rules"
OVERRIDE_ENV = "MINIR_RULES_DIR"

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """CJK-per-character estimate in tokens, with the 10% margin the page budgets assume.

    Deliberately not a tokenizer: the budget exists to catch bloat, not to match one vendor's
    counter. CJK is one token per character; everything else is four characters per token.
    """
    cjk = sum(1 for char in text or "" if _CJK_RE.match(char))
    other = len(text or "") - cjk
    return math.ceil((cjk + other / 4) * 1.1)


class RulesError(RuntimeError):
    """A rule file is missing or unreadable."""


def _override_dir(override_dir: Optional[Path | str], hermes_home: str) -> Optional[Path]:
    if override_dir:
        return Path(override_dir)
    env = str(os.environ.get(OVERRIDE_ENV, "")).strip()
    if env:
        return Path(env)
    home = str(hermes_home or "").strip()
    return Path(home) / "minir-memory" / "rules" if home else None


def _body(text: str) -> str:
    """The file without its front-matter block; every other line is returned as written."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return text.strip()
    return "\n".join(lines[end + 1:]).strip()


def rule_text(rule_id: str, *, override_dir: Optional[Path | str] = None,
              hermes_home: str = "") -> str:
    """One rule's body: the operator override if it exists, then the packaged file."""
    packaged = PACKAGE_RULES_DIR / f"{rule_id}.md"
    if not packaged.exists():
        raise RulesError(f"rule {rule_id!r} missing from {PACKAGE_RULES_DIR}")
    override = _override_dir(override_dir, hermes_home)
    if override is not None:
        path = override / f"{rule_id}.md"
        if path.exists():
            try:
                return _body(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError) as exc:
                logger.warning("Minir rule override %s unreadable (%s); using the packaged file",
                               path, exc)
    try:
        return _body(packaged.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise RulesError(f"cannot read rule file {packaged}: {exc}") from exc
