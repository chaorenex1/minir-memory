"""Deterministic claim selection for the adviser (P0-P3).

A claim is one assertion the memory already holds: a card line, a SOUL declaration, or a
knowledge page line. The selector decides which 0-3 claims bear on the current step; the
reviewer judges them; the ceiling maps their authority to a maximum severity. Nothing here
calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from . import card_review, knowledge

AUTHORITY_RANK = {"operator": 3, "card": 2, "knowledge": 1, "inference": 0}
SEVERITY_ORDER = {"nit": 0, "concern": 1, "blocker": 2}

#: Predicates are deterministic observations about the current step, never model judgement.
PREDICATES: tuple[str, ...] = (
    "draft_write", "shell_text", "unverified_final", "commit_unasked", "secret_read")

_WRITE_TOOLS = frozenset({"write_file", "patch", "create_file", "apply_patch"})
_READ_TOOLS = frozenset({"read_file", "search_files", "terminal"})
_SHELL_TEXT = re.compile(r"\b(cat|head|tail|grep|find|ls)\b")
_SECRET_PATH = re.compile(r"(\.env|id_rsa|secret|token|credential|\.pem)", re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    scope: str
    authority: str
    status: str = "active"
    test: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "scope": self.scope,
                "authority": self.authority, "status": self.status, "test": self.test}


def _lines(body: str) -> Iterable[tuple[int, str]]:
    for index, raw in enumerate(str(body or "").splitlines(), 1):
        text = raw.strip()
        if not text:
            continue
        if text.startswith(("- ", "* ")):
            text = text[2:].strip()
        if len(text) >= 3:
            yield index, text


def _claim_scope(heading: str, part: str) -> str:
    low = heading.casefold()
    if "tools" in low or "boundaries" in low or "critical" in low:
        return "tools"
    if "priorit" in low or "deliver" in low or "workflow" in low:
        return "delivery"
    if "preference" in low:
        return "delivery"
    return "domain"


def _infer_test(text: str) -> str:
    """Map a declarative line to the predicate it would violate; empty when it is not testable."""
    low = text.casefold()
    if ("草稿" in text or "draft" in low) and ("代码" in text or "code" in low):
        return "draft_write"
    if any(word in low for word in ("提交", "commit", "push")) and any(
            word in low for word in ("不", "未", "不要", "never", "without", "not ")):
        return "commit_unasked"
    if any(word in low for word in ("未验证", "not verified", "without evidence")) or \
            ("已验证" in text and "才" in text):
        return "unverified_final"
    if any(word in low for word in ("cat", "head", "tail", "grep", "find")):
        return "shell_text"
    if any(word in low for word in ("secret", "密钥", "token", ".env", "credential")):
        return "secret_read"
    return ""


def _relative(path: Path, root: Path) -> str:
    """A file's POSIX path under its root; the fallback keeps the id unique when it escapes."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def unique(claims: Iterable[Claim]) -> list[Claim]:
    """Assert the pool's ids are unique: a duplicate would silently shadow one claim."""
    seen: set[str] = set()
    out: list[Claim] = []
    for claim in claims:
        if claim.id in seen:
            raise ValueError(f"duplicate claim id {claim.id!r}")
        seen.add(claim.id)
        out.append(claim)
    return out


#: A missing card falls back to the base row that carries the same text (A §P2).
_CARD_BASE_COLUMNS = {
    "user_self_representation": "user_representation",
    "user_preferences": "user_peer_card",
    "ai_identity": "ai_identity_card",
    "ai_preference": "ai_self_representation",
}


def card_claims(hermes_home: str, profile: str,
                base: Optional[Mapping[str, Any]] = None) -> list[Claim]:
    """Every non-empty line of the four global cards, with the authority its meta declares.

    A card file that is missing or empty falls back to the base row that carries the same text,
    with ``#base:<line>`` ids so the two sources can never collide.
    """
    if not str(hermes_home or "").strip():
        return []
    card_root = Path(str(hermes_home)) / card_review.CARD_DIRNAME
    paths = card_review.card_paths(hermes_home, profile)
    claims: list[Claim] = []
    for part, path in paths.items():
        meta, body = card_review.read_card(path)
        fallback = False
        if not str(body or "").strip() and base:
            column = _CARD_BASE_COLUMNS.get(part)
            body = str((base.get(column) if column else "") or "")
            fallback = bool(str(body).strip())
        if not str(body or "").strip():
            continue
        rel = _relative(path, card_root)
        authority = "operator" if str(meta.get("source") or "") == "operator_edit" else "card"
        heading = ""
        for line_no, text in _lines(body):
            if text.startswith("#"):
                if text.startswith("##"):
                    heading = text.lstrip("#").strip()  # ### narrows the section too
                continue
            claims.append(Claim(
                id=(f"card:{rel}#base:{line_no}" if fallback else f"card:{rel}#{line_no}"),
                text=text,
                scope=_claim_scope(heading, part), authority=authority,
                test=_infer_test(text)))
    return claims


def soul_claims(hermes_home: str) -> list[Claim]:
    """The SOUL's constraint sections, one claim per declared line."""
    home = str(hermes_home or "").strip()
    if not home:
        return []
    path = Path(home) / "SOUL.md"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return []
    wanted = {"# boundaries", "# priorities", "# delivery contract", "# critical"}
    claims: list[Claim] = []
    active = False
    heading = ""
    for line_no, raw in enumerate(lines, 1):
        text = raw.strip()
        if text.startswith("# "):
            heading = text.casefold()
            active = heading in wanted
            continue
        if not active or not text or text.startswith("#"):
            continue
        if text.startswith(("- ", "* ")):
            text = text[2:].strip()
        if len(text) >= 8:
            claims.append(Claim(
                id=f"soul:SOUL.md#{line_no}", text=text,
                scope=_claim_scope(heading, "soul"), authority="operator",
                test=_infer_test(text)))
    return claims


def knowledge_claims(query: str, root: Any, limit: int = 3) -> list[Claim]:
    """Page lines that mention the query; a missing ripgrep means no claims, not an error."""
    if not str(query or "").strip() or not str(root or "").strip():
        return []
    try:
        found = knowledge.search_pages(
            query, root, limit=max(1, min(3, int(limit))), per_page=3)
    except Exception:
        return []
    if not found:
        return []
    claims: list[Claim] = []
    for row in found.get("results") or []:
        path = Path(root) / row["path"]
        try:
            _meta, body = knowledge._read_document(path)
        except Exception:
            continue
        _heading, content = knowledge._split_heading(body)
        retired = False
        for line_no, text in _lines(content):
            if text.startswith("#"):
                retired = any(word in text.casefold() for word in (
                    "retired", "deprecated", "已废弃", "已退役", "不再适用"))
                continue
            if retired:
                continue
            claims.append(Claim(
                id=f"knowledge:{row['path']}#{line_no}", text=text,
                scope="domain", authority="knowledge"))
            if len(claims) >= limit:
                return claims
    return claims


def _turn_text(turn: dict[str, Any]) -> str:
    return " ".join(str(turn.get(key) or "") for key in ("text", "tool", "args", "path"))


def matches(predicate: str, turn: dict[str, Any]) -> bool:
    """True when the current step is the situation the predicate names."""
    tool = str(turn.get("tool") or "")
    args = str(turn.get("args") or "")
    text = _turn_text(turn)
    low = text.casefold()
    if predicate == "draft_write":
        return tool in _WRITE_TOOLS and ("草稿" in text or "draft" in low)
    if predicate == "shell_text":
        return tool == "terminal" and bool(_SHELL_TEXT.search(args))
    if predicate == "unverified_final":
        done = any(word in low for word in ("done", "完成", "passed", "通过", "verified"))
        proof = any(word in low for word in ("exit=", "pytest", "test", "验证", "工具输出"))
        return tool in {"final_answer", "agent-end", "final"} and done and not proof
    if predicate == "commit_unasked":
        return tool == "terminal" and any(word in args for word in ("git commit", "git push"))
    if predicate == "secret_read":
        return tool in _READ_TOOLS and bool(_SECRET_PATH.search(args))
    return False


def select(turn: dict[str, Any], claims: Iterable[Claim], limit: int = 3
           ) -> list[dict[str, Any]]:
    """Rank claims deterministically; ``[]`` means the reviewer does not run the brain pass."""
    words = {term for term in re.findall(r"[\w-]{2,}", _turn_text(turn).casefold())}
    scored: list[tuple[float, Claim, str]] = []
    for claim in claims:
        if claim.status != "active":
            continue
        why: list[str] = []
        score = 0.0
        if claim.test and matches(claim.test, turn):
            score += 10.0
            why.append(f"predicate {claim.test}")
        if claim.scope and claim.scope.casefold() in _turn_text(turn).casefold():
            score += 3.0
            why.append(f"scope {claim.scope}")
        overlap = len(words & {term for term in re.findall(
            r"[\w-]{2,}", claim.text.casefold())})
        if overlap:
            score += min(2.0, float(overlap))
            why.append(f"{overlap} shared term(s)")
        if score > 0:
            scored.append((score, claim, ", ".join(why)))
    scored.sort(key=lambda item: (-item[0], AUTHORITY_RANK.get(item[1].authority, 0),
                                  item[1].id))
    return [{**claim.as_dict(), "why_now": why}
            for _score, claim, why in scored[:max(1, int(limit))]]


def apply_ceiling(notes: list[dict[str, Any]], claims: Iterable[dict[str, Any]]
                  ) -> list[dict[str, Any]]:
    """Cap each note at the authority of the claim it cites; never raise a severity."""
    by_id: dict[str, dict[str, Any]] = {}
    for claim in claims or ():
        cid = str(claim.get("id") or "")
        if cid in by_id:
            raise ValueError(f"duplicate claim id {cid!r}")
        by_id[cid] = claim
    ceiling = {"operator": "blocker", "card": "concern", "knowledge": "concern",
               "inference": "nit"}
    for note in notes:
        claim = by_id.get(str(note.get("claim") or ""))
        authority = str(claim.get("authority") or "") if claim else ""
        cap = ceiling.get(authority)
        if not cap and str(note.get("target") or "") == "user_preferences_check":
            cap = "concern"  # an unclaimed preference is legacy: never a blocker
        if cap and SEVERITY_ORDER.get(str(note.get("severity")), 0) > SEVERITY_ORDER[cap]:
            note["severity"] = cap
        if claim:
            note["claim_text"] = claim.get("text") or ""
    return notes


def parse_coverage(answer: str, active_ids: Iterable[str]) -> dict[str, str]:
    """Read the reviewer's per-claim coverage map; unknown ids and values are dropped."""
    import json

    try:
        payload = json.loads(str(answer or "").strip())
    except (TypeError, ValueError):
        return {}
    raw = payload.get("claim_coverage") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    wanted = set(active_ids)
    return {str(key): str(value) for key, value in raw.items()
            if str(key) in wanted and str(value) in {"clean", "found", "n/a"}}
