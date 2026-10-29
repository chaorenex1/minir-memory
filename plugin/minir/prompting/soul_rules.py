"""SOUL and dispatch-skill constraint sections, mapped to the three ``soul_*`` targets.

The reviewer never loads the whole operator SOUL: it is an identity document larger than the
constraint set, and ``load_soul_identity`` would also rewrite the reviewer's own identity. This
module reads ``$HERMES_HOME/SOUL.md`` plus the auto-loaded dispatch skill
(``skills/agent-workflows/hermes-dispatch/SKILL.md``), splits both on ``# `` headings, and returns
only the sections a target owns.

Execution rules moved out of SOUL into that skill, so ``# Tool policy``, ``# Workflow``,
``# Verification & failure`` and ``# Craft`` come from the skill while identity sections stay in
SOUL. SOUL wins when both files define the same heading. Each file is cached by
``(mtime, size)`` so a review costs two stats, and an unreadable SOUL returns ``None`` — the
caller disables its soul targets for that round rather than judging against nothing. A missing
skill only drops the sections it would have supplied.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any, Optional

#: target -> the SOUL sections it judges. Disjoint by contract: ``# Boundaries`` belongs to the
#: tool-policy target, ``# Delivery contract`` to the workflow target, ``# Craft`` to craft.
TARGET_SECTIONS: dict[str, tuple[str, ...]] = {
    "soul_tool_policy_check": ("Precedence", "Tool policy", "Boundaries"),
    "soul_workflow_check": ("Workflow", "Verification & failure", "Delivery contract", "Critical"),
    "soul_craft_check": ("Craft",),
}

#: Execution sections live here, not in SOUL; resolved under the same home as SOUL.md.
DISPATCH_SKILL_REL = Path("skills") / "agent-workflows" / "hermes-dispatch" / "SKILL.md"

_CACHE: dict[str, tuple[float, int, dict[str, str], str]] = {}
_LOCK = threading.Lock()


def parse_sections(text: str) -> dict[str, str]:
    """Split a markdown document into ``# heading -> body`` (body excludes the heading)."""
    sections: dict[str, str] = {}
    current = ""
    body: list[str] = []
    for line in str(text or "").splitlines():
        if line.startswith("# "):
            if current:
                sections[current] = "\n".join(body).strip()
            current = line[2:].strip()
            body = []
            continue
        if current:
            body.append(line)
    if current:
        sections[current] = "\n".join(body).strip()
    return sections


def _read(path: Path) -> Optional[tuple[dict[str, str], str]]:
    """Parsed sections plus a short content hash, or ``None`` when the file is unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (float(stat.st_mtime), int(stat.st_size))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and (cached[0], cached[1]) == stamp:
            return cached[2], cached[3]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parsed = parse_sections(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    with _LOCK:
        _CACHE[key] = (*stamp, parsed, digest)
    return parsed, digest


def load(hermes_home: Any = "") -> Optional[tuple[dict[str, str], str]]:
    """Merged sections plus a short combined hash, or ``None`` when SOUL is unreadable."""
    home = str(hermes_home or "").strip()
    if not home:
        return None
    soul = _read(Path(home) / "SOUL.md")
    if soul is None:
        return None
    skill = _read(Path(home) / DISPATCH_SKILL_REL) or ({}, "")
    merged = dict(skill[0])
    merged.update(soul[0])
    digest = hashlib.sha256(f"{soul[1]}:{skill[1]}".encode("utf-8")).hexdigest()[:16]
    return merged, digest


def sections_for(hermes_home: Any, target: str) -> str:
    """The target's mapped sections, heading included; empty when the file or section is absent."""
    loaded = load(hermes_home)
    if loaded is None:
        return ""
    sections, _sha = loaded
    parts = [f"# {name}\n{sections[name]}" for name in TARGET_SECTIONS.get(target, ())
             if sections.get(name)]
    return "\n\n".join(parts)


def references(hermes_home: Any) -> str:
    """Paths and section names for the rule text, instead of the text itself.

    Same parsing and the same "no SOUL, no targets" rule as :func:`block`. Pasting the rules
    costs more than the whole review budget, so the reviewer gets the two file paths plus the
    headings each target judges, and reads the section a finding turns on.
    """
    home = Path(str(hermes_home or "")).expanduser()
    if not home.is_dir():
        return ""
    loaded = load(home)
    if not loaded or not isinstance(loaded, tuple):
        return ""
    sections, _digest = loaded
    if not sections:
        return ""
    lines = ["Read the section by heading; SOUL.md wins when both files define it."]
    soul = home / "SOUL.md"
    skill = home / DISPATCH_SKILL_REL
    if soul.is_file():
        lines.append(f"- {soul}")
    if skill.is_file():
        lines.append(f"- {skill}")
    for target, names in TARGET_SECTIONS.items():
        present = [name for name in names if name in sections]
        if present:
            lines.append(f"{target}: " + ", ".join(f"# {name}" for name in present))
    return "\n".join(lines) if len(lines) > 2 else ""


def block(hermes_home: Any, targets: tuple[str, ...] | list[str] = ()) -> str:
    """All enabled soul targets' sections in one block, or ``""`` when none is readable."""
    wanted = tuple(targets) or tuple(TARGET_SECTIONS)
    parts = [sections_for(hermes_home, target) for target in wanted]
    return "\n\n".join(part for part in parts if part)


def available(hermes_home: Any = "") -> bool:
    """True when SOUL plus the dispatch skill parsed into at least one mapped section."""
    loaded = load(hermes_home)
    if loaded is None:
        return False
    sections, _sha = loaded
    return any(sections.get(name) for names in TARGET_SECTIONS.values() for name in names)
