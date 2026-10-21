"""Layer rule files: the field contracts assembled into each layer's system prompt.

A rule file is markdown with front-matter and exactly two H2 sections, CONTRACT and
REVIEW CHECKS. One file serves two readers: the pass that writes the artifact reads CONTRACT,
the reviewer reads REVIEW CHECKS. Keeping both in one file is what stops the writer's rule and
the reviewer's checklist from drifting apart.

The packaged files are code assets: a missing or invalid default fails the pass (the caller
reports ``rules_error``) rather than degrading to a prompt without field definitions. An operator
override under ``$HERMES_HOME/minir-memory/rules`` is optional; an invalid override falls back to
the packaged file with a warning, so one bad override cannot stop every pass.

Rules are read at call time, never compiled into constants, and every assembly returns a hash that
names the rule versions it used. That hash belongs in the artifact metadata: it is the only way to
tell, after a rollback, which rules produced a card.

The files are English like the system prompts they extend. Card values follow the conversation
language; headings and keys stay English because the parser reads them.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

PACKAGE_RULES_DIR = Path(__file__).resolve().parent / "rules"
OVERRIDE_ENV = "MINIR_RULES_DIR"

CONTRACT = "contract"
REVIEW_CHECKS = "review_checks"
CONSUMERS: tuple[str, ...] = (CONTRACT, REVIEW_CHECKS)

RULE_BUDGET_RATIO = 1.5   # a file's body may exceed max_tokens by half before it is rejected


def estimate_tokens(text: str) -> int:
    """CJK-per-character estimate in tokens, with the 10% margin the layer budget assumes.

    Deliberately not a tokenizer: the budget exists to catch bloat, not to match one vendor's
    counter. CJK is one token per character; everything else is four characters per token.
    """
    cjk = sum(1 for char in text or "" if _is_cjk(char))
    other = len(text or "") - cjk
    return math.ceil((cjk + other / 4) * 1.1)


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def _is_cjk(char: str) -> bool:
    return bool(_CJK_RE.match(char))


class RulesError(RuntimeError):
    """A rule file, its front-matter, its scope or the assembled budget is not usable."""


@dataclass(frozen=True)
class Rule:
    """One parsed rule file. ``sections`` maps ``contract`` / ``review_checks`` to their body."""

    id: str
    version: int
    applies_to: tuple[str, ...]
    slot: str
    max_tokens: int
    path: Path
    sections: dict[str, str]

    def section(self, consumer: str) -> str:
        if consumer not in CONSUMERS:
            raise RulesError(f"unknown consumer {consumer!r}")
        try:
            return self.sections[consumer]
        except KeyError:
            raise RulesError(f"rule {self.id!r} has no {consumer} section") from None


@dataclass(frozen=True)
class RuleSection:
    """The slice a pass receives, plus the version that keeps it auditable."""

    id: str
    version: int
    consumer: str
    text: str
    path: Path

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class PassContract:
    """Which files one pass reads, from which section, under which token budget."""

    rules: tuple[str, ...]
    consumer: str
    budget: int


# The assembly contract from the rule files draft, limited to layers that exist in code. A new
# layer adds a row here; an unlisted (layer, pass) is a programming error, not an empty prompt.
# ``card_review`` joins when that layer lands: it reads all four card contracts (1500 tokens).
PASS_CONTRACTS: dict[tuple[str, str], PassContract] = {
    ("base", "B1"): PassContract(("user-self", "user-preferences"), CONTRACT, 700),
    ("base", "B2"): PassContract(("ai-self", "ai-identity"), CONTRACT, 800),
    ("base", "B3"): PassContract(("markers-category", "markers-org"), CONTRACT, 900),
    ("dialectic", "review"): PassContract(
        ("user-self", "user-preferences", "ai-self", "ai-identity"), REVIEW_CHECKS, 450),
    ("card_review", "review"): PassContract(
        ("user-self", "user-preferences", "ai-self", "ai-identity"), CONTRACT, 1500),
}

def _layer_key(layer: str, pass_id: str) -> str:
    """The ``applies_to`` token a pass matches: ``base:B1`` for base, the layer name otherwise."""
    return f"base:{pass_id}" if layer == "base" else layer


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise RulesError(f"cannot read rule file {path}: {exc}") from exc


def _parse_front_matter(text: str, path: Path) -> tuple[dict[str, object], str]:
    """Parse the file's small front-matter: scalars, integers and flat inline lists only."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RulesError(f"{path.name}: missing front-matter")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise RulesError(f"{path.name}: unterminated front-matter") from None
    meta: dict[str, object] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep or not key.strip():
            raise RulesError(f"{path.name}: malformed front-matter line {line!r}")
        key, raw = key.strip(), raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            meta[key] = [part.strip().strip("\"'") for part in inner.split(",") if part.strip()]
        elif raw.isdigit():
            meta[key] = int(raw)
        else:
            meta[key] = raw.strip("\"'")
    return meta, "\n".join(lines[end + 1:])


_HEADING_RE = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
_HEADING_KEYS = {"CONTRACT": CONTRACT, "REVIEW CHECKS": REVIEW_CHECKS}


def _split_sections(body: str, path: Path) -> dict[str, str]:
    """Exactly the two contract headings, in order; anything else is a malformed file."""
    found = [(match.group(1).strip().upper(), match.start(), match.end())
             for match in _HEADING_RE.finditer(body)]
    names = [name for name, _start, _end in found]
    if names != ["CONTRACT", "REVIEW CHECKS"]:
        raise RulesError(f"{path.name}: expected CONTRACT then REVIEW CHECKS, found {names}")
    sections: dict[str, str] = {}
    for index, (name, _start, end) in enumerate(found):
        stop = found[index + 1][1] if index + 1 < len(found) else len(body)
        sections[_HEADING_KEYS[name]] = body[end:stop].strip()
    return sections


def parse_rule(path: Path) -> Rule:
    """Read one rule file and enforce its contract. Raises :class:`RulesError`."""
    meta, body = _parse_front_matter(_read_text(path), path)
    missing = [key for key in ("id", "version", "applies_to", "slot", "max_tokens", "sections")
               if key not in meta]
    if missing:
        raise RulesError(f"{path.name}: front-matter missing {', '.join(missing)}")
    rule_id = str(meta["id"]).strip()
    if rule_id != path.stem:
        raise RulesError(f"{path.name}: id {rule_id!r} does not match the filename")
    version = meta["version"]
    max_tokens = meta["max_tokens"]
    if not isinstance(version, int) or version < 1:
        raise RulesError(f"{path.name}: version must be a positive integer")
    if not isinstance(max_tokens, int) or max_tokens < 1:
        raise RulesError(f"{path.name}: max_tokens must be a positive integer")
    applies_to = meta["applies_to"]
    if not isinstance(applies_to, list) or not applies_to:
        raise RulesError(f"{path.name}: applies_to must be a non-empty list")
    if str(meta["slot"]).strip() != "fields":
        raise RulesError(f"{path.name}: unsupported slot {meta['slot']!r}")
    declared = meta["sections"]
    if not isinstance(declared, list) or [str(name).strip() for name in declared] != list(CONSUMERS):
        raise RulesError(f"{path.name}: sections must be [contract, review_checks]")
    sections = _split_sections(body, path)
    tokens = estimate_tokens(body)
    if tokens > max_tokens * RULE_BUDGET_RATIO:
        raise RulesError(
            f"{path.name}: body is ~{tokens} tokens, over {max_tokens} x {RULE_BUDGET_RATIO}")
    return Rule(id=rule_id, version=version, applies_to=tuple(str(item).strip() for item in applies_to),
                slot="fields", max_tokens=max_tokens, path=path, sections=sections)


class RulesLoader:
    """Load, validate and cache rule files; ``build_system`` assembles one pass's prompt.

    The cache is keyed by path and invalidated by ``(mtime_ns, size)``, so editing a rule takes
    effect on the next call without a restart and without re-reading unchanged files.
    """

    def __init__(self, *, default_dir: Optional[Path | str] = None,
                 override_dir: Optional[Path | str] = None, hermes_home: str = "") -> None:
        self.default_dir = Path(default_dir) if default_dir else PACKAGE_RULES_DIR
        self.override_dir = self._resolve_override(override_dir, hermes_home)
        self._cache: dict[Path, tuple[tuple[int, int], Rule]] = {}

    @staticmethod
    def _resolve_override(override_dir: Optional[Path | str], hermes_home: str) -> Optional[Path]:
        if override_dir:
            return Path(override_dir)
        env = str(os.environ.get(OVERRIDE_ENV, "")).strip()
        if env:
            return Path(env)
        home = str(hermes_home or "").strip()
        return Path(home) / "minir-memory" / "rules" if home else None

    def _cached(self, path: Path) -> Rule:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        rule = parse_rule(path)
        self._cache[path] = (stamp, rule)
        return rule

    def load(self, rule_id: str, consumer: str, *, layer: str = "") -> RuleSection:
        """One rule's section for *layer*; ``layer`` is the ``applies_to`` token (``base:B1``)."""
        if consumer not in CONSUMERS:
            raise RulesError(f"unknown consumer {consumer!r}")
        default = self.default_dir / f"{rule_id}.md"
        if not default.exists():
            raise RulesError(f"rule {rule_id!r} missing from {self.default_dir}")
        candidates: list[Path] = []
        if self.override_dir is not None:
            override = self.override_dir / f"{rule_id}.md"
            if override.exists():
                candidates.append(override)
        candidates.append(default)
        last_error: Optional[RulesError] = None
        for path in candidates:
            is_override = path != default
            try:
                rule = self._cached(path)
                if layer and layer not in rule.applies_to:
                    raise RulesError(
                        f"rule {rule.id!r} does not apply to {layer!r} "
                        f"(applies_to: {', '.join(rule.applies_to)})")
            except (RulesError, OSError) as exc:
                if not is_override:
                    raise RulesError(str(exc)) from exc
                logger.warning("Minir rule override %s rejected (%s); using the packaged file",
                               path, exc)
                last_error = exc if isinstance(exc, RulesError) else RulesError(str(exc))
                continue
            return RuleSection(id=rule.id, version=rule.version, consumer=consumer,
                               text=rule.section(consumer), path=path)
        raise last_error or RulesError(f"rule {rule_id!r} could not be loaded")


def rules_hash(blocks: Sequence[RuleSection]) -> str:
    """Short digest of the rule ids, versions and text an assembly used."""
    digest = hashlib.sha256()
    for block in blocks:
        digest.update(f"{block.id}@{block.version}\n{block.text}\n".encode("utf-8"))
    return digest.hexdigest()[:16]


def _render_block(block: RuleSection) -> str:
    return f'<rules id="{block.id}" version="{block.version}">\n{block.text}\n</rules>'


def build_system(layer: str, pass_id: str, consumer: str, *, core: str, output: str,
                 loader: Optional[RulesLoader] = None) -> tuple[str, str]:
    """Assemble one pass's system message and the hash of the rules it carries.

    Order is fixed: layer core, then one ``<rules>`` block per rule, then the output contract.
    The output contract is always last so a rule cannot outrank it. A budget overrun is an error
    (compress the rule, not the evidence), and the consumer must be the one the pass is contracted
    for -- a writer cannot be handed the reviewer's checklist by accident.
    """
    contract = PASS_CONTRACTS.get((layer, pass_id))
    if contract is None:
        raise RulesError(f"no rule contract for {layer}:{pass_id}")
    if consumer != contract.consumer:
        raise RulesError(f"{layer}:{pass_id} reads {contract.consumer}, not {consumer}")
    if not (core or "").strip() or not (output or "").strip():
        raise RulesError(f"{layer}:{pass_id} needs both a core and an output contract")
    loader = loader or RulesLoader()
    blocks = [loader.load(rule_id, consumer, layer=_layer_key(layer, pass_id))
              for rule_id in contract.rules]
    tokens = sum(block.tokens for block in blocks)
    if tokens > contract.budget:
        raise RulesError(
            f"{layer}:{pass_id} rules need ~{tokens} tokens, budget is {contract.budget}")
    parts = [core.strip(), *(_render_block(block) for block in blocks), output.strip()]
    return "\n\n".join(parts), rules_hash(blocks)
