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
* **ripgrep finds, front-matter scores.** :func:`select_paths` asks the host's managed ``rg``
  (see :mod:`minir.rgtool`) which pages mention a query term and reads only those heads, so a
  turn pays for metadata, not for every page body. A host without ripgrep keeps the org-folder
  scan; either way the score comes from the front-matter, never from the hit. There is no index
  database and no embedding model.
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
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import rgtool
from .layers import CATEGORIES, EPISTEMIC, ORGS, RELATION_TYPES
from .rules import estimate_tokens

logger = logging.getLogger(__name__)

SCHEMA = "minir-knowledge/1"
KNOWLEDGE_DIRNAME = Path("minir-memory") / "knowledge"
FAILED_DIRNAME = "_failed"

SLUG_MAX = 60
DEFAULT_BUDGET = 2000
DEFAULT_LIMIT = 6
DEFAULT_MAX_CHARS = 700
DEFAULT_SEARCH_LIMIT = 8
MATCH_PREVIEW = 240
MAX_BLOCK_CHARS = 700
QUEUE_MAX = 512
RECENCY_DAYS = 30.0
SUMMARY_MAX = 120
PAGE_BODY_MAX = 1200          # the same cap the B4 prompt states; points, not prose
PAGE_BODY_MIN_LINES = 3
PAGE_BODY_MAX_LINES = 8
PAGE_LINE_MAX = 200
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
    Two topics that normalize to the same slug are the same page path, so the second one is a
    revision of the first rather than a second file.
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


def anchor_in_window(unit: Mapping[str, Any], from_ts: float, to_ts: float) -> bool:
    """True when the unit's anchor overlaps the window it is being enqueued for.

    An inherited marker keeps the anchor of the window that produced it, so a refresh cannot
    attribute another session's work to this one. A unit without a numeric anchor is not
    enqueued: the writer that produced it already wrote the page, and a guess here is exactly
    the misattribution this filter exists to prevent.
    """
    anchor = unit.get("anchor")
    if not isinstance(anchor, Mapping):
        return False
    try:
        start = float(anchor.get("from_ts"))
        end = float(anchor.get("to_ts"))
    except (TypeError, ValueError):
        return False
    if end < start:
        start, end = end, start
    return end >= float(from_ts) and start <= float(to_ts)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _revisions(meta: Mapping[str, Any]) -> int:
    try:
        return max(0, int(str(meta.get("revisions") or "0").strip() or 0))
    except (TypeError, ValueError):
        return 1


# -- paths -------------------------------------------------------------------------------------


def normalize_path(path: Any) -> str:
    """A page identity: ``<org>/<CATEGORY>-<slug>.md`` under the knowledge root.

    The relative path is the only identity a page has. Front-matter ``(category, org, topic)``
    is metadata: rendered, indexed and declared to the edge builder, but it never decides which
    file a unit belongs to.
    """
    raw = str(path or "").strip()
    if not raw:
        raise KnowledgeError("page path is required")
    if "\\" in raw or ":" in raw or raw.startswith("/"):
        raise KnowledgeError(f"page path must be a relative POSIX path, got {raw!r}")
    parts = PurePosixPath(raw).parts
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise KnowledgeError(f"page path must be <org>/<CATEGORY>-<slug>.md, got {raw!r}")
    org, name = parts
    if org not in ORGS:
        raise KnowledgeError(f"page path org must be one of: {', '.join(ORGS)}; got {org!r}")
    if not name.endswith(".md"):
        raise KnowledgeError(f"page path must end in .md, got {raw!r}")
    category, sep, slug = name[:-3].partition("-")
    if not sep or category.upper() not in CATEGORIES or not slug:
        raise KnowledgeError(f"page file must be <CATEGORY>-<slug>.md, got {name!r}")
    return f"{org}/{name}"


def resolve_path(org: Any, category: Any, topic: Any) -> str:
    """The canonical path for a unit that has no page yet; the writer never guesses one."""
    org_text = str(org or "").strip()
    category_text = str(category or "").strip().upper()
    if org_text not in ORGS:
        raise KnowledgeError(f"org must be one of: {', '.join(ORGS)}; got {org_text!r}")
    if category_text not in CATEGORIES:
        raise KnowledgeError(f"unknown category {category!r}")
    if not str(topic or "").strip():
        raise KnowledgeError("topic is required to resolve a page path")
    return normalize_path(f"{org_text}/{render_filename(category_text, topic)}")



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


def _write_failure(root: Optional[Path], unit: Mapping[str, Any], error: str, *,
                   op: str = "write", path: str = "") -> Optional[Path]:
    """Record a failed write beside the pages, never in place of one.

    The record names the version it could not write: op, relative path, the file's current
    sha256 and body line count. Without those a later reader cannot tell which version failed.
    """
    if root is None:
        return None
    try:
        rel = str(path or "").strip()
        try:
            rel = normalize_path(unit.get("path"))
        except KnowledgeError:
            pass
        target = Path(root) / Path(rel) if rel else None
        sha = ""
        lines = 0
        if target is not None and target.is_file():
            raw = target.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            lines = len(raw.decode("utf-8", "replace").splitlines())
        category = str(unit.get("category") or "UNIT").strip().upper()
        if category not in CATEGORIES:
            category = "UNIT"
        topic = str(unit.get("topic") or "").strip()
        stem = slugify(topic) if topic else (
            rel.rsplit("/", 1)[-1][:-3].partition("-")[2] if rel.endswith(".md") else "unit")
        path_out = Path(root) / FAILED_DIRNAME / f"{category}-{stem or 'unit'}.json"
        payload = {"schema": SCHEMA, "failed_at": _now(), "op": str(op or "write"),
                   "path": rel, "sha256": sha, "lines": lines, "error": str(error),
                   "unit": dict(unit)}
        _atomic_write(path_out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path_out
    except Exception:  # a failure record must never replace the failure's return value
        logger.warning("minir knowledge: cannot write failure record", exc_info=True)
        return None


def write_unit(unit: Any, *, language: str = "en", budget: int = DEFAULT_BUDGET,
               root: Any = None, render: Optional[RenderFn] = None) -> dict[str, Any]:
    """Create or atomically rewrite one knowledge page; never raises for a unit-level problem.

    ``unit["path"]`` is the page identity (relative to the knowledge root); a unit without a
    valid one is a failure, not a new page. Returns ``{"status", "path", "revisions", "error"}``
    where status is one of ``created``, ``updated``, ``unchanged``, ``stale`` or ``failed``, and
    ``path`` is the relative path. A failed write keeps the previous file exactly as it was and
    records the error plus the original unit under ``_failed/``.
    """
    root_path: Optional[Path]
    try:
        root_path = Path(root) if root is not None else knowledge_dir()
    except Exception as exc:
        return {"status": "failed", "path": "", "revisions": 0, "error": str(exc)}
    try:
        return _write_unit(unit, root=root_path, language=language, budget=budget, render=render)
    except Exception as exc:
        error = str(exc) if isinstance(exc, KnowledgeError) else f"{type(exc).__name__}: {exc}"
        _write_failure(root_path, unit if isinstance(unit, Mapping) else {}, error)
        logger.warning("minir knowledge: write failed for %r: %s",
                       unit.get("topic") if isinstance(unit, Mapping) else unit, error)
        rel = ""
        if isinstance(unit, Mapping):
            try:
                rel = normalize_path(unit.get("path"))
            except Exception:
                rel = str(unit.get("path") or "")
        return {"status": "failed", "path": rel, "revisions": 0, "error": error}


def _is_date(value: Any) -> bool:
    try:
        datetime.strptime(str(value or "").strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def page_edges(rel_path: Any, meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The edges a page declares: every object is ``instance_of``, every relation its own type.

    The page file is the source; this function never invents an edge the front matter does not
    state. A declared target without a matching conclusion stays in the table and is dropped by
    the expansion join and the maintenance sweep, so a dangling edge is visible, not silent.
    """
    path = normalize_path(rel_path)
    edges: list[dict[str, Any]] = []
    for entry in meta.get("objects") or []:
        kind, sep, key = str(entry).partition(":")
        if sep and kind.strip() and key.strip():
            edges.append({"src": ("page", path), "rel": "instance_of",
                          "dst": (kind.strip(), key.strip())})
    for entry in meta.get("relations") or []:
        if not isinstance(entry, Mapping):
            continue
        relation = str(entry.get("type") or "").strip().lower()
        kind, sep, key = str(entry.get("ref") or "").partition(":")
        if relation in RELATION_TYPES and sep and kind.strip() and key.strip():
            edges.append({"src": ("page", path), "rel": relation,
                          "dst": (kind.strip(), key.strip())})
    return edges


def derived_edges(root: Any = None) -> list[dict[str, Any]]:
    """Every page-declared edge under the knowledge root, sorted for a stable rebuild."""
    base = Path(root) if root is not None else knowledge_dir()
    edges: list[dict[str, Any]] = []
    for org in ORGS:
        folder = base / org
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            meta = read_front_matter(path)
            if meta:
                edges.extend(page_edges(f"{org}/{path.name}", meta))
    return edges


def page_topics(root: Any = None) -> list[dict[str, str]]:
    """Every page's declared topic, for the offline merge-candidate pass."""
    base = Path(root) if root is not None else knowledge_dir()
    topics: list[dict[str, str]] = []
    for org in ORGS:
        folder = base / org
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            meta = read_front_matter(path)
            topic = " ".join(str(meta.get("topic") or "").split())
            if topic:
                topics.append({"path": f"{org}/{path.name}", "topic": topic})
    return topics


def _validate_org_shape(org: str, unit: Mapping[str, Any]) -> None:
    """The org's shape contract, as far as a writer can check it without reading meaning.

    The rules file decides the org; these are the structural minimums a wrong choice fails. They
    are shapes, not proof: a projects unit that lists three lines can still miss its goal, and
    that stays a review finding rather than a write gate.
    """
    lines = [line for line in str(unit.get("content") or "").splitlines() if line.strip()]
    if org == "events":
        if not _is_date(unit.get("time")):
            raise KnowledgeError("an events unit needs a dated time (YYYY-MM-DD)")
        if not [item for item in unit.get("objects") or [] if str(item).strip()]:
            raise KnowledgeError("an events unit needs at least one object")
        if len(lines) < 2:
            raise KnowledgeError("an events unit needs the action and its result")
    if org == "projects":
        if len(lines) < 3:
            raise KnowledgeError(
                "a projects unit needs a goal, a deliverable and a current state (3 lines)")


def run_value_markers(units: Iterable[Any], *, root: Any = None, language: str = "en",
                      budget: int = DEFAULT_BUDGET, session_id: str = "", source: str = "",
                      render: Optional[RenderFn] = None) -> list[dict[str, Any]]:
    """The single writer for every source: window refresh, note extraction, remember queue.

    Each result carries the unit's ``topic``/``path`` beside the write status, so ``page_writes``
    and ``_failed`` describe exactly the attempts that happened, retries included. Running the
    same units twice is a no-op through the stored content hash, so a retry cannot duplicate a
    page.
    """
    results: list[dict[str, Any]] = []
    for raw in list(units or ()):
        if not isinstance(raw, Mapping):
            results.append({"topic": "", "path": "", "status": "failed", "revisions": 0,
                            "error": "unit must be an object"})
            continue
        unit = dict(raw)
        unit.setdefault("source_session", str(session_id or ""))
        if source:
            unit.setdefault("source_artifact", str(source))
        result = write_unit(unit, language=language, budget=budget, root=root, render=render)
        results.append({
            "topic": " ".join(str(unit.get("topic") or "").split()),
            "path": str(result.get("path") or unit.get("path") or ""),
            "status": str(result.get("status") or "failed"),
            "revisions": int(result.get("revisions") or 0),
            "error": str(result.get("error") or ""),
        })
    return results


def _write_unit(unit: Any, *, root: Path, language: str, budget: int,
                render: Optional[RenderFn]) -> dict[str, Any]:
    if not isinstance(unit, Mapping):
        raise KnowledgeError("unit must be an object")
    rel = normalize_path(unit.get("path"))
    org, name = rel.split("/", 1)
    category, _sep, slug = name[:-3].partition("-")
    category = category.upper()
    topic = " ".join(str(unit.get("topic") or "").split())
    if not topic:
        raise KnowledgeError("unit topic is empty")
    if slugify(topic) != slug:
        raise KnowledgeError(f"topic {topic!r} does not match page path {rel!r}")
    if org == "events" and category == "KNOWLEDGE":
        unit = {**unit, "epistemic": "fact"}  # an occurrence is observed, never inferred
    _validate_org_shape(org, unit)
    target = Path(root) / Path(rel)
    existing = target if target.is_file() else None
    existing_meta = read_front_matter(existing) if existing else {}
    stored_revisions = _revisions(existing_meta) if existing else 0

    if existing and _is_stale(unit.get("time"), existing_meta.get("time")):
        return {"status": "stale", "path": rel, "revisions": stored_revisions, "error": ""}

    fingerprint = _unit_fingerprint(unit)
    stored_hash = str(existing_meta.get("content_hash") or "") if existing else ""
    if existing and stored_hash == fingerprint:
        return {"status": "unchanged", "path": rel,
                "revisions": stored_revisions, "error": ""}

    body = _body_for(unit, render, language)
    if existing and not stored_hash:
        # A page written before fingerprints existed: the body is the only evidence of change.
        if _read_document(existing)[1].strip() == body.strip():
            return {"status": "unchanged", "path": rel,
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

    _atomic_write(target, text)
    return {"status": "updated" if existing else "created", "path": rel,
            "revisions": revisions, "error": ""}


# -- file-level editing ------------------------------------------------------------------------

OPS: tuple[str, ...] = ("create", "append", "insert", "replace", "delete")
ARCHIVE_DIRNAME = "_archive"


def _page_target(root: Path, path: Any) -> tuple[Path, str, str, str, str]:
    """A page path -> (absolute, relative, org, category, slug); reject anything else."""
    rel_text = normalize_path(path)
    rel = PurePosixPath(rel_text)
    org, name = rel.parts
    category, _sep, slug = name[:-3].partition("-")
    return Path(root) / Path(rel_text), rel_text, org, category.upper(), slug


def _body_lines(body: str) -> list[str]:
    return body.splitlines()


def _split_heading(body: str) -> tuple[str, str]:
    """A page body -> (``# topic`` heading, content below it)."""
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        rest = lines[1:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        return lines[0], "\n".join(rest).strip("\n")
    return "", body.strip("\n")


def _set_front_matter(text: str, updates: Mapping[str, Any]) -> str:
    """Replace keys in an existing front-matter block, keeping every other line untouched."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KnowledgeError("page has no front-matter block")
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise KnowledgeError("page front-matter is not closed")
    remaining = dict(updates)
    for index in range(1, end):
        key = lines[index].partition(":")[0].strip()
        if key in remaining:
            lines[index] = f"{key}: {_front_matter_value(remaining.pop(key))}"
    insert_at = end
    for key, value in remaining.items():
        lines.insert(insert_at, f"{key}: {_front_matter_value(value)}")
        insert_at += 1
    return "\n".join(lines).rstrip() + "\n"


def _page_warnings(lines: Sequence[str]) -> list[str]:
    warnings: list[str] = []
    if len(lines) > PAGE_BODY_MAX_LINES:
        warnings.append(f"body has {len(lines)} lines; the page contract allows at most "
                        f"{PAGE_BODY_MAX_LINES}")
    if any(len(line) > PAGE_LINE_MAX for line in lines):
        warnings.append(f"a line is longer than {PAGE_LINE_MAX} characters")
    if any(line.lstrip().startswith(("本页", "我们", "本次")) for line in lines[:1]):
        warnings.append("the body opens with a preamble instead of a claim")
    return warnings


def _edit_failure(root: Path, category: str, topic: str, op: str, path: str,
                  error: str) -> dict[str, Any]:
    _write_failure(root, {"category": category, "topic": topic, "op": op, "path": path}, error)
    return {"status": "failed", "file": Path(path).name, "path": path, "org": "",
            "category": category, "topic": topic, "lines": 0, "bytes": 0,
            "sha256": "", "verified": False, "restored": False, "warnings": [],
            "error": error}


def edit_page(*, root: Any, path: Any, op: str, header: Optional[Mapping[str, Any]] = None,
              content: str = "", line: int = 0, end_line: int = 0, old: str = "",
              position: str = "after", expect_sha: str = "", language: str = "en",
              budget: int = DEFAULT_BUDGET, source_session: str = "") -> dict[str, Any]:
    """One file-level page edit; reads, writes atomically, verifies, and rolls back on mismatch.

    ``create`` renders the header through :func:`write_unit`; every other op addresses the body
    by 1-based line or by exact unique text. A failed verification restores the previous bytes.
    """
    root_path = Path(root)
    header = dict(header or {})
    try:
        target, rel, org, category, slug = _page_target(root_path, path)
    except KnowledgeError as exc:
        return _edit_failure(root_path, "", "", str(op or ""), str(path or ""), str(exc))
    if op not in OPS:
        return _edit_failure(root_path, category, slug, str(op), rel,
                             f"op must be one of: {', '.join(OPS)}")
    if op == "create":
        return _create_page(root_path, target, rel, org, category, slug, header, content,
                            language=language, budget=budget, source_session=source_session)
    if not target.is_file():
        return _edit_failure(root_path, category, slug, op, rel, "page does not exist")

    original = target.read_text(encoding="utf-8-sig", errors="replace")
    current_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
    meta, body = _read_document(target)
    heading, page_body = _split_heading(body)
    body_lines = page_body.splitlines()
    if expect_sha and expect_sha != current_sha:
        return {"status": "stale", "file": target.name, "path": rel, "org": org,
                "category": category, "topic": str(meta.get("topic") or slug),
                "lines": len(body_lines), "bytes": len(page_body.encode("utf-8")),
                "sha256": current_sha, "verified": False, "restored": False,
                "warnings": [], "error": "the file changed since it was read",
                "content": page_body}
    if not meta:
        return _edit_failure(root_path, category, str(meta.get("topic") or slug), op, rel,
                             "page has no parseable front-matter")
    topic = " ".join(str(meta.get("topic") or "").split())
    if (str(meta.get("org") or "") != org or str(meta.get("category") or "").upper() != category
            or slugify(topic) != slug):
        return _edit_failure(root_path, category, topic or slug, op, rel,
                             "front-matter does not match the path")

    try:
        start, end = _edit_anchor(body_lines, op=op, line=line, end_line=end_line, old=old)
    except KnowledgeError as exc:
        if str(exc) == "__absent__":
            return _edit_unchanged(target, rel, org, category, topic or slug, body_lines,
                                   current_sha)
        return _edit_failure(root_path, category, topic or slug, op, rel, str(exc))

    new_lines = content.strip("\n").splitlines() if content.strip("\n") else []
    if op != "delete" and not new_lines:
        return _edit_failure(root_path, category, topic or slug, op, rel, "content is empty")
    if op == "append":
        if body_lines[-len(new_lines):] == new_lines:
            return _edit_unchanged(target, rel, org, category, topic, body_lines, current_sha)
        updated = body_lines + new_lines
    elif op == "insert":
        if position not in ("before", "after"):
            return _edit_failure(root_path, category, topic or slug, op, rel,
                                 "position must be before or after")
        at = start - 1 if position == "before" else end
        if body_lines[at:at + len(new_lines)] == new_lines:
            return _edit_unchanged(target, rel, org, category, topic, body_lines, current_sha)
        updated = body_lines[:at] + new_lines + body_lines[at:]
    elif op == "replace":
        if body_lines[start - 1:end] == new_lines:
            return _edit_unchanged(target, rel, org, category, topic, body_lines, current_sha)
        updated = body_lines[:start - 1] + new_lines + body_lines[end:]
    else:  # delete
        updated = body_lines[:start - 1] + body_lines[end:]

    expected = "\n".join(updated).strip("\n")
    text = _set_front_matter(original, {
        "updated_at": _now(), "revisions": _revisions(meta) + 1,
    })
    head, _old_body = _read_document_text(text)
    prefix = head + "\n\n" if head else ""
    if heading:
        prefix += heading + "\n\n"
    new_text = prefix + (expected + "\n" if expected else "")
    warnings = _page_warnings(updated)
    try:
        _atomic_write(target, new_text)
    except OSError as exc:
        return _edit_failure(root_path, category, topic or slug, op, rel,
                             f"write failed: {type(exc).__name__}: {exc}")

    verify_meta, verify_body = _read_document(target)
    _verify_heading, verify_content = _split_heading(verify_body)
    verified = (verify_content.strip("\n") == expected
                and _revisions(verify_meta) == _revisions(meta) + 1)
    if not verified:
        try:
            _atomic_write(target, original)
        except OSError:
            pass
        error = "the write did not match its intent; the previous bytes were restored"
        _write_failure(root_path, {"category": category, "topic": topic or slug, "op": op,
                                   "path": rel}, error)
        return {"status": "unverified", "file": target.name, "path": rel, "org": org,
                "category": category, "topic": topic or slug, "lines": len(updated),
                "bytes": len(expected.encode("utf-8")), "sha256": current_sha,
                "lines_before": len(body_lines), "lines_after": len(updated),
                "verified": False, "restored": True, "warnings": warnings, "error": error}
    final_text = target.read_text(encoding="utf-8-sig", errors="replace")
    return {"status": "written", "file": target.name, "path": rel, "org": org,
            "category": category, "topic": topic or slug, "lines": len(updated),
            "bytes": len(expected.encode("utf-8")),
            "sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            "lines_before": len(body_lines), "lines_after": len(updated), "verified": True,
            "restored": False, "warnings": warnings, "error": ""}


def archive_page(root: Any, path: Any, *, reason: str = "") -> dict[str, Any]:
    """Move one page under ``_archive/`` and mark it archived; never deletes the text."""
    root_path = Path(root)
    try:
        target, rel, _org, _category, _slug = _page_target(root_path, path)
    except KnowledgeError as exc:
        return {"status": "failed", "path": str(path or ""), "error": str(exc)}
    if not target.is_file():
        return {"status": "missing", "path": rel, "error": "page does not exist"}
    original = target.read_text(encoding="utf-8-sig", errors="replace")
    try:
        text = _set_front_matter(original, {"archived_at": _now(), "archive_reason": reason})
    except KnowledgeError as exc:
        return {"status": "failed", "path": rel, "error": str(exc)}
    archived = root_path / ARCHIVE_DIRNAME / rel
    try:
        _atomic_write(archived, text)
        target.unlink()
    except OSError as exc:
        return {"status": "failed", "path": rel, "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "archived", "path": rel,
            "archived_path": archived.relative_to(root_path).as_posix()}


def _read_document_text(text: str) -> tuple[str, str]:
    """A full page text -> (front-matter block including fences, body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return "", text
    return "\n".join(lines[:end + 1]), "\n".join(lines[end + 1:]).lstrip("\n")


def _edit_anchor(lines: Sequence[str], *, op: str, line: int, end_line: int,
                 old: str) -> tuple[int, int]:
    """Resolve one edit anchor to a 1-based inclusive body-line range; append needs none."""
    if line and old:
        raise KnowledgeError("give either line or old, not both")
    if not old and not line:
        if op == "append":
            return 0, 0
        raise KnowledgeError("line or old is required")
    if old:
        hits = [index + 1 for index, text in enumerate(lines) if text == old]
        if len(hits) == 1:
            return hits[0], hits[0]
        if not hits and op == "delete":
            raise KnowledgeError("__absent__")
        raise KnowledgeError(
            f"old matches {len(hits)} lines; it must match exactly one (candidates: "
            f"{', '.join(str(hit) for hit in hits[:5]) or 'none'})")
    if not line:
        raise KnowledgeError("line or old is required")
    start = int(line)
    end = int(end_line or line)
    if start < 1 or end < start or end > len(lines):
        raise KnowledgeError(f"line range {start}..{end} is outside the body ({len(lines)} lines)")
    return start, end


def _edit_unchanged(target: Path, rel: str, org: str, category: str, topic: str,
                    lines: Sequence[str], sha: str) -> dict[str, Any]:
    return {"status": "unchanged", "file": target.name, "path": rel, "org": org,
            "category": category, "topic": topic, "lines": len(lines),
            "bytes": len("\n".join(lines).encode("utf-8")), "sha256": sha,
            "verified": True, "restored": False, "warnings": [], "error": ""}


def _create_page(root: Path, target: Path, rel: str, org: str, category: str, slug: str,
                 header: Mapping[str, Any], content: str, *, language: str, budget: int,
                 source_session: str) -> dict[str, Any]:
    topic = " ".join(str(header.get("topic") or "").split())
    if target.exists():
        _meta, body = _read_document(target)
        _heading, existing = _split_heading(body)
        if existing.strip() == content.strip():
            return _edit_unchanged(target, rel, org, category, topic or slug,
                                   existing.splitlines(), hashlib.sha256(
                                       target.read_bytes()).hexdigest())
        return {"status": "exists", "file": target.name, "path": rel, "org": org,
                "category": category, "topic": topic or slug, "lines": 0, "bytes": 0,
                "sha256": "", "verified": False, "restored": False, "warnings": [],
                "error": "page exists; use replace/append instead of create"}
    if not topic or len(topic) < 2 or len(topic) > 80:
        return _edit_failure(root, category, topic, "create", rel,
                            "topic must be 2..80 characters")
    if slugify(topic) != slug:
        return _edit_failure(root, category, topic, "create", rel,
                            "the file stem must equal slugify(topic)")
    lines = content.strip("\n").splitlines() if content.strip("\n") else []
    if len(lines) < PAGE_BODY_MIN_LINES or len(lines) > PAGE_BODY_MAX_LINES:
        return _edit_failure(root, category, topic, "create", rel,
                            f"body must be {PAGE_BODY_MIN_LINES}..{PAGE_BODY_MAX_LINES} lines")
    if len(content.strip("\n")) > PAGE_BODY_MAX:
        return _edit_failure(root, category, topic, "create", rel,
                            f"body exceeds {PAGE_BODY_MAX} characters")
    summary = " ".join(str(header.get("summary") or "").split())
    if not summary or len(summary) > SUMMARY_MAX:
        return _edit_failure(root, category, topic, "create", rel,
                            f"summary is required and must be at most {SUMMARY_MAX} characters")
    epistemic = str(header.get("epistemic") or "").strip().lower()
    if category == "KNOWLEDGE" and epistemic not in EPISTEMIC:
        return _edit_failure(root, category, topic, "create", rel,
                            f"KNOWLEDGE requires epistemic in {', '.join(EPISTEMIC)}")
    unit = {
        "path": rel,
        "category": category, "org": org, "topic": topic, "summary": summary,
        "epistemic": epistemic if category == "KNOWLEDGE" else "",
        "time": str(header.get("time") or ""),
        "objects": header.get("objects") or [],
        "relations": header.get("relations") or [],
        "content": "\n".join(lines), "source_session": source_session,
    }
    result = write_unit(unit, language=language, budget=budget, root=root)
    status = str(result.get("status") or "failed")
    written = Path(root) / Path(rel)
    if status not in ("created", "updated", "unchanged"):
        return _edit_failure(root, category, topic, "create", rel,
                             str(result.get("error") or status))
    text = written.read_text(encoding="utf-8-sig", errors="replace")
    _meta, body = _read_document(written)
    _heading, written_body = _split_heading(body)
    return {"status": "created" if status != "unchanged" else "unchanged",
            "file": written.name, "path": rel, "org": org, "category": category,
            "topic": topic, "lines": len(written_body.splitlines()),
            "bytes": len(written_body.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "lines_before": 0, "lines_after": len(written_body.splitlines()), "verified": True,
            "restored": False, "warnings": _page_warnings(written_body.splitlines()),
            "error": ""}


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


def _scan_pages(directory: Path) -> list[Path]:
    """Every page under a known org: the superset used when ripgrep cannot run."""
    pages: list[Path] = []
    for org in ORGS:
        try:
            pages.extend(sorted((directory / org).glob("*.md")))
        except OSError:  # an unreadable org directory must not empty the whole index
            continue
    return pages


def _candidate_pages(directory: Path, terms: set[str], hermes_home: Any) -> list[Path]:
    """Pages worth scoring: ripgrep's term hits, or every page when ripgrep cannot run.

    The pattern is the same term set the scorer uses, escaped and OR-joined, so ripgrep can only
    narrow what the score would accept: a page carrying an object hit or a topic/summary overlap
    already contains that text. ``None`` from :func:`minir.rgtool.search` means no binary, an
    error exit or a timeout, and then :func:`_scan_pages` returns the superset instead — the
    selection must not go blind because a tool directory moved.

    Only ``<org>/<page>.md`` under a known org is a page: ripgrep also reports anything else a
    stray ``*.md`` under the root matches, and those are not part of the index.
    """
    pattern = "|".join(re.escape(term)
                       for term in sorted(terms, key=lambda term: (-len(term), term)))
    hits = rgtool.search(pattern, directory, glob="*.md", ignore_case=True,
                         hermes_home=hermes_home)
    if hits is None:
        return _scan_pages(directory)
    pages: list[Path] = []
    for hit in hits:
        path = hit if hit.is_absolute() else directory / hit
        try:
            relative = path.relative_to(directory)
        except ValueError:
            continue
        if len(relative.parts) == 2 and relative.parts[0] in ORGS:
            pages.append(path)
    return pages


def select_paths(query: str, knowledge_dir: Any, limit: int = DEFAULT_LIMIT,
                 max_chars: int = DEFAULT_MAX_CHARS,
                 recent: Sequence[Any] = (), hermes_home: Any = "") -> list[dict[str, Any]]:
    """Rank knowledge pages for one query, reading front-matter only.

    Score is exactly ``2 * objects_hit + 1 * word_overlap + 0.5 * recency``: an object named in
    the query is the strongest signal, a topic/summary word overlap the next, and freshness only
    breaks ties. The candidates come from the host's ``rg`` (``hermes_home`` locates its tool
    store) and are scored on their front-matter; without ``rg`` every page under a known org is
    a candidate. Paths in ``recent`` were already delivered; they keep their score but sort after
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

    for path in _candidate_pages(directory, terms, hermes_home):
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
        row = {"category": category, "org": relative.split("/")[0], "topic": topic,
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


def _body_start(path: Path) -> int:
    """1-based number of the first body line; 1 when the file has no complete front-matter.

    The result rows quote body lines before front-matter lines, because the front-matter fields
    already travel as ``topic``/``summary``/``updated_at``; without this a page whose topic and
    objects match would spend its whole snippet budget on metadata.
    """
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return 1
    if not lines or lines[0].strip() != "---":
        return 1
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    return 1 if end is None else end + 2


def _page_row(path: Path, relative: str, category: str) -> Optional[dict[str, Any]]:
    """One search result from a page's front-matter, with the body a writer can edit."""
    meta = read_front_matter(path)
    if not meta:
        return None
    page_category = str(meta.get("category") or "").strip().upper()
    if page_category not in CATEGORIES or (category and page_category != category):
        return None
    topic = str(meta.get("topic") or "").strip()
    if not topic:
        return None
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    _meta, body = _read_document(path)
    _heading, content = _split_heading(body)
    body_lines = content.splitlines()
    truncated = len(content) > PAGE_BODY_MAX or len(body_lines) > 40
    shown = body_lines[:40]
    if len(content) > PAGE_BODY_MAX:
        content = content[:PAGE_BODY_MAX].rstrip() + "…"
    return {"file": path.name, "path": relative, "org": relative.split("/")[0],
            "category": page_category, "topic": topic,
            "summary": str(meta.get("summary") or ""), "revisions": _revisions(meta),
            "updated_at": str(meta.get("updated_at") or ""),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "match": None, "lines": [{"n": index + 1, "text": text}
                                     for index, text in enumerate(shown)],
            "content": content, "bytes": len(content.encode("utf-8")),
            "truncated": truncated}


def search_pages(pattern: str, knowledge_dir: Any, *, org: str = "", category: str = "",
                 limit: int = DEFAULT_SEARCH_LIMIT, per_page: int = 2,
                 hermes_home: Any = "") -> Optional[dict[str, Any]]:
    """Pages whose text matches *pattern*, with the matched lines; ``None`` without ripgrep.

    :func:`select_paths` only hints at pages for one turn; this is the explicit lookup behind the
    ``minir_kb_search`` tool, so the pattern is the caller's own instead of a term set derived
    from a prompt. Scope and page rules are the selector's: an ``<org>/<page>.md`` under
    :data:`ORGS` with a known category and a topic. ``org``/``category`` filter front-matter,
    ``per_page`` caps the lines quoted from one page (body lines first, since the front-matter
    already travels as fields; a page that matches only in its front-matter still shows that
    line), and ``total`` counts every matching page before ``limit`` — a caller seeing
    ``total > count`` should narrow the pattern rather than raise the limit.

    ``None`` means ripgrep is missing or refused the pattern (Rust regex syntax, so no lookaround
    or backreferences). A bad ``org`` or ``category`` raises ``ValueError``: that is a caller
    mistake with a fix the model can apply.
    """
    if org and org not in ORGS:
        raise ValueError(f"org must be one of: {', '.join(ORGS)}")
    wanted = str(category or "").strip().upper()
    if wanted and wanted not in CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
    directory = Path(knowledge_dir)
    found: dict[str, Any] = {"root": str(directory), "count": 0, "total": 0, "results": []}
    if not directory.is_dir():
        return found
    hits = rgtool.matches(pattern, directory, glob="*.md", ignore_case=True,
                          max_per_file=max(per_page * 4, 12), hermes_home=hermes_home)
    if hits is None:
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for hit in hits:
        path = Path(hit["path"])
        try:
            relative = path.relative_to(directory)
        except ValueError:
            continue
        if len(relative.parts) != 2 or relative.parts[0] not in ORGS:
            continue
        if org and relative.parts[0] != org:
            continue
        key = relative.as_posix()
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(hit)
    pages: list[dict[str, Any]] = []
    for key in order:
        path = directory / key
        row = _page_row(path, key, wanted)
        if row is None:
            continue
        body_start = _body_start(path)
        quoted = [hit for hit in grouped[key] if int(hit["line"]) >= body_start]
        first = (quoted or grouped[key])[0]
        line_no = int(first["line"])
        row["match"] = {"line": line_no - body_start - 1 if quoted else line_no,
                        "text": str(first["text"])[:MATCH_PREVIEW]}
        pages.append(row)
    found.update(count=len(pages[:max(1, limit)]), total=len(pages),
                 results=pages[:max(1, limit)])
    return found


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
                 queue_size: int = QUEUE_MAX, autostart: bool = True,
                 on_written: Optional[Callable[[str], None]] = None):
        self.root = Path(root) if root is not None else knowledge_dir(hermes_home)
        self.language = str(language or "en")
        self.budget = max(1, int(budget))
        self.render = render
        self.on_written = on_written
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

    def enqueue_from_refresh(self, session_id: str, value_markers: Iterable[Mapping[str, Any]],
                             *, from_ts: float, to_ts: float) -> int:
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
                if not anchor_in_window(unit, from_ts, to_ts):
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
        rel = normalize_path(unit.get("path"))
        existing = self.root / Path(rel)
        if not existing.is_file():
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
                result = run_value_markers(
                    [item], language=self.language, budget=self.budget,
                    root=self.root, render=self.render)[0]
                with self._lock:
                    status = str(result.get("status") or "failed")
                    self._counts[status] = self._counts.get(status, 0) + 1
                if status in {"created", "updated"} and self.on_written is not None:
                    try:
                        self.on_written(str(result.get("path") or ""))
                    except Exception:
                        logger.warning("minir knowledge: edge hook failed", exc_info=True)
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
