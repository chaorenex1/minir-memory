"""Knowledge production: one markdown page per marker unit, plus the paths-only retrieval block.

The base layer already decides what is durable; this module turns each marker unit into a file
under ``$HERMES_HOME/minir-memory/knowledge`` and later answers "which pages might this turn
need?" without reading them. It is stage P6 of the layer pipeline and deliberately narrow:

* **One file per identity.** ``(category, org, topic)`` is the identity. A rewrite replaces that
  file atomically and bumps ``revisions``; older evidence is skipped, never merged backwards.
* **A file budget, not a truncation.** Every write is measured with
  :func:`minir.rules.estimate_tokens` against ``knowledge_token_budget`` (2k by default). An
  over-budget body is retried once with its oldest evidence lines dropped; still over means the
  write fails, the previous file stays intact and the failure lands in ``_failed/``. Shortening
  a page silently would turn a budget breach into a page that looks complete.
* **Front-matter is the index.** :func:`select_paths` reads only the head of each file, so a turn
  pays for metadata, not for every page body. There is no index database and no embedding model.
* **Production is off the hot path.** :class:`KnowledgePipeline` owns one daemon thread and one
  bounded queue per profile; a base publish never waits for a page, and a failed render is logged
  and counted, never raised into the caller that published the artifact.

The body is the unit's ``content`` verbatim unless a ``render`` callable is supplied by the
caller. This module never calls a model itself: the render layer is optional, and its absence must
not turn a publish into a network dependency.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .layers import CATEGORIES, ORGS
from .rules import estimate_tokens

logger = logging.getLogger(__name__)

SCHEMA = "minir-knowledge/1"
KNOWLEDGE_DIRNAME = Path("minir-memory") / "knowledge"
FAILED_DIRNAME = "_failed"

SLUG_MAX = 60
DEFAULT_BUDGET = 2000
DEFAULT_LIMIT = 6
DEFAULT_MAX_CHARS = 700
MAX_BLOCK_CHARS = 700
QUEUE_MAX = 512
RECENCY_DAYS = 30.0
SUMMARY_MAX = 120
_FRONT_MATTER_READ = 16_384

BLOCK_HEADER = "[knowledge — retrieve on demand]"
BLOCK_FOOTER = "[end knowledge]"

FRONT_MATTER_KEYS: tuple[str, ...] = (
    "schema", "category", "org", "topic", "epistemic", "time", "revisions", "updated_at",
    "id", "objects", "relations", "summary", "source_session", "source_artifact", "produced_at",
    "content_hash",
)

RenderFn = Callable[[Mapping[str, Any], str], str]

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_PATH_SEPARATORS = frozenset('/\\:*?"<>|')
_EVIDENCE_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+\S")
_CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+")
_WORD_RE = re.compile(r"[a-z0-9_]{2,}")


class KnowledgeError(RuntimeError):
    """A unit cannot be turned into a page, or the page cannot be made to fit its budget."""


# -- naming ------------------------------------------------------------------------------------


def slugify(topic: Any, *, limit: int = SLUG_MAX) -> str:
    """The topic's filename stem: CJK/alphanumerics kept, spaces become ``-``, the rest dropped.

    Path separators, the Windows-reserved punctuation and control characters are removed rather
    than transliterated, so a topic can never escape its org directory. Runs of punctuation
    collapse to one hyphen, the result is capped at ``limit`` characters (the default 60 keeps
    Windows below its path-component ceiling) and a reserved device name is prefixed with ``_``.
    Two topics that normalize to the same slug are still two identities; the collision is
    resolved by :func:`_new_path`, never by merging the topics.
    """
    text = unicodedata.normalize("NFKC", str(topic or "")).strip()
    chars: list[str] = []
    for char in text:
        if char.isspace():
            chars.append("-")
        elif char in _PATH_SEPARATORS or unicodedata.category(char).startswith("C"):
            continue
        elif char.isalnum() or char in "-_.":
            chars.append(char)
        else:
            chars.append("-")
    slug = re.sub(r"[-_.]{2,}", "-", "".join(chars)).strip("-._")
    if len(slug) > max(1, int(limit)):
        slug = slug[:max(1, int(limit))].rstrip("-._")
    if not slug:
        digest = hashlib.sha1(text.casefold().encode("utf-8")).hexdigest()[:8]
        slug = f"topic-{digest}"
    if slug.casefold() in _WINDOWS_RESERVED:
        slug = f"_{slug}"
    return slug


def render_filename(category: Any, topic: Any) -> str:
    """``CATEGORY-slug.md`` — the file name both the writer and the failure record use."""
    name = str(category or "").strip().upper()
    if name not in CATEGORIES:
        raise KnowledgeError(f"unknown category {category!r}")
    return f"{name}-{slugify(topic)}.md"


def knowledge_dir(hermes_home: Any = "") -> Path:
    """``$HERMES_HOME/minir-memory/knowledge`` for a profile.

    ``hermes_home`` wins over the environment. There is no platform-default guess: writing under
    a home directory the caller did not name would put pages in a profile the caller never asked
    about, so an unconfigured root is an error.
    """
    home = str(hermes_home or "").strip() or os.environ.get("HERMES_HOME", "").strip()
    if not home:
        raise KnowledgeError(
            "knowledge root is not configured: pass root/hermes_home or set HERMES_HOME")
    return Path(home) / KNOWLEDGE_DIRNAME


def _topic_key(topic: Any) -> str:
    return " ".join(str(topic or "").split()).casefold()


def _identity(unit: Mapping[str, Any]) -> tuple[str, str, str]:
    """Validate the three identity fields against the layer contract's vocabularies."""
    if not isinstance(unit, Mapping):
        raise KnowledgeError("unit must be a mapping")
    category = str(unit.get("category") or "").strip().upper()
    org = str(unit.get("org") or "").strip()
    topic = " ".join(str(unit.get("topic") or "").split())
    if category not in CATEGORIES:
        raise KnowledgeError(f"unknown category {category!r}")
    if org not in ORGS:
        raise KnowledgeError(f"unknown org {org!r}")
    if not topic:
        raise KnowledgeError("unit topic is empty")
    return category, org, topic


# -- front-matter ------------------------------------------------------------------------------


def _front_matter_value(value: Any) -> str:
    """One line, JSON-quoted only when a plain scalar would not survive the parser."""
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    if not text:
        return '""'
    unsafe = (text != text.strip()
              or any(char in text for char in '"\n\r:#[]{}')
              or text.casefold() in {"null", "true", "false", "~"})
    return json.dumps(text, ensure_ascii=False) if unsafe else text


def _front_matter_scalar(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return json.loads(raw)
        except ValueError:
            inner = raw[1:-1].strip()
            return [part.strip().strip("\"'") for part in inner.split(",") if part.strip()]
    return raw


def read_front_matter(path: Any) -> dict[str, Any]:
    """Parse only the front-matter of one knowledge page; ``{}`` when absent or unreadable.

    The file is opened for a bounded head read: the per-turn selector must never pay for page
    bodies, and a malformed or hand-edited file is skipped rather than raising into a turn.
    """
    try:
        with Path(path).open("r", encoding="utf-8-sig", errors="replace") as handle:
            head = handle.read(_FRONT_MATTER_READ)
    except OSError:
        return {}
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}
    meta: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep or not key.strip():
            continue
        value = _front_matter_scalar(raw)
        name = key.strip()
        if name == "revisions" and isinstance(value, str) and value.isdigit():
            value = int(value)
        meta[name] = value
    return meta


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}, ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}, text
    return read_front_matter(path), "\n".join(lines[end + 1:])


# -- unit helpers ------------------------------------------------------------------------------


def _relation_key(relation: Any) -> str:
    if isinstance(relation, Mapping):
        return f"{relation.get('type', '')}:{relation.get('ref', '')}"
    return str(relation)


def _unit_fingerprint(unit: Mapping[str, Any]) -> str:
    """Content identity for change detection; ``time`` is excluded on purpose.

    A re-timestamped unit with the same body is not new evidence, so it must not rewrite the page
    or bump ``revisions``. Time is still compared separately for the stale check.
    """
    payload = {
        "category": str(unit.get("category") or "").strip().upper(),
        "org": str(unit.get("org") or "").strip(),
        "topic": _topic_key(unit.get("topic")),
        "epistemic": str(unit.get("epistemic") or "").strip(),
        "content": str(unit.get("content") or ""),
        "objects": [str(item) for item in _as_list(unit.get("objects"))],
        "relations": [_relation_key(item) for item in _as_list(unit.get("relations"))],
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _summary_for(unit: Mapping[str, Any]) -> str:
    text = " ".join(str(unit.get("summary") or "").split())
    if not text:
        content = " ".join(str(unit.get("content") or "").split())
        text = re.split(r"(?<=[.!?。！？])\s*", content)[0] if content else ""
    text = " ".join(text.split())
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX - 1].rstrip() + "…"
    return text


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _is_stale(unit_time: Any, stored_time: Any) -> bool:
    """True when the incoming evidence predates what the page already holds.

    Missing or unparseable timestamps are treated as "cannot prove stale", because dropping a
    real content update is worse than rewriting a page whose clock is ambiguous.
    """
    incoming = " ".join(str(unit_time or "").split())
    stored = " ".join(str(stored_time or "").split())
    if not incoming or not stored:
        return False
    new, old = _parse_time(incoming), _parse_time(stored)
    if new is not None and old is not None:
        return new < old
    return incoming < stored


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _revisions(meta: Mapping[str, Any]) -> int:
    try:
        return max(0, int(str(meta.get("revisions") or "0").strip() or 0))
    except (TypeError, ValueError):
        return 1


# -- paths -------------------------------------------------------------------------------------


def _meta_matches(meta: Mapping[str, Any], category: str, org: str, topic: str) -> bool:
    return (str(meta.get("category") or "").strip().upper() == category
            and str(meta.get("org") or "").strip() == org
            and _topic_key(meta.get("topic")) == _topic_key(topic))


def _find_existing(root: Path, category: str, org: str, topic: str) -> Optional[Path]:
    """The page already holding this identity, by front-matter — not by guessed filename alone.

    Front-matter is the authority: a page whose name happens to match but whose ``topic`` differs
    is another topic's page, and a page written under a collision suffix is still found by its
    identity. This is also what makes ``Docker`` and ``docker`` one identity on case-sensitive
    filesystems.
    """
    folder = Path(root) / org
    if not folder.is_dir():
        return None
    expected = folder / render_filename(category, topic)
    if expected.exists():
        meta = read_front_matter(expected)
        if meta and _meta_matches(meta, category, org, topic):
            return expected
    prefix = f"{category.casefold()}-"
    try:
        candidates = sorted(folder.glob("*.md"))
    except OSError:
        return None
    for path in candidates:
        if path == expected or not path.name.casefold().startswith(prefix):
            continue
        meta = read_front_matter(path)
        if meta and _meta_matches(meta, category, org, topic):
            return path
    return None


def _occupied(folder: Path, name: str) -> bool:
    if (folder / name).exists():
        return True
    if not folder.is_dir():
        return False
    target = name.casefold()
    return any(path.name.casefold() == target for path in folder.glob("*.md"))


def _new_path(root: Path, category: str, org: str, topic: str) -> Path:
    """The target for a new identity; a slug collision gets a deterministic hash suffix."""
    folder = Path(root) / org
    base = render_filename(category, topic)
    if not _occupied(folder, base):
        return folder / base
    digest = hashlib.sha1(_topic_key(topic).encode("utf-8")).hexdigest()[:8]
    stem = base[:-len(".md")]
    candidate = folder / f"{stem}-{digest}.md"
    counter = 2
    while _occupied(folder, candidate.name):
        candidate = folder / f"{stem}-{digest}-{counter}.md"
        counter += 1
    return candidate


# -- rendering and writing ---------------------------------------------------------------------


def _body_for(unit: Mapping[str, Any], render: Optional[RenderFn], language: str) -> str:
    if render is None:
        body = str(unit.get("content") or "")
    else:
        try:
            body = render(dict(unit), str(language or "en"))
        except Exception as exc:  # a render layer's failure is this page's failure
            raise KnowledgeError(f"render failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(body, str):
            raise KnowledgeError("render must return a string body")
    body = body.strip("\n")
    if not body.strip():
        raise KnowledgeError("unit body is empty")
    return body


def _compose(*, category: str, org: str, topic: str, unit: Mapping[str, Any], body: str,
             revisions: int, updated_at: str, produced_at: str) -> str:
    fields: list[tuple[str, Any]] = [
        ("schema", SCHEMA),
        ("category", category),
        ("org", org),
        ("topic", topic),
        ("epistemic", str(unit.get("epistemic") or "")),
        ("time", str(unit.get("time") or "")),
        ("revisions", revisions),
        ("updated_at", updated_at),
        ("id", str(unit.get("id") or "")),
        ("objects", _as_list(unit.get("objects"))),
        ("relations", _as_list(unit.get("relations"))),
        ("summary", _summary_for(unit)),
        ("source_session", str(unit.get("source_session") or "")),
        ("source_artifact", str(unit.get("source_artifact") or "")),
        ("produced_at", produced_at),
        ("content_hash", _unit_fingerprint(unit)),
    ]
    lines = ["---", *(f"{key}: {_front_matter_value(value)}" for key, value in fields), "---", ""]
    lines.append(f"# {topic}")
    lines.append("")
    lines.append(body)
    return "\n".join(lines).rstrip() + "\n"


def _fit_by_dropping_evidence(body: str, compose: Callable[[str], str], budget: int) -> Optional[str]:
    """Drop the body's oldest evidence lines until the page fits; ``None`` when it never does.

    Evidence lines are the bullet/numbered lines a page accumulates over time; the newest one is
    the most valuable, so removal starts at the top. One retry means one trimming pass — a body
    without evidence lines has nothing to drop and fails immediately.
    """
    lines = body.splitlines()
    while True:
        index = next((position for position, line in enumerate(lines)
                      if _EVIDENCE_LINE_RE.match(line)), None)
        if index is None:
            return None
        del lines[index]
        candidate = "\n".join(lines).strip("\n")
        if not candidate.strip():
            return None
        if estimate_tokens(compose(candidate)) <= budget:
            return candidate


def _atomic_write(path: Path, text: str) -> None:
    """temp file in the target directory + ``os.replace``: a reader never sees half a page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                             dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_failure(root: Optional[Path], unit: Mapping[str, Any], error: str) -> Optional[Path]:
    """Record a failed write beside the pages, never in place of one."""
    if root is None:
        return None
    try:
        category = str(unit.get("category") or "UNIT").strip().upper()
        if category not in CATEGORIES:
            category = "UNIT"
        topic = str(unit.get("topic") or "").strip()
        path = Path(root) / FAILED_DIRNAME / f"{category}-{slugify(topic) if topic else 'unit'}.json"
        payload = {"schema": SCHEMA, "failed_at": _now(), "error": str(error), "unit": dict(unit)}
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path
    except Exception:  # a failure record must never replace the failure's return value
        logger.warning("minir knowledge: cannot write failure record", exc_info=True)
        return None


def write_unit(unit: Any, *, language: str = "en", budget: int = DEFAULT_BUDGET,
               root: Any = None, render: Optional[RenderFn] = None) -> dict[str, Any]:
    """Create or atomically rewrite one knowledge page; never raises for a unit-level problem.

    Returns ``{"status", "path", "revisions", "error"}`` where status is one of ``created``,
    ``updated``, ``unchanged``, ``stale`` or ``failed``. A failed write keeps the previous file
    exactly as it was and records the error plus the original unit under ``_failed/``.
    """
    root_path: Optional[Path]
    try:
        root_path = Path(root) if root is not None else knowledge_dir()
    except Exception as exc:
        return {"status": "failed", "path": None, "revisions": 0, "error": str(exc)}
    try:
        return _write_unit(unit, root=root_path, language=language, budget=budget, render=render)
    except Exception as exc:
        error = str(exc) if isinstance(exc, KnowledgeError) else f"{type(exc).__name__}: {exc}"
        _write_failure(root_path, unit if isinstance(unit, Mapping) else {}, error)
        logger.warning("minir knowledge: write failed for %r: %s",
                       unit.get("topic") if isinstance(unit, Mapping) else unit, error)
        existing = None
        try:
            category, org, topic = _identity(unit)
            existing = _find_existing(root_path, category, org, topic)
        except Exception:
            pass
        return {"status": "failed", "path": existing, "revisions": 0, "error": error}


def _write_unit(unit: Any, *, root: Path, language: str, budget: int,
                render: Optional[RenderFn]) -> dict[str, Any]:
    category, org, topic = _identity(unit)
    existing = _find_existing(root, category, org, topic)
    existing_meta = read_front_matter(existing) if existing else {}
    stored_revisions = _revisions(existing_meta) if existing else 0

    if existing and _is_stale(unit.get("time"), existing_meta.get("time")):
        return {"status": "stale", "path": existing, "revisions": stored_revisions, "error": ""}

    fingerprint = _unit_fingerprint(unit)
    stored_hash = str(existing_meta.get("content_hash") or "") if existing else ""
    if existing and stored_hash == fingerprint:
        return {"status": "unchanged", "path": existing,
                "revisions": stored_revisions, "error": ""}

    body = _body_for(unit, render, language)
    if existing and not stored_hash:
        # A page written before fingerprints existed: the body is the only evidence of change.
        if _read_document(existing)[1].strip() == body.strip():
            return {"status": "unchanged", "path": existing,
                    "revisions": stored_revisions, "error": ""}
    revisions = stored_revisions + 1 if existing else 1
    updated_at = _now()
    produced_at = str(existing_meta.get("produced_at") or "") or updated_at
    limit = max(1, int(budget))

    def compose(candidate: str) -> str:
        return _compose(category=category, org=org, topic=topic, unit=unit, body=candidate,
                        revisions=revisions, updated_at=updated_at, produced_at=produced_at)

    text = compose(body)
    if estimate_tokens(text) > limit:
        trimmed = _fit_by_dropping_evidence(body, compose, limit)
        if trimmed is None:
            raise KnowledgeError(
                f"page exceeds the {limit}-token budget even after dropping evidence lines")
        body, text = trimmed, compose(trimmed)

    target = existing or _new_path(root, category, org, topic)
    _atomic_write(target, text)
    return {"status": "updated" if existing else "created", "path": target,
            "revisions": revisions, "error": ""}


# -- retrieval: paths only ---------------------------------------------------------------------


def _terms(text: Any) -> set[str]:
    """Comparison units: ASCII words of 2+ chars and CJK bigrams.

    CJK has no spaces, so per-word overlap would always be zero; bigrams give a deterministic
    overlap without a tokenizer dependency or an embedding model.
    """
    folded = str(text or "").casefold()
    terms = set(_WORD_RE.findall(folded))
    for run in _CJK_RUN_RE.findall(folded):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def _recency(updated_at: Any, now: Optional[float] = None) -> float:
    """Linear freshness over the last :data:`RECENCY_DAYS` days, in ``[0.0, 1.0]``."""
    stamp = _parse_time(updated_at)
    if stamp is None:
        return 0.0
    current = float(now) if now is not None else datetime.now(timezone.utc).timestamp()
    age_days = (current - stamp.timestamp()) / 86_400.0
    if age_days <= 0:
        return 1.0
    return max(0.0, 1.0 - age_days / RECENCY_DAYS)


def _row_line(row: Mapping[str, Any]) -> str:
    path = row.get("path", "")
    text = path.as_posix() if isinstance(path, Path) else str(path)
    return f"- {row.get('category', '')}/{row.get('org', '')} · {row.get('topic', '')} · {text}"


def _block_overhead() -> int:
    return len(BLOCK_HEADER) + len(BLOCK_FOOTER) + 2


def _recent_keys(recent: Iterable[Any]) -> set[str]:
    keys: set[str] = set()
    for item in recent or ():
        text = str(item or "").strip()
        if not text:
            continue
        keys.add(text.replace("\\", "/").casefold())
        keys.add(Path(text).name.casefold())
    return keys


def select_paths(query: str, knowledge_dir: Any, limit: int = DEFAULT_LIMIT,
                 max_chars: int = DEFAULT_MAX_CHARS,
                 recent: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Rank knowledge pages for one query, reading front-matter only.

    Score is exactly ``2 * objects_hit + 1 * word_overlap + 0.5 * recency``: an object named in
    the query is the strongest signal, a topic/summary word overlap the next, and freshness only
    breaks ties. Paths in ``recent`` were already delivered; they keep their score but sort after
    everything else, which lets the caller implement the "do not repeat three turns running" rule
    without hiding pages permanently. The selection also stops before the rendered block would
    exceed ``max_chars``, so a turn never gets a pointer list it cannot afford.
    """
    directory = Path(knowledge_dir)
    if not directory.is_dir():
        return []
    terms = _terms(query)
    if not terms:
        return []
    query_folded = str(query or "").casefold()
    recall = _recent_keys(recent)
    current = datetime.now(timezone.utc).timestamp()
    scored: list[tuple[float, bool, float, str, dict[str, Any]]] = []

    for org in ORGS:
        folder = directory / org
        if not folder.is_dir():
            continue
        try:
            pages = sorted(folder.glob("*.md"))
        except OSError:
            continue
        for path in pages:
            meta = read_front_matter(path)
            if not meta:
                continue
            category = str(meta.get("category") or "").strip().upper()
            if category not in CATEGORIES:
                continue
            topic = str(meta.get("topic") or "").strip()
            if not topic:
                continue
            relative = path.relative_to(directory).as_posix()
            hits = 0
            for item in _as_list(meta.get("objects")):
                name = str(item).partition(":")[2].strip() or str(item).strip()
                if len(name) >= 2 and name.casefold() in query_folded:
                    hits += 1
            overlap = len(terms & _terms(f"{topic} {meta.get('summary') or ''}"))
            freshness = _recency(meta.get("updated_at"), now=current)
            # Quantize so a few milliseconds of clock drift cannot outrank the recent-path
            # demotion: freshness is a tie-breaker, not a second relevance signal.
            score = round(2.0 * hits + 1.0 * overlap + 0.5 * freshness, 3)
            stamp = _parse_time(meta.get("updated_at"))
            row = {"category": category, "org": org, "topic": topic,
                   "path": relative, "updated_at": str(meta.get("updated_at") or "")}
            scored.append((score, relative.casefold() in recall or Path(relative).name.casefold()
                           in recall, stamp.timestamp() if stamp else 0.0, relative, row))

    scored.sort(key=lambda entry: (-entry[0], entry[1], -entry[2], entry[3]))
    selected: list[dict[str, Any]] = []
    used = _block_overhead()
    for _score, _seen, _stamp, _relative, row in scored:
        if len(selected) >= max(0, int(limit)):
            break
        cost = len(_row_line(row)) + 1
        if used + cost > max(0, int(max_chars)):
            continue
        selected.append(row)
        used += cost
    return selected


def render_paths_block(rows: Sequence[Mapping[str, Any]]) -> str:
    """The prefetch block: paths only, empty when there is nothing to point at.

    The literal head and tail are part of the contract — the main session is instructed to fetch
    a page only when it needs it, and the markers let it recognise the block. The whole block is
    capped at 700 characters; a row is shortened rather than allowed to push the tail out.
    """
    items = [row for row in rows or () if isinstance(row, Mapping)]
    if not items:
        return ""
    budget = MAX_BLOCK_CHARS - _block_overhead()
    used = 0
    lines: list[str] = []
    for row in items:
        remaining = budget - used
        if remaining <= 1:
            break
        line = _row_line(row)
        if len(line) > remaining:
            if remaining < 12:
                break
            line = line[:remaining - 1].rstrip() + "…"
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "\n".join([BLOCK_HEADER, *lines, BLOCK_FOOTER])


# -- production pipeline -----------------------------------------------------------------------

_STOP = object()


class KnowledgePipeline:
    """One daemon consumer and one bounded queue per profile.

    The producer (a base publish) only enqueues; it never waits for a render or a disk write. When
    the queue is full the oldest item is dropped and counted — the newest evidence is the evidence
    worth keeping. :meth:`stop` drains and joins, so a shutdown does not leave a thread writing to
    a directory the provider no longer owns.
    """

    def __init__(self, hermes_home: Any = "", *, root: Any = None, language: str = "en",
                 budget: int = DEFAULT_BUDGET, render: Optional[RenderFn] = None,
                 queue_size: int = QUEUE_MAX, autostart: bool = True):
        self.root = Path(root) if root is not None else knowledge_dir(hermes_home)
        self.language = str(language or "en")
        self.budget = max(1, int(budget))
        self.render = render
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._dropped = 0
        self._errors = 0
        self._counts: dict[str, int] = {}
        self._started = False
        self._stopped = False
        self._thread = threading.Thread(target=self._consume, name="minir-knowledge",
                                        daemon=True)
        if autostart:
            self.start()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> "KnowledgePipeline":
        with self._state_lock:
            if self._started or self._stopped:
                return self
            self._started = True
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Drain what is queued, wake the consumer, and join it; safe to call twice."""
        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
            started = self._started
        if not started:
            return
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            try:
                self._queue.get_nowait()
                with self._lock:
                    self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(_STOP)
            except queue.Full:
                pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def __enter__(self) -> "KnowledgePipeline":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # -- producer --------------------------------------------------------------

    def enqueue_from_refresh(self, session_id: str, value_markers: Iterable[Mapping[str, Any]]) -> int:
        """Queue the markers this refresh produced that are new or whose content changed.

        File existence plus the stored content fingerprint answer "already produced?"; there is no
        extra table to migrate. Every failure — a malformed unit, an unreadable page, a full
        queue — is logged or counted here and returns, never raised into the publish.
        """
        enqueued = 0
        try:
            for unit in list(value_markers or ()):
                if self._stopped:
                    break
                if not isinstance(unit, Mapping):
                    continue
                try:
                    if not self._needs_write(unit):
                        continue
                except Exception:
                    pass  # let the writer record why, instead of dropping the unit silently
                item = dict(unit)
                if not item.get("source_session"):
                    item["source_session"] = str(session_id or "")
                if self._put(item):
                    enqueued += 1
        except Exception:
            logger.warning("minir knowledge: enqueue failed", exc_info=True)
        return enqueued

    def _needs_write(self, unit: Mapping[str, Any]) -> bool:
        category, org, topic = _identity(unit)
        existing = _find_existing(self.root, category, org, topic)
        if existing is None:
            return True
        meta = read_front_matter(existing)
        stored = str(meta.get("content_hash") or "")
        if stored:
            return stored != _unit_fingerprint(unit)
        return _read_document(existing)[1].strip() != _body_for(unit, None, self.language).strip()

    def _put(self, item: Mapping[str, Any]) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
            with self._lock:
                self._dropped += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    # -- consumer --------------------------------------------------------------

    def _consume(self) -> None:
        while True:
            if self._stopped and self._queue.empty():
                return
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stopped:
                    return
                continue
            try:
                if item is _STOP:
                    return
                result = write_unit(item, language=self.language, budget=self.budget,
                                    root=self.root, render=self.render)
                with self._lock:
                    status = str(result.get("status") or "failed")
                    self._counts[status] = self._counts.get(status, 0) + 1
            except Exception:
                with self._lock:
                    self._errors += 1
                logger.warning("minir knowledge: consumer failed on one unit", exc_info=True)
            finally:
                self._queue.task_done()

    # -- observability ---------------------------------------------------------

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def dropped_count(self) -> int:
        return self.dropped

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts = dict(self._counts)
            dropped, errors = self._dropped, self._errors
        return {"pending": self._queue.qsize(), "dropped": dropped,
                "created": counts.get("created", 0), "updated": counts.get("updated", 0),
                "unchanged": counts.get("unchanged", 0), "stale": counts.get("stale", 0),
                "failed": counts.get("failed", 0), "errors": errors}
