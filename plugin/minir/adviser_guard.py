"""Emission guard for adviser-mode notes.

Port of omp's ``AdvisorEmissionGuard`` (``advisor/emission-guard.ts``) with the same
job: reject the noise a reviewer produces when it has nothing to say, collapse rephrasings of a
note already delivered, keep an escalation (same note, higher severity), cap how many notes one
update may publish, and remember what it let through.

The guard is deliberately lexical, not semantic. The reviewer prompt does the judgement; this
module only enforces the contract:

* ``key_for`` normalizes a note to an NFKC, lowercase, punctuation-free key.
* ``is_noise`` drops acknowledgements ("looks good", "no issues", ...).
* ``EmissionGuard.admit`` returns the accepted notes plus per-reason drop counts, which the
  review row stores so a silent reviewer can be told apart from a noisy one.
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

SEVERITY_RANK: dict[str, int] = {"nit": 0, "concern": 1, "blocker": 2}
DEFAULT_BUDGET = 4
MAX_BUDGET = 32
CACHE_CAPACITY = 4096

# Normalized keys, not raw phrases: ``key_for`` lowercases and strips punctuation, so
# "Looks good." and "looks  GOOD!" both reduce to the entry below.
NOISE_KEYS: frozenset[str] = frozenset({
    "ok", "okay", "fine", "good", "great", "nice", "correct", "approved", "agreed",
    "lgtm", "continue", "proceed", "carry on", "keep going", "all good", "looks good",
    "sounds good", "that works", "well done", "no issues", "no problem", "no problems",
    "nothing to add", "nothing else", "nothing more", "no advice", "no concerns",
    "no changes needed", "i agree", "none", "n a",
})

_WORD = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def key_for(text: Any) -> str:
    """Normalize a note to the dedupe key both the guard and the ledger use."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    flat = unicodedata.normalize("NFKC", text).lower()
    return " ".join(_WORD.sub(" ", flat).split())


def is_noise(text: Any) -> bool:
    """True when the note carries no instruction (an acknowledgement, not advice)."""
    key = key_for(text)
    if not key:
        return True
    return key in NOISE_KEYS or key.rstrip(".!") in NOISE_KEYS


def severity_rank(severity: Any) -> int:
    return SEVERITY_RANK.get(str(severity or "").strip().lower(), SEVERITY_RANK["concern"])


@dataclass
class GuardVerdict:
    """What one update may publish, and why the rest was dropped."""

    accepted: list[dict[str, Any]] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    detail: list[dict[str, Any]] = field(default_factory=list)

    def count(self, reason: str) -> int:
        return int(self.dropped.get(reason, 0))


class EmissionGuard:
    """Per-session dedupe cache shared by every adviser in the roster.

    The cache is a bounded FIFO of ``key -> severity``. It is an in-memory fast path only:
    the ledger's unique ``(session_id, fingerprint)`` index is the durable record, and
    ``admit(..., seen=...)`` takes the ledger's view so a guard rebuilt after a restart still
    refuses a duplicate.
    """

    def __init__(self, capacity: int = CACHE_CAPACITY) -> None:
        self._capacity = max(1, min(int(capacity or CACHE_CAPACITY), CACHE_CAPACITY))
        self._cache: "OrderedDict[str, str]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._cache)

    def seen(self, key: str) -> Optional[str]:
        return self._cache.get(key)

    def remember(self, key: str, severity: str) -> None:
        if not key:
            return
        if key in self._cache:
            # Keep the strongest severity ever seen for this key.
            if severity_rank(severity) > severity_rank(self._cache[key]):
                self._cache[key] = str(severity)
            self._cache.move_to_end(key)
            return
        self._cache[key] = str(severity)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    def admit(self, notes: Iterable[dict[str, Any]], *, budget: int = DEFAULT_BUDGET,
              seen: Optional[Mapping[str, str]] = None) -> GuardVerdict:
        """Apply the emission rules to one update's notes.

        ``notes`` are the reviewer's parsed rows (``severity``/``content``). Blockers are
        admitted first and do not spend the non-blocker budget; a blocker can therefore never
        be crowded out by a chatty reviewer. ``seen`` maps keys already present in the ledger
        to their severity, so an escalation still passes while a repeat is dropped.
        """
        try:
            limit = max(1, min(int(budget or DEFAULT_BUDGET), MAX_BUDGET))
        except (TypeError, ValueError):
            limit = DEFAULT_BUDGET
        prior: dict[str, str] = dict(seen or {})
        candidates: list[tuple[int, dict[str, Any]]] = []
        for index, note in enumerate(notes or []):
            if isinstance(note, dict):
                candidates.append((index, note))
        # Stable, strongest-first: the accepted order is the delivered order.
        candidates.sort(key=lambda pair: (-severity_rank(pair[1].get("severity")), pair[0]))

        verdict = GuardVerdict()
        non_blockers = 0
        for index, note in candidates:
            content = str(note.get("content") or note.get("text") or "")
            key = str(note.get("fingerprint") or key_for(content))
            severity = str(note.get("severity") or "concern").strip().lower()
            if severity not in SEVERITY_RANK:
                severity = "concern"
            if not key:
                self._drop(verdict, index, "empty", key)
                continue
            if is_noise(content):
                self._drop(verdict, index, "noise", key)
                continue
            previous = prior.get(key) or self.seen(key)
            if previous is not None and severity_rank(severity) <= severity_rank(previous):
                self._drop(verdict, index, "duplicate", key)
                continue
            if severity != "blocker" and non_blockers >= limit:
                self._drop(verdict, index, "rate-limit", key)
                continue
            accepted = dict(note)
            accepted["content"] = content
            accepted["severity"] = severity
            accepted["fingerprint"] = key
            verdict.accepted.append(accepted)
            prior[key] = severity
            self.remember(key, severity)
            if severity != "blocker":
                non_blockers += 1
        return verdict

    @staticmethod
    def _drop(verdict: GuardVerdict, index: int, reason: str, key: str) -> None:
        verdict.dropped[reason] = verdict.count(reason) + 1
        verdict.detail.append({"index": index, "reason": reason, "key": key[:80]})
