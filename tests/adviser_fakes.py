"""Test adapter: drive the reviewer's ``advise`` tool from a fake JSON answer.

The review path no longer parses the model's answer: the tool call *is* the output channel, and
the host run owns that tool for the run only (``extra_tools``). A test still wants to say "the
reviewer produced this note / this fork verdict", so it passes the old JSON shape and this
helper feeds it through the handler the run received. One call per note, exactly as a model
would do.
"""

from __future__ import annotations

import json
from typing import Any


def drive_advise(kwargs: dict[str, Any], answer: Any) -> list[str]:
    """Call ``advise`` for every item in a legacy JSON answer; returns the acks."""
    tools = kwargs.get("extra_tools") or []
    handler = next((tool.get("handler") for tool in tools
                    if isinstance(tool, dict) and tool.get("name") == "advise"), None)
    if handler is None:
        return []
    try:
        payload = json.loads(answer) if isinstance(answer, str) else answer
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    envelope = {key: payload[key] for key in ("forks", "fork_grades", "coverage")
                if payload.get(key)}
    acks: list[str] = []
    items = payload.get("advices") or []
    if not items and envelope:
        # Envelope-only answer (fork verdicts / coverage with no note).
        acks.append(handler(dict(envelope)))
    for item in items:
        if not isinstance(item, dict):
            continue
        args: dict[str, Any] = {
            "note": item.get("text") or item.get("content"),
            "severity": item.get("severity") or "concern",
            "target": item.get("target") or "general",
        }
        for key in ("decision", "evidence", "claim", "candidate_id", "deferred"):
            if item.get(key) is not None:
                args[key] = item[key]
        args.update(envelope)
        acks.append(handler(args))
    return acks
