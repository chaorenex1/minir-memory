"""Fork keys, the strength ladder and rule validation. No model, no I/O.

A *fork* is one tool-level branch that has an action which does not need the main model to
decide it: normalize an argument, refuse a third identical failure, distill an oversized
result, or pick an executor for an orchestrator node. Everything in this module is
deterministic and cheap because it runs inside ``pre_tool_call``, whose timeout is
fail-closed: a slow decision here does not delay a tool call, it refuses one.

Two normalizations decide whether a learned rule ever hits again:

* a path loses its leaf, so reading two sibling documents shares one key (``path_family``);
* a long number becomes ``<n>``, so a turn counter or a pid cannot fragment the key space.

Keys are matched exactly. A pattern language would add ambiguity and a test surface without
adding a decision the current rules cannot express.
"""

from __future__ import annotations

import json
import re
from hashlib import sha1
from typing import Any, Optional

#: Fork kinds a rule may name. ``retry`` and ``gate`` are key namespaces only: the retry guard is
#: a fixed predicate that is never learned, and a gate is the operator handing one decision to the
#: adviser, so neither is a rule kind.
KINDS: tuple[str, ...] = ("arg", "scope", "route")

#: Key namespaces: the three rule kinds plus the retry counter and the gate verdict.
KEY_KINDS: tuple[str, ...] = ("arg", "retry", "scope", "gate")

#: What a rule may do. ``block`` is deliberately absent: blocking belongs to the operator claim
#: gate, the retry guard and the integration gate, none of which is learned.
DECISIONS: tuple[str, ...] = ("rewrite", "compress", "inline", "dispatch")

STRENGTH: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3, "exact": 4}

#: Stages whose acceptance is mechanical (a count, an equality, a path that exists, an exit
#: code). Only these may run inline; everything else is dispatched to an agent.
INLINE_STAGES: tuple[str, ...] = ("fork", "merge")

#: Rule scope that applies under every project.
SCOPE_ALL = "*"

#: A command that rewrites history or lands someone else's patch. This is the merge gate's
#: whole trigger surface: reads and status commands are not integration.
INTEGRATION_HEADS: tuple[str, ...] = (
    "git merge", "git rebase", "git cherry-pick", "git apply", "patch -p", "gh pr merge",
)

_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+")
_LONG_NUMBER = re.compile(r"\b\d{2,}\b")
_WS = re.compile(r"\s+")


def path_family(value: str) -> str:
    """A path with its leaf replaced by ``*``: sibling reads share one family."""
    cleaned = str(value).replace("\\", "/").rstrip("/")
    head, _, _leaf = cleaned.rpartition("/")
    return f"{head}/*" if head else "*"


def _normalized(value: Any) -> Any:
    """Digit and path placeholders applied to string values, recursively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _PATH.sub(lambda match: path_family(match.group(0)),
                         _LONG_NUMBER.sub("<n>", value))
    if isinstance(value, int):
        return "<n>" if abs(value) >= 10 else value
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return value


def signature(args: Any) -> str:
    """Stable shape of tool args: paths become families, long numbers placeholders."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return signature({"raw": args})
    try:
        raw = json.dumps(_normalized(args), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(args)
    return raw[:400]


def fork_key(kind: str, tool: str = "", *, args: Any = None, goal: str = "") -> str:
    """The exact-match key for one fork. ``gate`` is a ledger namespace, not a rule kind."""
    if kind == "route":
        return f"route:{signature(goal)}"
    if kind in KEY_KINDS:
        return f"{kind}:{tool}:{signature(args)}"
    raise ValueError(f"unknown fork kind {kind!r}")


def rule_id(kind: str, key: str) -> str:
    """Deterministic id for a rule that was written without one."""
    return f"r-{kind}-{sha1(key.encode('utf-8')).hexdigest()[:6]}"


def ladder(rule: dict[str, Any]) -> int:
    return int(STRENGTH.get(str(rule.get("strength") or "none"), 0))


def scope_ok(rule: dict[str, Any], scope: str) -> bool:
    """A rule applies under its own scope or under ``*``; an empty scope is current."""
    declared = str(rule.get("scope") or "").strip()
    return not declared or declared == SCOPE_ALL or not scope or declared == scope


def normalize_rule(entry: Any, *, scope: str = "") -> Optional[dict[str, Any]]:
    """One loaded entry -> one usable rule, or ``None``.

    A malformed entry is skipped and named by the caller: refusing the whole file would let one
    hand-edited line disable every rule beside it. ``block`` is not a rule decision, so an
    entry that asks for one is dropped here rather than trusted later.
    """
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("kind") or "").strip()
    if kind not in KINDS:
        return None
    key = str(entry.get("key") or "").strip()
    if not key.startswith(f"{kind}:"):
        return None
    decision = str(entry.get("decision") or "").strip()
    if decision not in DECISIONS:
        return None
    strength = str(entry.get("strength") or "").strip()
    if strength not in STRENGTH or strength == "none":
        return None
    action = entry.get("action")
    action = dict(action) if isinstance(action, dict) else {}
    if decision == "rewrite":
        args = action.get("args")
        if not isinstance(args, dict) or not args:
            return None
        action["args"] = dict(args)
    try:
        ttl_days = max(0, int(entry.get("ttl_days") if entry.get("ttl_days") is not None else 0))
    except (TypeError, ValueError):
        ttl_days = 0
    try:
        created_at = float(entry.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    return {
        "id": str(entry.get("id") or "").strip() or rule_id(kind, key),
        "kind": kind,
        "key": key,
        "decision": decision,
        "strength": strength,
        "action": action,
        "scope": str(entry.get("scope") or scope or "").strip(),
        "authority": str(entry.get("authority") or "operator").strip(),
        # Why the rule exists. A learned rule without one is refused before it reaches the file;
        # an operator rule keeps whatever the operator wrote.
        "reason": str(entry.get("reason") or "")[:400],
        "ttl_days": ttl_days,
        "created_at": created_at,
    }


def may_apply(rule: dict[str, Any], *, hook: str) -> bool:
    """Each hook accepts exactly one decision shape; a low rule stays shadow.

    ``low`` is an adviser verdict that has not been seen twice yet. It is readable, listable
    and revocable, but it never changes a call: a single model opinion is not a rule.
    """
    decision = str(rule.get("decision") or "")
    if decision not in DECISIONS or ladder(rule) < STRENGTH["medium"]:
        return False
    if hook == "transform":
        return decision == "compress"
    if hook == "route":
        return decision in {"inline", "dispatch"}
    return decision == "rewrite"


def coerce_args(args: Any) -> dict[str, Any]:
    """The hook usually hands a dict; a JSON string is parsed so the gate cannot skip silently."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def is_integration(tool: str, args: Any = None) -> bool:
    """True only for a terminal command that lands history; reads and status are not a gate."""
    if str(tool or "") != "terminal":
        return False
    command = str(coerce_args(args).get("command") or "").strip().lower()
    if not command:
        return False
    return _WS.sub(" ", command).startswith(INTEGRATION_HEADS)


def is_self_tool(tool: str) -> bool:
    """The dispatcher's own tools never produce a fork or a replaced result."""
    name = str(tool or "")
    return name.startswith("minir_") or name.startswith("adviser_")
