"""One entry point for every fork: deterministic on the hot path, the adviser off it.

A fork is one tool-level branch with an action the main model does not have to decide. Three
paths exist because the host gives each of them a different budget:

* ``resolve_now`` — memory only. It runs inside ``pre_tool_call`` and
  ``transform_tool_result``, and ``pre_tool_call`` is fail-closed: an exception or a timeout
  there refuses the tool. No model call, no disk write, no lock held across I/O.
* ``resolve_gate`` — a synchronous adviser consult for one integration command. Only the
  ``pre_tool_call`` integration branch calls it, and only because the host patch lets that
  callback declare a longer budget. It stays fail-closed: no answer means block.
* ``judge`` — the slow path for a fork that has no rule. Worker threads only.

The rule text lives in ``<hermes_home>/minir-memory/forks/{static,learned}.json`` because a rule
is content: readable, diffable, hand-editable. SQLite holds the counters and the ledger, never
the rule.

Blocking is never learned. The only two blocks this module returns are the retry guard (a fixed
predicate: the same call failing past a limit) and the operator-authorized integration gate. A
rule whose ``decision`` is ``block`` is dropped at load time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from . import fork_rules

logger = logging.getLogger(__name__)

#: Characters of the live turn handed to a gate or a pull consult. The review delta is not
#: enough here: a merge decision needs the user message and the plan, not just the last tool.
GATE_DIGEST_CHARS = 8000
#: Upper bound on one candidate's arguments in the reviewer prompt (a bounded summary, not the
#: tool call: the reviewer reads the delta for evidence).
CANDIDATE_ARGS_CHARS = 400
MAX_FAILURE_KEYS = 512
PROMOTE_HITS = 2
DEMOTE_OVERTURNS = 1
DROP_OVERTURNS = 2

_GO_LINE = re.compile(r"^(go|no[\s-]?go|no|concern)\b", re.IGNORECASE)
_DECISION_LINE = re.compile(r"\bdecision\s*:\s*(inline|dispatch|split|rewrite|compress)\b",
                            re.IGNORECASE)
_TARGET_LINE = re.compile(r"\btarget\s*:\s*([a-z_]+)\b", re.IGNORECASE)
_WORD = re.compile(r"[\w./-]{3,}")


@dataclass(frozen=True)
class Verdict:
    """One decision about one fork. ``key`` is exact-match; ``rule_id`` names a stored rule."""

    key: str
    decision: str
    source: str
    strength: str = "none"
    rule_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class ForkDispatcher:
    """Rules, counters and the three resolve paths. Constructed once per provider."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self._lock = threading.Lock()
        self._rules: dict[str, list[dict[str, Any]]] = {}
        self._failures: dict[str, tuple[int, str]] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._scope = ""
        self._loaded = False

    # -- configuration -----------------------------------------------------

    def _config(self, key: str, default: Any = None) -> Any:
        raw = getattr(self._provider, "_config", None) or {}
        block = raw.get("adviser_mode") if isinstance(raw, dict) else None
        return (block or {}).get(key, default) if isinstance(block, dict) else default

    def mode(self) -> str:
        """``off`` | ``shadow`` | ``on``; an unknown value is ``off``, never a surprise action."""
        value = str(self._config("forks", "off") or "off").strip().lower()
        return value if value in {"off", "shadow", "on"} else "off"

    def active(self) -> bool:
        """Shadow and on both observe; only ``on`` applies."""
        return self.mode() != "off"

    def enabled(self) -> bool:
        return self.mode() == "on"

    def gate_mode(self) -> str:
        value = str(self._config("gates", "off") or "off").strip().lower()
        return value if value in {"off", "shadow", "merge"} else "off"

    # -- state -------------------------------------------------------------

    def _home(self) -> str:
        return str(getattr(self._provider, "_hermes_home", "") or "")

    def _dir(self) -> Path:
        return Path(self._home()) / "minir-memory" / "forks"

    def _static_path(self) -> Path:
        return self._dir() / "static.json"

    def _learned_path(self) -> Path:
        return self._dir() / "learned.json"

    def default_scope(self) -> str:
        """Project root name: a rule outlives the session it was learned in."""
        root = ""
        try:
            from . import adviser

            root = str(adviser.project_dir() or "")
        except Exception:  # a host without the adviser module keeps the cwd's name
            root = ""
        return Path(root or os.getcwd()).name or "default"

    def scope(self) -> str:
        return self._scope or self.default_scope()

    def load(self, scope: str = "") -> None:
        """Cache ``static.json`` + ``learned.json``; the files stay the authority.

        Static rules load first, so an operator rule wins over a learned one with the same key.
        A malformed entry is skipped and named in the log: refusing the whole file would let
        one hand-edited line disable every rule beside it.
        """
        self._scope = str(scope or self._scope or "").strip() or self.default_scope()
        if not self._home():
            return
        rules: dict[str, list[dict[str, Any]]] = {kind: [] for kind in fork_rules.KINDS}
        for path in (self._static_path(), self._learned_path()):
            for entry in self._read_entries(path):
                rule = fork_rules.normalize_rule(entry)
                if rule is None:
                    logger.warning("jev: skipped a malformed rule in %s", path.name)
                    continue
                rules.setdefault(str(rule["kind"]), []).append(rule)
        with self._lock:
            self._rules = rules
            self._loaded = True

    def _read_entries(self, path: Path) -> list[Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("jev: unreadable rule file %s: %s", path, exc)
            return []
        try:
            data = json.loads(text)
        except ValueError as exc:
            logger.warning("jev: %s is not JSON and was ignored: %s", path.name, exc)
            return []
        entries = data.get("rules") if isinstance(data, dict) else data
        return entries if isinstance(entries, list) else []

    def _active(self, kind: str, *, scope: str = "") -> list[dict[str, Any]]:
        with self._lock:
            rules = list(self._rules.get(kind) or ())
        current = scope or self.scope()
        return [rule for rule in rules if fork_rules.scope_ok(rule, current)]

    def rules(self, kind: str = "") -> list[dict[str, Any]]:
        """Loaded rules (optionally one kind), for inspection and tests."""
        kinds = (kind,) if kind else fork_rules.KINDS
        return [rule for item in kinds for rule in self._active(item)]

    def _covered(self, kind: str, key: str) -> bool:
        """A fork an active (medium or better) rule already answers is not re-asked."""
        return any(rule.get("key") == key and fork_rules.ladder(rule) >= fork_rules.STRENGTH["medium"]
                   for rule in self._active(kind))

    # -- hot path ----------------------------------------------------------

    def resolve_now(self, kind: str, tool: str = "", *, args: Any = None,
                    hook: str = "pre_tool", session: str = "") -> Optional[Verdict]:
        """Memory only. ``None`` means split: the call is left exactly as the model wrote it."""
        if not self.active() or fork_rules.is_self_tool(tool):
            return None
        verdict = self._match(kind, tool, args=args, hook=hook, session=session)
        if verdict is None:
            return None
        self._count(verdict)
        if not self.enabled():
            self.record(verdict, applied=False, session=session, outcome="shadow",
                        note="shadow mode: nothing was applied")
            return None
        self.record(verdict, applied=True, session=session)
        return verdict

    def _match(self, kind: str, tool: str = "", *, args: Any = None, hook: str = "pre_tool",
               session: str = "", goal: str = "") -> Optional[Verdict]:
        """The rule lookup alone: no counter, no ledger row, no classification."""
        if not self.active() or fork_rules.is_self_tool(tool):
            return None
        key = fork_rules.fork_key(kind, tool, args=args, goal=goal)
        for rule in self._active(kind):
            if rule.get("key") != key or not fork_rules.may_apply(rule, hook=hook):
                continue
            return Verdict(key=key, decision=str(rule["decision"]), source="rule",
                           strength=str(rule["strength"]), rule_id=str(rule["id"]),
                           payload=dict(rule.get("action") or {}),
                           reason=str(rule.get("reason") or ""))
        return None

    def observe_tool(self, session: str, tool: str, args: Any, *, error: Any = "") -> None:
        """Feed the retry guard. Runs on every ``post_tool_call``, including forks off."""
        if not self.active() or fork_rules.is_self_tool(tool):
            return
        key = fork_rules.fork_key("retry", tool, args=args)
        message = str(error or "").strip()[:400]
        with self._lock:
            if len(self._failures) > MAX_FAILURE_KEYS:
                self._failures.clear()  # one process's working set, not a leak
            count, last = self._failures.get(key, (0, ""))
            self._failures[key] = (count + 1, message or last) if message else (0, "")

    def retry_guard(self, tool: str, args: Any, *, session: str = "") -> Optional[Verdict]:
        """Refuse the call after ``fork_retry_limit`` identical failures. Never learned."""
        if not self.enabled() or fork_rules.is_self_tool(tool):
            return None
        key = fork_rules.fork_key("retry", tool, args=args)
        with self._lock:
            count, last = self._failures.get(key, (0, ""))
        try:
            limit = max(1, int(self._config("fork_retry_limit", 2) or 2))
        except (TypeError, ValueError):
            limit = 2
        if count < limit:
            return None
        verdict = Verdict(key=key, decision="block", source="guard", strength="high",
                          reason=f"same failure {count} times: {last[:200]}")
        self.record(verdict, applied=True, session=session, note=last)
        return verdict

    def transform(self, session: str, tool: str, args: Any, result: str, *,
                  status: str = "") -> Optional[str]:
        """Distill one oversized result. ``None`` leaves it untouched; a marker is mandatory."""
        if not self.enabled() or fork_rules.is_self_tool(tool):
            return None
        if str(status or "").lower() in {"error", "failed", "timeout"}:
            return None  # an error is evidence: shortening it can hide the cause
        try:
            cap = max(400, int(self._config("fork_compress_chars", 4000) or 4000))
        except (TypeError, ValueError):
            cap = 4000
        text = str(result or "")
        if len(text) <= cap:
            return None
        verdict = self._match("scope", tool, args=args, hook="transform", session=session)
        if verdict is None:
            return None
        body, omitted = self._distill(text, cap)
        head = f"[jev:rule={verdict.rule_id or verdict.key} original={len(text)} chars"
        marker = f"{head}{omitted}]\n"
        out = marker + body
        self._count(verdict)
        self.record(verdict, applied=True, session=session,
                    note=f"{len(text)} -> {len(out)} chars")
        return out

    @staticmethod
    def _distill(text: str, cap: int) -> tuple[str, str]:
        lines = text.splitlines()
        if len(lines) < 4:  # one long line (JSON, a minified blob): no structure to keep
            return f"{text[:cap // 2]}\n…\n{text[-cap // 2:]}", ""
        keep = max(2, cap // 8)
        head, tail = lines[:keep], lines[-keep:]
        omitted = f", {len(lines) - len(head) - len(tail)} lines omitted"
        numbered = [*(f"{index}: {line}" for index, line in enumerate(head, start=1)),
                    "…",
                    *(f"{len(lines) - len(tail) + index}: {line}"
                      for index, line in enumerate(tail, start=1))]
        return "\n".join(numbered), omitted

    # -- gate path ---------------------------------------------------------

    def is_integration(self, tool: str, args: Any = None) -> bool:
        return fork_rules.is_integration(tool, args)

    def resolve_gate(self, session: str, tool: str, args: Any, *,
                     timeout: Optional[float] = None) -> Optional[Verdict]:
        """Synchronous no-go gate for one integration command.

        Fail-closed by design: a timeout, an exception or an empty answer blocks when
        ``gates: merge`` is set. ``shadow`` records the same verdict and never blocks.
        """
        mode = self.gate_mode()
        if mode == "off" or not self.is_integration(tool, args):
            return None
        adviser = getattr(self._provider, "_adviser_mode", None)
        if adviser is None or not adviser.enabled(session):
            # ``gates: merge`` without an adviser has nobody to ask. Blocking every merge would
            # be a trap set by a contradictory config, so the command passes and the warning
            # says why the operator's gate did nothing.
            logger.warning("jev gate: gates=%s but the adviser is off; `%s` is not gated",
                           mode, tool)
            return None
        if timeout is None:
            try:
                timeout = float(self._config("gate_timeout_s", 90) or 90)
            except (TypeError, ValueError):
                timeout = 90.0
        question = (
            f"Integration gate: the main agent is about to run `{tool}` with "
            f"{json.dumps(fork_rules.coerce_args(args), ensure_ascii=False)[:1200]}\n"
            "Judge whether this command should land now, against the turn below.\n"
            "First line, exactly: GO <one reason> or NO-GO <one reason naming a target> "
            "(tool_choice, current_step, user_intent, final_answer or general).")
        answer: Any = None
        try:
            answer = self._consult(question, session=session, timeout=timeout)
        except Exception as exc:  # a gate that raises still made no decision
            logger.warning("jev gate: consult failed: %s", exc)
            answer = None
        text = str((answer or {}).get("answer") or "").strip()
        key = fork_rules.fork_key("gate", tool, args=args)
        first = _first_line(text)
        case = _GO_LINE.match(first) if first else None
        if case is not None and case.group(1).lower() in {"go", "concern"}:
            verdict = Verdict(key=key, decision="allow", source="gate", strength="exact",
                              reason=text[:400] or "go")
            self.record(verdict, applied=False, session=session, outcome="go",
                        note="gate answered go")
            return None
        reason = text[:400] if text else (
            f"adviser gate timed out or answered nothing after {timeout:.0f}s")
        verdict = Verdict(key=key, decision="block", source="gate", strength="exact",
                          reason=reason)
        if mode != "merge":
            self.record(verdict, applied=False, session=session, outcome="shadow",
                        note="gate shadow: the command was not blocked")
            return None
        self.record(verdict, applied=True, session=session, note="gate blocked the command")
        return verdict

    def _consult(self, question: str, *, session: str, timeout: float) -> Any:
        from ..agentrun import agent_loop

        return agent_loop.run_with_timeout(
            lambda: self._provider._adviser_mode.consult(
                question, context=self._turn_digest(session), session=session),
            timeout)

    def _turn_digest(self, session: str) -> str:
        """The live turn, bounded: user message, tool calls, their results and the intent.

        The reviewer's delta is not enough here — a gate has to see what the turn is for. The
        ring holds this turn; the transcript facade keeps prior turns and is not re-read here.
        """
        from . import adviser_mode

        mode = getattr(self._provider, "_adviser_mode", None)
        if mode is None:
            return ""
        try:
            events = list(mode._ring(session))[-12:]
        except Exception:
            logger.debug("jev: turn digest unavailable", exc_info=True)
            return ""
        blocks = [adviser_mode._render_event(event) for event in events]
        return "\n\n".join(blocks)[:GATE_DIGEST_CHARS]

    # -- slow path ---------------------------------------------------------

    def judge(self, kind: str, tool: str = "", *, args: Any = None, goal: str = "",
              session: str = "", timeout: float = 30.0) -> Optional[Verdict]:
        """One adviser consult for a fork with no rule. Worker threads only.

        The consult contract answers in prose, so the answer is read as a decision line rather
        than JSON. Only ``inline`` and ``dispatch`` are actionable here: a rewrite or a
        compression needs an argument payload the review contract supplies, and a slow judge
        cannot invent one.
        """
        if not self.active():
            return None
        question = (
            "Fork judgement. One tool-level branch of the turn could be decided without the "
            "main model.\n"
            f"kind={kind} tool={tool or '-'} args="
            f"{json.dumps(fork_rules.coerce_args(args), ensure_ascii=False)[:1200]}\n"
            f"goal={str(goal or '')[:600]}\n"
            "Reply on the first line exactly: DECISION: <inline|dispatch|split>; REASON: <one "
            "sentence>. Answer split when the action is not mechanical.")
        try:
            answer = self._consult(question, session=session, timeout=timeout)
        except Exception as exc:
            logger.warning("jev judge: consult failed: %s", exc)
            return None
        text = str((answer or {}).get("answer") or "").strip()
        case = _DECISION_LINE.search(text)
        decision = case.group(1).lower() if case else ""
        if decision not in ("inline", "dispatch"):
            return None
        verdict = Verdict(key=fork_rules.fork_key(kind, tool, args=args, goal=goal),
                          decision=decision, source="adviser", strength="low",
                          reason=text[:400])
        self.record(verdict, applied=False, session=session, note="low: shadow until it hits twice")
        return verdict

    # -- route -------------------------------------------------------------

    def register_handler(self, stage: str, handler: Callable[..., Any]) -> None:
        """Register the inline handler for one stage. Membership is the whole question."""
        if str(stage or "").strip() and callable(handler):
            self._handlers[str(stage).strip()] = handler

    def has_handler(self, stage: str) -> bool:
        return str(stage or "").strip() in self._handlers

    def route(self, node: Mapping[str, Any], *, default: str = "dispatch",
              judge: Optional[Callable[[Mapping[str, Any]], Optional[Verdict]]] = None
              ) -> dict[str, Any]:
        """Pick an executor for one orchestrator node.

        ``inline`` needs all three: a stage whose acceptance is mechanical, a registered
        handler, and a rule (or a judge verdict) that says so. Anything else dispatches, which
        is the safe default — a dispatched node costs a model call, an inline node that took the
        wrong job costs a wrong result.
        """
        stage = str(node.get("stage") or "").strip()
        goal = str(node.get("goal") or node.get("id") or "")
        session = str(node.get("session") or "")
        verdict = self._match("route", goal=goal, hook="route", session=session)
        if verdict is None and judge is not None:
            verdict = judge(node)
        if verdict is None:
            return {"decision": default, "stage": stage, "handler": None,
                    "reason": "no route rule matched"}
        if verdict.decision == "dispatch":
            return {"decision": "dispatch", "stage": stage, "handler": None,
                    "reason": verdict.reason or "rule says dispatch"}
        handler = self._handlers.get(stage)
        if stage not in fork_rules.INLINE_STAGES:
            return {"decision": "dispatch", "stage": stage, "handler": None,
                    "reason": f"stage {stage or '-'} has no mechanical acceptance"}
        if handler is None:
            rejected = Verdict(key=verdict.key, decision="dispatch", source="guard",
                               strength=verdict.strength, rule_id=verdict.rule_id,
                               reason=f"no inline handler registered for stage {stage}")
            self.record(rejected, applied=False, session=session,
                        note="inline refused: no handler")
            return {"decision": "dispatch", "stage": stage, "handler": None,
                    "reason": rejected.reason}
        if not bool(node.get("mechanical")):
            return {"decision": "dispatch", "stage": stage, "handler": None,
                    "reason": "node acceptance is not mechanical"}
        return {"decision": "inline", "stage": stage, "handler": handler,
                "reason": verdict.reason}

    # -- candidates for the review ----------------------------------------

    def candidates(self, events: Iterable[Mapping[str, Any]], *, session: str = "",
                   limit: int = 3) -> list[dict[str, Any]]:
        """0-3 forks the reviewer should decide, scored; a covered fork is not re-asked."""
        goal = ""
        counts: dict[str, int] = {}
        failed: set[str] = set()
        rows: list[tuple[Mapping[str, Any], dict[str, Any], str]] = []
        for event in events or ():
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("kind") or "")
            if kind == "user" and str(event.get("text") or "").strip():
                goal = str(event.get("text") or "")
                continue
            if kind != "tool":
                continue
            tool = str(event.get("tool") or "")
            if not tool or fork_rules.is_self_tool(tool):
                continue
            args = fork_rules.coerce_args(event.get("args"))
            if not args:
                continue
            key = fork_rules.fork_key("arg", tool, args=args)
            counts[key] = counts.get(key, 0) + 1
            if str(event.get("error") or "").strip() or \
                    str(event.get("status") or "").lower() in {"error", "failed"}:
                failed.add(key)
            rows.append((event, args, key))
        wanted = _WORD.findall(goal.casefold())
        scored: dict[str, tuple[float, list[str], Mapping[str, Any], dict[str, Any]]] = {}
        for event, args, key in rows:
            if self._covered("arg", key):
                continue
            score = 0.0
            why: list[str] = []
            if key in failed:
                score += 3.0
                why.append("failed or was retried this turn")
            if counts.get(key, 0) >= 2:
                score += 2.0
                why.append(f"same shape {counts[key]}x this turn")
            text = (str(event.get("text") or "") + " "
                    + json.dumps(args, ensure_ascii=False)).casefold()
            if wanted and any(word in text for word in wanted):
                score += 1.0
                why.append("overlaps the user's goal")
            if key not in scored or scored[key][0] <= score:
                scored[key] = (score, why, event, args)
        ordered = sorted(scored.items(), key=lambda item: (-item[1][0], str(item[0])))
        out: list[dict[str, Any]] = []
        for index, (key, (score, why, event, args)) in enumerate(
                ordered[:max(0, int(limit))], start=1):
            out.append({
                "id": f"f{index}",
                "kind": "arg",
                "key": key,
                "tool": str(event.get("tool") or ""),
                "args": json.dumps(args, ensure_ascii=False)[:CANDIDATE_ARGS_CHARS],
                "arg_keys": sorted(str(key) for key in args),
                "recent": (str(event.get("error") or "") or str(event.get("result") or "")
                           )[:CANDIDATE_ARGS_CHARS],
                "why": "; ".join(why) or "repeated shape",
                "score": score,
            })
        return out

    # -- learned rules -----------------------------------------------------

    def apply_review_forks(self, session: str, verdicts: Iterable[Mapping[str, Any]]) -> int:
        """Write the reviewer's verdicts into ``learned.json`` as ``low`` rules. Single writer.

        The review worker is the only caller, which is what makes the read-modify-replace safe.
        """
        entries = [dict(item) for item in (verdicts or ()) if isinstance(item, Mapping)]
        if not entries:
            return 0
        rules = [rule for rule in self._read_entries(self._learned_path())
                 if fork_rules.normalize_rule(rule) is not None]
        by_key: dict[str, int] = {}
        for index, item in enumerate(rules):
            normalized = fork_rules.normalize_rule(item)
            if normalized is not None:
                by_key[str(normalized["key"])] = index
        now = time.time()
        written = 0
        for verdict in entries:
            if not verdict.get("learn", True):
                # A verdict without a reason is ledger-only: it says what happened, not what the
                # rule should be.
                self.record(Verdict(key=str(verdict.get("key") or ""),
                                    decision=str(verdict.get("decision") or "split"),
                                    source="adviser", strength="low",
                                    reason=str(verdict.get("reason") or "")),
                            applied=False, session=session, note="no reason: ledger only")
                continue
            rule = fork_rules.normalize_rule({
                "id": verdict.get("id"),
                "kind": verdict.get("kind"),
                "key": verdict.get("key"),
                "decision": verdict.get("decision"),
                "strength": "low",
                "authority": "inference",
                "action": verdict.get("action") or {},
                "reason": verdict.get("reason") or "",
                "scope": verdict.get("scope") or self.scope(),
                "ttl_days": verdict.get("ttl_days") or self._config("fork_ttl_days", 14),
                "created_at": now,
            })
            if rule is None:
                logger.warning("jev: refused a review verdict that would not load: %r",
                               verdict.get("key"))
                continue
            index = by_key.get(str(rule["key"]))
            if index is None:
                by_key[str(rule["key"])] = len(rules)
                rules.append(rule)
                written += 1
            else:
                kept = rules[index]
                if fork_rules.ladder(kept) >= fork_rules.STRENGTH["medium"]:
                    continue  # an active rule is not replaced by a fresh shadow
                rules[index] = rule
                written += 1
            self.record(Verdict(key=str(rule["key"]), decision=str(rule["decision"]),
                                source="adviser", strength="low", rule_id=str(rule["id"]),
                                reason=str(verdict.get("reason") or "")),
                        applied=False, session=session, note="learned as low")
        if not written:
            return 0
        self._write_learned(self._cap(rules))
        self.load(self.scope())
        return written

    def apply_grades(self, session: str, grades: Iterable[Mapping[str, Any]]) -> int:
        """Stamp the reviewer's ``accepted`` / ``overturned`` onto applied ledger rows.

        An overturn is the only signal that lowers a rule: it is counted here and the demotion
        happens in ``refresh_learned``, so the file stays the single place a strength changes.
        """
        store = getattr(self._provider, "_store", None)
        if store is None:
            return 0
        graded = 0
        for grade in grades or ():
            if not isinstance(grade, Mapping):
                continue
            try:
                ledger_id = int(grade.get("id") or 0)
            except (TypeError, ValueError):
                continue
            outcome = str(grade.get("verdict") or "").strip().lower()
            if not ledger_id or outcome not in {"accepted", "overturned"}:
                continue
            try:
                if not store.set_fork_outcome(ledger_id, outcome, str(grade.get("reason") or "")):
                    continue
            except Exception:
                logger.debug("jev: grade write failed", exc_info=True)
                continue
            graded += 1
            if outcome != "overturned":
                continue
            try:
                row = store.fork_ledger_entry(ledger_id) or {}
            except Exception:
                row = {}
            rule_id = str(row.get("rule_id") or "")
            if rule_id:
                self.note_overturn(rule_id, session=session,
                                   evidence=str(grade.get("reason") or ""))
        return graded

    def refresh_learned(self, *, now: Optional[float] = None) -> int:
        """Promote, demote and expire learned rules from their counters. Review-thread only."""
        raw = [rule for rule in self._read_entries(self._learned_path())
               if isinstance(rule, dict)]
        if not raw:
            return 0
        stamp = float(now if now is not None else time.time())
        stats = {}
        store = getattr(self._provider, "_store", None)
        if store is not None:
            try:
                stats = store.fork_stats()
            except Exception:
                logger.debug("jev: fork stats read failed", exc_info=True)
        kept: list[dict[str, Any]] = []
        changed = 0
        for item in raw:
            rule = fork_rules.normalize_rule(item)
            if rule is None:
                changed += 1
                continue
            ttl_days = int(rule["ttl_days"] or self._config("fork_ttl_days", 14) or 0)
            created = float(rule["created_at"] or 0.0)
            if ttl_days and created and created + ttl_days * 86_400.0 < stamp:
                changed += 1
                continue
            if str(rule["authority"]) == "operator":
                kept.append(item)
                continue
            counters = stats.get(str(rule["id"])) or {}
            hits = int(counters.get("hits") or 0)
            overturns = int(counters.get("overturns") or 0)
            strength = str(rule["strength"])
            if overturns >= DROP_OVERTURNS:
                changed += 1
                continue
            if overturns >= DEMOTE_OVERTURNS and strength != "low":
                item = {**item, "strength": "low"}
                changed += 1
            elif hits >= PROMOTE_HITS and overturns == 0 and strength == "low":
                item = {**item, "strength": "medium"}
                changed += 1
            kept.append(item)
        if not changed:
            return 0
        self._write_learned(kept)
        self.load(self.scope())
        return changed

    def note_overturn(self, rule_id: str, *, session: str = "", evidence: str = "") -> bool:
        """Record that the main model undid a rule's action; demotion happens in the review."""
        store = getattr(self._provider, "_store", None)
        if store is None or not rule_id:
            return False
        try:
            store.record_fork_overturn(rule_id, self.scope())
        except Exception:
            logger.debug("jev: overturn write failed", exc_info=True)
            return False
        self.refresh_learned()
        return True

    def _cap(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hold ``learned.json`` under ``fork_rules_max``: the least recently used go first."""
        try:
            cap = max(1, int(self._config("fork_rules_max", 500) or 500))
        except (TypeError, ValueError):
            cap = 500
        if len(rules) <= cap:
            return rules
        store = getattr(self._provider, "_store", None)
        stats: dict[str, dict[str, int]] = {}
        if store is not None:
            try:
                stats = store.fork_stats()
            except Exception:
                logger.debug("jev: fork stats read failed while capping", exc_info=True)

        def last_hit(item: dict[str, Any]) -> float:
            rule = fork_rules.normalize_rule(item)
            if rule is None:
                return 0.0
            return float((stats.get(str(rule["id"])) or {}).get("last_hit") or 0.0)

        ordered = sorted(rules, key=last_hit, reverse=True)
        return ordered[:cap]

    def _write_learned(self, rules: list[dict[str, Any]]) -> None:
        """Atomic replace: a reader never sees half a file, and a crash never needs a repair."""
        path = self._learned_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        payload = {"version": 1, "scope": self.scope(), "rules": rules}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # -- pull consult ------------------------------------------------------

    def advise(self, session: str, phase: str, question: str = "", *,
               timeout: Optional[float] = None) -> dict[str, Any]:
        """``minir_advise``: one read-only consult the main agent asked for."""
        phases = {"plan": "current_step", "merge": "tool_choice", "ship": "final_answer"}
        name = str(phase or "").strip().lower()
        if name not in phases:
            return {"error": f"phase must be one of: {', '.join(sorted(phases))}"}
        mode = getattr(self._provider, "_adviser_mode", None)
        if mode is None or not mode.enabled(session):
            return {"error": "the adviser is off; there is nothing to consult",
                    "verdict": "concern", "target": phases[name], "advice": ""}
        if timeout is None:
            # The pull consult shares the gate's budget: both are one synchronous adviser call
            # made while the agent waits, and a second key would only let the two drift.
            try:
                timeout = float(self._config("gate_timeout_s", 90) or 90)
            except (TypeError, ValueError):
                timeout = 90.0
        ask = (str(question or "").strip()
               or f"Judge this {name} step: is the plan sound, and what is missing?")
        prompt = (f"Pull consult ({name}).\n{ask}\n"
                  "First line exactly: GO, NO-GO or CONCERN followed by one reason. "
                  f"Second line: TARGET: {phases[name]} (or another advice target).")
        try:
            answer = self._consult(prompt, session=session, timeout=timeout)
        except Exception as exc:
            logger.warning("jev advise: consult failed: %s", exc)
            return {"error": f"consult failed: {exc}"}
        text = str((answer or {}).get("answer") or "").strip()
        if not text:
            return {"error": f"the adviser answered nothing within {timeout:.0f}s"}
        first = _first_line(text)
        case = _GO_LINE.match(first) if first else None
        verdict = {"go": "go", "no": "no-go", "no-go": "no-go", "concern": "concern"}.get(
            (case.group(1).lower().replace(" ", "-") if case else ""), "concern")
        target = phases[name]
        named = _TARGET_LINE.search(text)
        if named and named.group(1).lower() in _advice_targets():
            target = named.group(1).lower()
        return {"verdict": verdict, "target": target, "advice": text[:4000]}

    # -- bookkeeping -------------------------------------------------------

    def record(self, verdict: Verdict, *, applied: bool, session: str = "",
               outcome: str = "", note: str = "") -> int:
        """Write the ledger row for one decision. A plain split never reaches this method."""
        store = getattr(self._provider, "_store", None)
        if store is None or (not self.active() and self.gate_mode() == "off"):
            return 0
        try:
            return store.record_fork(
                str(verdict.key).split(":", 1)[0], verdict.key, verdict.decision,
                verdict.source, verdict.strength, session_id=session, applied=applied,
                outcome=outcome, note=note or verdict.reason, rule_id=verdict.rule_id)
        except Exception:
            logger.debug("jev: ledger write failed", exc_info=True)
            return 0

    def _count(self, verdict: Verdict) -> None:
        store = getattr(self._provider, "_store", None)
        if store is None or not verdict.rule_id:
            return
        try:
            store.record_fork_hit(verdict.rule_id, self.scope())
        except Exception:
            logger.debug("jev: fork hit write failed", exc_info=True)


def _first_line(text: str) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    return line.strip().strip("*_`# ").strip()


def _advice_targets() -> frozenset[str]:
    from ..prompting.prompting import DELIVERED_TARGETS

    return frozenset(DELIVERED_TARGETS)
