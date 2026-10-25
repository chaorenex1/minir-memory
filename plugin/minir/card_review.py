"""The three-day card review: the only writer of the global user/AI card files.

The base layer keeps a session snapshot of the cards; this layer merges what several sessions
proved into four profile-global files, and records every write as a revision. The file is the
current state, the database is its history. The merge runs as four single-file passes: the global
user identity, user preference, AI identity and AI preference files each have their own rule and
output contract, and code joins the four results.

Two rules shape the module:

* **The review is the only writer.** The model reads the files, it never writes them: a pass
  that could rewrite ``identity.md`` and then cite it would make its own evidence circular.
  Writes go through :func:`write_cards` on the scheduler's code path.
* **Validation is all-or-nothing.** Any file that is missing a key, carries a session-scoped
  field or exceeds its token budget fails the whole review; the previous four files stay exactly
  as they were.

The cadence is wall-clock, not a turn counter: a review is due every three days (even with
no live session), the anchor advances only on success, and a failure retries after 30 minutes,
2 hours and 6 hours before waiting for the next period. This module owns only the anchor and
the retry policy; the plugin's unified :mod:`minir.scheduler` polls ``due()``/``tick()``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import layers
from . import rules as layer_rules

logger = logging.getLogger(__name__)

CARD_SCHEMA = "minir-card/v1"
CARD_DIRNAME = "minir-memory"
PERIOD_SECONDS = 3 * 24 * 3600
RETRY_SECONDS: tuple[int, ...] = (1800, 7200, 21600)
TICK_SECONDS = 600.0
_MAX_EVIDENCE_CHARS = 120_000
_MAX_SESSIONS = 20
#: Card review reads the original text of the sessions it calibrates, but the card bodies above
#: are the baseline: three sessions, 4000 characters total.
_CARD_TRANSCRIPT_SESSIONS = 3
_CARD_TRANSCRIPT_CHARS = 4_000

# Every card file is front-matter plus one markdown body. Nothing here is field-structured: the
# model writes the body, the code frames it, and an empty body is a valid empty card.
USER_SELF = "user_self_representation"
USER_PREFERENCE = "user_preferences"
AI_IDENTITY = "ai_identity"
AI_PREFERENCE = "ai_preference"
PARTS: tuple[str, ...] = (USER_SELF, USER_PREFERENCE, AI_IDENTITY, AI_PREFERENCE)

PART_SCOPE: dict[str, str] = {
    USER_SELF: "user", USER_PREFERENCE: "user", AI_IDENTITY: "ai", AI_PREFERENCE: "ai",
}
PART_NAME: dict[str, str] = {
    USER_SELF: "identity", USER_PREFERENCE: "preference",
    AI_IDENTITY: "identity", AI_PREFERENCE: "preference",
}
PART_CARD: dict[str, str] = {
    USER_SELF: "user-card", USER_PREFERENCE: "user-card",
    AI_IDENTITY: "ai-card", AI_PREFERENCE: "ai-card",
}

# One agent per file: each pass writes exactly one global card body, reads its own rule file, and
# returns one key. Code joins the four passes before the all-or-nothing write.
CARD_SCOPE_PARTS: dict[str, tuple[str, ...]] = {
    "user_self_representation": (USER_SELF,),
    "user_preferences": (USER_PREFERENCE,),
    "ai_identity": (AI_IDENTITY,),
    "ai_preference": (AI_PREFERENCE,),
}
CARD_SCOPE_RULES: dict[str, tuple[str, ...]] = {
    "user_self_representation": ("user-self",),
    "user_preferences": ("user-preferences",),
    "ai_identity": ("ai-identity",),
    "ai_preference": ("ai-preferences",),
}
CARD_SCOPE_CORE: dict[str, str] = {
    "user_self_representation": "card_review_user",
    "user_preferences": "card_review_user",
    "ai_identity": "card_review_ai",
    "ai_preference": "card_review_ai",
}
CARD_SCOPE_ORDER: tuple[str, ...] = tuple(CARD_SCOPE_PARTS)

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


class CardReviewError(RuntimeError):
    """The review produced files that may not be published."""


def _safe_profile(profile: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(profile or "default")).strip("._")
    return safe or "default"


def card_paths(hermes_home: str, profile: str) -> dict[str, Path]:
    """Absolute path of each card file for this profile."""
    home = Path(str(hermes_home or ""))
    base = home / CARD_DIRNAME
    agent = base / "agents" / _safe_profile(profile)
    return {
        USER_SELF: base / "user" / "identity.md",
        USER_PREFERENCE: base / "user" / "preference.md",
        AI_IDENTITY: agent / "identity.md",
        AI_PREFERENCE: agent / "preference.md",
    }


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER_RE.match(text or "")
    if match is None:
        return {}, text or ""
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [item.strip().strip("\"'") for item in value[1:-1].split(",")
                                 if item.strip()]
        else:
            meta[key.strip()] = value.strip("\"'")
    return meta, (text or "")[match.end():]


def read_card(path: Path) -> tuple[dict[str, Any], str]:
    """A card file -> (front-matter, markdown body). Missing or legacy JSON body -> empty text."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}, ""
    meta, body = _parse_front_matter(text)
    if body.lstrip().startswith("```"):
        return meta, ""  # a card written by the field-era renderer: no markdown body to reuse
    return meta, body.strip()


def render_card(part: str, text: str, meta: dict[str, Any]) -> str:
    """Front-matter plus the model's markdown body, unchanged."""
    header = {
        "schema": CARD_SCHEMA, "scope": PART_SCOPE[part], "part": PART_NAME[part],
        "generated_at": meta.get("generated_at") or _now_text(),
        "source": meta.get("source") or "review",
        "evidence": meta.get("evidence") or [],
        "reviewer": meta.get("reviewer") or "",
    }
    lines = ["---"]
    for key, value in header.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(item) for item in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    body = str(text or "").strip()
    return "\n".join(lines) + (f"\n\n{body}\n" if body else "\n")


def _now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _tokens(text: str) -> int:
    return layer_rules.estimate_tokens(text)


def validate_cards(payload: Any, *, budget: int = 2000
                   ) -> dict[str, dict[str, Any]]:
    """Validate the review object and every rendered file; returns part -> values."""
    if not isinstance(payload, dict):
        raise CardReviewError("the review must be one JSON object")
    missing = [part for part in PARTS if part not in payload]
    extra = [key for key in payload if key not in PARTS]
    if missing:
        raise CardReviewError(f"missing card(s): {', '.join(missing)}")
    if extra:
        raise CardReviewError(f"unexpected card(s): {', '.join(extra)}")
    cleaned: dict[str, str] = {}
    for part in PARTS:
        try:
            cleaned[part] = layers.validate_card_body(payload[part], part)
        except layers.LayerContractError as exc:
            raise CardReviewError(str(exc)) from exc
        if _tokens(cleaned[part]) > budget:
            raise CardReviewError(
                f"{part} is {_tokens(cleaned[part])} tokens; the per-file budget is {budget}")
    return cleaned


def load_global_cards(hermes_home: str, profile: str) -> dict[str, Any]:
    """The durable baseline the base layer prefers: four files merged into three parts."""
    if not str(hermes_home or "").strip():
        return {}
    paths = card_paths(hermes_home, profile)
    user_self_representation = read_card(paths[USER_SELF])[1]
    user_preference = read_card(paths[USER_PREFERENCE])[1]
    ai_identity = read_card(paths[AI_IDENTITY])[1]
    ai_preference = read_card(paths[AI_PREFERENCE])[1]
    cards: dict[str, Any] = {}
    if user_self_representation:
        cards["user_self_representation"] = user_self_representation
    if user_preference:
        cards["user_preferences"] = user_preference
    identity = "\n\n".join(text for text in (ai_identity, ai_preference) if text)
    if identity:
        cards["ai_identity"] = identity
    return cards


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def write_cards(store: Any, hermes_home: str, profile: str, cards: dict[str, str],
                *, reviewer: str = "model", budget: int = 2000,
                source: str = "review", parts: Optional[tuple[str, ...]] = None,
                reason: str = "") -> list[int]:
    """Validate then publish the requested card files; any failure writes none of them.

    A single-part write overlays the new body on the current file and preserves the other
    three. The files are canonical; each write leaves a card_revisions row (before/after hash
    and the changed body) as the change history.
    """
    requested = tuple(parts or PARTS)
    unknown = [part for part in requested if part not in PARTS]
    if unknown:
        raise CardReviewError(f"unknown card part(s): {', '.join(unknown)}")
    paths = card_paths(hermes_home, profile)
    merged = {part: read_card(paths[part])[1] for part in PARTS}
    merged.update({part: cards[part] for part in requested if part in cards})
    cleaned = validate_cards(merged, budget=budget)
    rendered = {part: render_card(part, cleaned[part], {
        "source": source, "reviewer": reviewer,
        "evidence": [reason] if reason else []}) for part in PARTS}
    for part in PARTS:  # render-time budget is re-checked: the caller may pass a tiny budget
        if _tokens(rendered[part]) > budget:
            raise CardReviewError(f"{part} exceeds the configured card budget")
    revisions: list[int] = []
    for part in requested:
        path = paths[part]
        old_text = ""
        if path.exists():
            try:
                old_text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                old_text = ""
        old_meta, _old_values = read_card(path)
        if old_text and not old_meta:
            # A file written by hand before this layer existed: keep it for audit, then take over.
            declared = path.with_name(f"{path.stem}.declared.md")
            if not declared.exists():
                _atomic_write(declared, old_text)
        previous = store.latest_card_revision(PART_SCOPE[part], PART_NAME[part])
        if old_text and previous is not None and _sha(old_text) != previous["after_hash"]:
            store.add_card_revision(
                PART_SCOPE[part], PART_NAME[part], str(path),
                before_hash=previous["after_hash"], after_hash=_sha(old_text),
                changed_fields=[], source="operator_edit",
                evidence=["operator edit detected before review"], reviewer=reviewer)
        _atomic_write(path, rendered[part])
        before_hash = _sha(old_text) if old_text else ""
        changed = _changed_body(old_text, cleaned[part])
        revisions.append(store.add_card_revision(
            PART_SCOPE[part], PART_NAME[part], str(path),
            before_hash=before_hash, after_hash=_sha(rendered[part]),
            changed_fields=changed, source=source,
            evidence=[reason] if reason else [], reviewer=reviewer))
    return revisions


def _changed_body(old_text: str, new_text: str) -> list[str]:
    """Provenance for a text card: one label, since a body has no fields to name."""
    old_body = ""
    if old_text:
        _meta, old_body = _parse_front_matter(old_text)
    return ["body"] if old_body.strip() != str(new_text or "").strip() else []


# -- the review pass -----------------------------------------------------------

OUTPUT_USER_IDENTITY = """OUTPUT
Return exactly one JSON object:
{"user_self_representation": "<markdown body>"}
The value is the final global identity card body: complete markdown, not an object, not a patch.
An empty string is a valid empty card. No audit arrays, no front-matter, no code fence, no extra
key. Code records the change history itself.
"""

OUTPUT_USER_PREFERENCE = """OUTPUT
Return exactly one JSON object:
{"user_preferences": "<markdown body>"}
The value is the final global preference card body: complete markdown, not an object, not a patch.
An empty string is a valid empty card. No audit arrays, no front-matter, no code fence, no extra
key. Code records the change history itself.
"""

OUTPUT_AI_IDENTITY = """OUTPUT
Return exactly one JSON object:
{"ai_identity": "<markdown body>"}
The value is the final global identity card body: complete markdown, not an object, not a patch.
An empty string is a valid empty card. No audit arrays, no front-matter, no code fence, no extra
key. Code records the change history itself.
"""

OUTPUT_AI_PREFERENCE = """OUTPUT
Return exactly one JSON object:
{"ai_preference": "<markdown body>"}
The value is the final global preference card body: complete markdown, not an object, not a patch.
An empty string is a valid empty card. No audit arrays, no front-matter, no code fence, no extra
key. Code records the change history itself.
"""

CARD_SCOPE_OUTPUT: dict[str, str] = {
    "user_self_representation": OUTPUT_USER_IDENTITY,
    "user_preferences": OUTPUT_USER_PREFERENCE,
    "ai_identity": OUTPUT_AI_IDENTITY,
    "ai_preference": OUTPUT_AI_PREFERENCE,
}


def _hand_edited(store: Any, paths: dict[str, Path],
                 parts: tuple[str, ...]) -> list[str]:
    """Files whose content no longer matches the last recorded revision: the operator edited them."""
    edited: list[str] = []
    for part in parts:
        path = paths[part]
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if not text.strip():
            continue
        previous = store.latest_card_revision(PART_SCOPE[part], PART_NAME[part])
        if previous is None or _sha(text) != str(previous.get("after_hash") or ""):
            edited.append(f"{PART_SCOPE[part]}/{PART_NAME[part]}: {path}")
    return edited


def build_prompt(store: Any, hermes_home: str, profile: str, since: float, *, scope: str,
                 plugin_yaml: str, language: str = "en", transcript: Any = None,
                 transcript_profile: str = "") -> tuple[str, str]:
    """One file's system prompt and user message: its card, the evidence, its markers."""
    parts_for_scope = CARD_SCOPE_PARTS[scope]
    side = PART_SCOPE[parts_for_scope[0]]
    paths = card_paths(hermes_home, profile)
    current = []
    for part in parts_for_scope:
        path = paths[part]
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            text = ""
        if text.strip():
            current.append(f"--- {path.name} ({PART_SCOPE[part]}/{PART_NAME[part]}) ---\n{text}")
    rows = store.base_refreshes_since(since, per_session=1, max_sessions=_MAX_SESSIONS)
    blocks: list[str] = []
    size = 0
    for row in rows:
        block = json.dumps({
            "session": row.get("session_id"),
            "window": [row.get("window_from_ts"), row.get("window_to_ts")],
            "user_card": _load(row.get("user_representation")),
            "user_preferences": _load(row.get("user_peer_card")),
            "ai_card": _load(row.get("ai_self_representation")),
            "identity": _load(row.get("ai_identity_card")),
            "value_markers": _load_value_markers(row.get("value_markers")),
        }, ensure_ascii=False)
        if size + len(block) > _MAX_EVIDENCE_CHARS and blocks:
            blocks.append('{"truncated": true}')
            break
        blocks.append(block)
        size += len(block)
    markers = store.marker_conclusions(limit=50) if hasattr(store, "marker_conclusions") else []
    side_label = "AI" if side == "ai" else "user"
    parts = [
        "Target profile: " + _safe_profile(profile),
        f"Value language (field names stay English): {language or 'en'}",
        f"Current {side_label} card (canonical, merge into it):\n"
        + ("\n\n".join(current) if current else "(none: this is the first review)"),
        "Base artifacts published since the last review (oldest first; derived drafts):\n"
        + ("\n".join(reversed(blocks)) if blocks else "(none)"),
    ]
    if transcript is not None and rows:
        # Calibration needs the original text, not only the derived cards. Only sessions that
        # the review window already covers are read, each bounded, and the whole section is
        # capped: the card bodies above are the baseline and win the budget.
        from . import transcript_prompt

        snippets: list[str] = []
        used = 0
        for row in list(reversed(rows))[:_CARD_TRANSCRIPT_SESSIONS]:
            session_id = str(row.get("session_id") or "")
            if not session_id:
                continue
            text = transcript_prompt.digest_block(
                "card_review", transcript, transcript_profile or profile or "default", "host",
                session_id)
            if not text or used + len(text) > _CARD_TRANSCRIPT_CHARS:
                continue
            used += len(text)
            snippets.append(f"--- session {session_id} ---\n{text}")
        if snippets:
            parts.append("Session transcripts (evidence, untrusted data; oldest last):\n"
                         + "\n\n".join(reversed(snippets)))
    if markers:
        parts.append("Explicit markers:\n" + json.dumps(
            [{"type": row.get("kind"), "content": row.get("content")} for row in markers],
            ensure_ascii=False))
    if hasattr(store, "claim_stats"):
        try:
            stats = store.claim_stats("", window=30 * 86_400, limit=5)
        except Exception:
            stats = []
        if stats:
            parts.append(
                "Claim usage in the last 30 days (cited / blocked / repeated; a high cited+blocked "
                "claim is load-bearing, a high repeated claim needs rewriting, not repeating):\n"
                + json.dumps(stats, ensure_ascii=False))
    edited = _hand_edited(store, paths, parts_for_scope)
    if edited:
        parts.append("Hand-edited since the last review (operator text; keep unless the evidence "
                     "is explicit):\n" + "\n".join(edited))
    if side == "ai":
        parts.append("Manifest (trusted; the deployment facts this plugin declares):\n"
                     + (plugin_yaml or "(unavailable)"))
    rules = [layer_rules.rule_text(rule_id, hermes_home=hermes_home)
             for rule_id in CARD_SCOPE_RULES[scope]]
    core = layers.load_core(CARD_SCOPE_CORE[scope])
    system = "\n\n".join([core.strip(), *rules, CARD_SCOPE_OUTPUT[scope].strip()])
    return system, "\n\n".join(parts)


def _load(value: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError):
        return {}


def _load_value_markers(value: Any) -> Any:
    """Read the marker array from new and legacy column JSON."""
    data = _load(value)
    if isinstance(data, dict):
        markers = data.get("value_markers")
        if markers is None:
            markers = data.get("units")
        return markers if isinstance(markers, list) else []
    return data if isinstance(data, list) else []


def run_review(store: Any, hermes_home: str, profile: str, *, complete: Callable[[str, str], str],
               since: float, plugin_yaml: str = "", language: str = "en",
               reviewer: str = "model", transcript: Any = None,
               transcript_profile: str = "") -> list[int]:
    """Four single-file passes, one validation boundary, one write.

    Each pass writes one global card body and reads one rule file; the database's card_revisions
    rows are the change history. The four files are published together: a malformed pass cannot
    leave the other three rewritten.
    """
    cards: dict[str, Any] = {}
    for scope in CARD_SCOPE_ORDER:
        expected = CARD_SCOPE_PARTS[scope]
        system, body = build_prompt(store, hermes_home, profile, since, scope=scope,
                                    plugin_yaml=plugin_yaml, language=language,
                                    transcript=transcript,
                                    transcript_profile=transcript_profile)
        payload = _json_object(complete(system, body))
        if not payload:
            raise CardReviewError(
                f"the {scope} card reviewer did not return a JSON object (missing {expected[0]})")
        extra = sorted(set(payload) - set(expected))
        if extra:
            raise CardReviewError(
                f"the {scope} card reviewer returned unexpected key(s): {', '.join(extra)}")
        missing = [part for part in expected if part not in payload]
        if missing:
            raise CardReviewError(
                f"the {scope} card reviewer is missing {', '.join(missing)}")
        cards.update({part: payload[part] for part in expected})
    return write_cards(store, hermes_home, profile, cards, reviewer=reviewer)


def _json_object(text: str) -> dict[str, Any]:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", candidate).strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


# -- scheduling ----------------------------------------------------------------

class CardReviewScheduler:
    """Review anchor: due every period, retry on failure, advance only on success."""

    def __init__(self, store: Any, run: Callable[[float], list[int]], *,
                 subject: str, period: float = PERIOD_SECONDS,
                 retries: tuple[int, ...] = RETRY_SECONDS,
                 files_present: Optional[Callable[[], bool]] = None):
        self._store = store
        self._run = run
        self._subject = subject
        self._period = max(1.0, float(period))
        self._retries = retries
        self._files_present = files_present

    def _state(self) -> dict[str, Any]:
        row = self._store.layer_state("card_review", self._subject)
        try:
            return json.loads(str((row or {}).get("content") or "")) or {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self._store.put_layer_state("card_review", self._subject,
                                    json.dumps(state, ensure_ascii=False))

    def ensure_anchor(self, now: Optional[float] = None) -> float:
        """Start the clock without claiming a review ran; a missing card still runs immediately."""
        now = time.time() if now is None else float(now)
        state = self._state()
        if "started_at" not in state and "last_success_at" not in state:
            state = {"started_at": now, "attempts": 0, "next_attempt_at": now + self._period}
            self._save(state)
        return float(state.get("started_at") or state.get("last_success_at") or now)

    def due(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else float(now)
        if self._files_present is not None:
            try:
                if not self._files_present():
                    return True
            except Exception as exc:
                logger.debug("Minir card presence check failed: %s", exc)
        state = self._state()
        anchor = state.get("last_success_at") or state.get("started_at")
        if anchor is None:
            return True
        return now + 1e-6 >= float(state.get("next_attempt_at") or
                            float(anchor) + self._period)

    def tick(self, now: Optional[float] = None) -> bool:
        """Run one due review synchronously; returns True when it succeeded."""
        now = time.time() if now is None else float(now)
        if not self.due(now):
            return False
        state = self._state()
        since = float(state.get("last_success_at") or state.get("started_at") or now - self._period)
        try:
            self._run(since)
        except Exception as exc:
            logger.warning("Minir card review failed: %s", exc)
            attempts = int(state.get("attempts") or 0) + 1
            if attempts <= len(self._retries):
                next_at = now + self._retries[attempts - 1]
            else:
                next_at = now + self._period
            self._save({**state, "attempts": attempts, "next_attempt_at": next_at,
                        "last_error": str(exc)[:400]})
            return False
        self._save({"last_success_at": now, "attempts": 0,
                    "next_attempt_at": now + self._period})
        return True
