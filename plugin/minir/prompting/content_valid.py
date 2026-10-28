"""Deterministic length check for an agent's own output.

An agent cannot count its own tokens, and "feels long enough" is not a budget. This module turns
the two counts the plugin already trusts into one local tool call: Unicode code points for a
character budget, :func:`rules.estimate_tokens` for a token budget. It reads no store and calls
no model, so it is available before the provider is initialized.

:func:`handle` is the tool handler; :func:`measure` is the same count without the JSON envelope.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import tool_error

from . import rules

TOOL_NAME = "minir_content_valid"
MODES: tuple[str, ...] = ("string", "token")
_UNIT = {"string": "chars", "token": "tokens"}
_TOKEN_ESTIMATOR = "rules.estimate_tokens"


def measure(content: str, mode: str = "string") -> int:
    """Characters (code points) in ``string`` mode, estimated tokens in ``token`` mode."""
    if mode == "token":
        return rules.estimate_tokens(content)
    return len(content)


def handle(args: dict[str, Any]) -> str:
    """Measure one payload against one limit and return the report as JSON.

    The tool reports; it never truncates, rewrites or stores. A caller over budget deletes its
    weakest lines and measures again.
    """
    args = args or {}
    content = args.get("content")
    if not isinstance(content, str):
        return tool_error("content is required and must be a string")
    mode = str(args.get("mode") or "string").strip().lower()
    if mode not in MODES:
        return tool_error("mode must be 'string' or 'token'")
    limit = args.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, (int, float, str)):
        return tool_error("limit is required and must be an integer")
    try:
        bounded = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit is required and must be an integer")
    if bounded < 1:
        return tool_error("limit must be at least 1")

    measured = measure(content, mode)
    report: dict[str, Any] = {
        "ok": measured <= bounded,
        "mode": mode,
        "unit": _UNIT[mode],
        "measured": measured,
        "limit": bounded,
        "over": max(0, measured - bounded),
        "remaining": max(0, bounded - measured),
    }
    if mode == "token":
        report["estimator"] = _TOKEN_ESTIMATOR
    return json.dumps(report)
