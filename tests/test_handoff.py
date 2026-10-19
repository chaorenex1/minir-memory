"""Handoff summary agent — the background run must keep the caller's context.

The summary model runs on a daemon thread so ``compress()`` can bound it with a timeout. On a
multiplexed gateway the route's API key is resolved through a ContextVar
(``agent.secret_scope``), so a bare thread would fail closed instead of reading the profile's
secret — the scheduling context has to travel with the run.
"""

from __future__ import annotations

import contextvars


def test_the_summary_thread_keeps_the_callers_context(plugin_package):
    scope = contextvars.ContextVar("minir_test_scope", default="")
    seen: dict = {}

    def _run() -> str:
        seen["scope"] = scope.get()
        return "ok"

    token = scope.set("profile-a")
    try:
        assert plugin_package.handoff._run_with_timeout(_run, 2.0) == "ok"
    finally:
        scope.reset(token)
    assert seen["scope"] == "profile-a"


def test_a_failure_still_comes_back_to_the_caller(plugin_package):
    def _run() -> str:
        raise ValueError("route down")

    try:
        plugin_package.handoff._run_with_timeout(_run, 2.0)
    except ValueError as exc:
        assert str(exc) == "route down"
    else:  # pragma: no cover - the assertion below is the real check
        raise AssertionError("the worker's exception must reach the caller")
