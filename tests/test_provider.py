"""Provider behavior — lifecycle, recall, persistence, counting, and tools."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest


def _no_model_call(*args, **kwargs):
    """Poisons the layer model call: any test that reaches it fails loudly."""
    raise AssertionError("the layer worker must not call the model in this test")


def _base_user_json():
    return json.dumps({
        "user_self_representation": "## Self\n\n- Occupation: Engineer\n- Skills: Python\n- Communication: direct",
        "user_preferences": "",
    })


def _base_ai_json():
    return json.dumps({
        "ai_self_representation": ("## Self\n\n- Positioning: executor\n- Task: ship the plugin\n"
                    "- Known: the window is the evidence\n- State: executing"),
        "ai_identity": ("## Identity\n\n- Name: minir\n- Version: 0.8.0\n"
                        "- Duties: maintain the memory layer"),
    })


def _summary_section():
    """The markdown section a base refresh returns, in the template's shape."""
    return "\n\n".join([
        "## Window Summary",
        "### Objective\ndeploy on Windows",
        "### Current State\n#### Done\n- deployed",
        "#### Doing",
        "#### Todo",
        "### Decisions",
        "### Constraints",
        "### Verified Facts\n- it runs — evidence: smoke test",
        "### Open Issues",
        "### Next Actions\n- [high] verify",
        "### References",
    ])


def _summary_json():
    return json.dumps({"window_summary": _summary_section()})


def _units_json(*, markers=None) -> str:
    """B4's reply: this window's units (one by default)."""
    marker = {"id": "k1", "category": "METHOD", "org": "tools", "topic": "pytest verification",
              "content": "run the project's verify command\nread its exit code",
              "summary": "verify with pytest", "relations": [], "objects": [],
              "anchor": {"from_ts": 0.0, "to_ts": 1e12, "message_ids": []}}
    return json.dumps({"value_markers": [marker] if markers is None else markers})


def _base_reply(prompt):
    """Which base segment the worker is running, picked by the PASS line it carries."""
    if "PASS: b3_window_summary" in prompt:
        return _summary_json()
    if "PASS: b4_value_markers" in prompt:
        return _units_json()
    if "PASS: b2_ai_card" in prompt:
        return _base_ai_json()
    return _base_user_json()


def _dialectic_json(*, changed=False, fields=None, peer=None):
    lines = "\n".join(f"- {item}" for item in (peer if peer is not None else ["wants evidence"]))
    ai_card = json.loads(_base_ai_json())
    return json.dumps({
        "user_card": {
            "user_self_representation": "## Self\n\n- Occupation: Engineer",
            "user_preferences": "## Preferences\n\n" + lines,
        },
        "ai_card": ai_card,
        "window_summary": _summary_section(),
        "base_changed": {"changed": bool(changed), "fields": list(fields or [])},
        "changes": [],
        "prunings": [],
    })



def test_initialize_creates_profile_scoped_store(provider, tmp_path):
    assert (tmp_path / "minir-memory" / "memory.db").exists()


def test_conclusion_rows_survive_a_new_instance(plugin_package, make_provider):
    first = make_provider({})
    first._store.save_conclusion(
        "session_summary", "We standardized on SQLite for the store",
        session_id=first._session_id, subject="session")
    first.shutdown()

    second = make_provider({})  # same tmp home -> same db file
    block = second.prefetch("SQLite store")
    assert block == "", "prefetch injects layer state only, never explicit recall"
    found = json.loads(second.handle_tool_call("minir_search", {"query": "SQLite store"}))
    assert found["count"] >= 1, "the row is still reachable through minir_search"


def test_no_conversation_content_is_persisted_anywhere(plugin_package, make_provider, monkeypatch):
    """The provider's whole point of honour: turns are reasoned over in memory and dropped."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, system, prompt, **kwargs: (
        _dialectic_json()
        if "dialectic reviewer" in system else
        _base_reply(prompt)))
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    provider.sync_turn("the secret sauce is a staging cluster in Frankfurt", "noted")
    provider._worker.stop()
    db_path = provider._store.path
    provider.shutdown()

    connection = sqlite3.connect(str(db_path))
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        assert "turns" not in tables
        for table in tables:
            if table.startswith("sqlite_") or table.startswith("conclusions_vec"):
                continue  # vec0 shadow tables need the extension loaded to read
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            assert "Frankfurt" not in str(rows), f"{table} must hold no conversation content"
    finally:
        connection.close()


def test_a_restart_expires_windows_it_can_no_longer_read(plugin_package, make_provider):
    """The content lived in the old process's memory; the job it queued must not be retried."""
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    pending = provider._store.pending_jobs()
    assert pending, "the tick queued jobs"
    db_path = provider._store.path
    provider.shutdown()

    # A later process opens the same store: the jobs are closed, not re-queued.
    reopened = plugin_package.store.MinirStore(db_path)
    try:
        later = time.time() + 0.001  # that process started just after these jobs were queued
        assert reopened.expire_open_jobs("gone", before=later) == len(pending)
        assert reopened.pending_jobs() == []
        statuses = {row["status"] for row in reopened._conn.execute("SELECT status FROM jobs")}
        assert statuses == {"expired"}
    finally:
        reopened.close()


def test_sync_turn_keeps_the_author_for_the_base_layer(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.sync_turn("kv cache tuning preferences", "Noted.",
                       turn_author={"id": "u1", "name": "alice", "is_bot": False})
    assert [(row["author"], row["author_id"]) for row in provider._transcript.messages("session-test")] \
        == [("alice", "u1"), ("", "")]


def test_sync_turn_does_nothing_when_the_layers_are_off(make_provider):
    provider = make_provider({})
    provider.sync_turn("something important about staging deploys", "ok")
    assert provider._transcript.messages("session-test") == []


def test_sync_turn_skipped_for_non_primary_context(plugin_package, make_provider, monkeypatch):
    """Host contract: subagent/cron/flush contexts must not write memory."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({}, agent_context="subagent")
    provider.sync_turn("subagent scratch work that should not persist", "ok")
    assert provider._transcript.messages("session-test") == []


def test_sync_turn_skips_trivial_prompts(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.sync_turn("thanks!", "You're welcome.")
    assert provider._transcript.messages("session-test") == []


def test_a_message_never_lands_on_a_window_edge(plugin_package):
    """Windows are ``(from, to]``, so a message stamped exactly on an edge would be invisible to
    both neighbouring windows. Clocks are coarser than the tick→arrival gap, so the buffer keeps
    every message strictly past the newest edge instead of relying on the clock."""
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.mark_edge("s", 100.0)
    buffer.add("s", "user", "on the edge", created_at=100.0)   # a tie with the edge
    buffer.add("s", "user", "before the edge", created_at=50.0)
    stamps = [row["created_at"] for row in buffer.messages("s")]
    assert all(stamp > 100.0 for stamp in stamps)
    assert [row["content"] for row in buffer.window("s", 100.0, 100.01)] == ["on the edge",
                                                                           "before the edge"]
    buffer.mark_edge("s", 50.0)  # an older edge must never move the mark back
    buffer.add("s", "user", "after", created_at=150.0)
    assert buffer.messages("s")[-1]["created_at"] == 150.0


def test_the_provider_never_buffers_a_message_on_an_edge(plugin_package, make_provider, monkeypatch):
    """The regression that motivated ``mark_edge``: an interval of 1 puts a window edge on the
    very turn that follows it, and a coarse clock can stamp the message on that edge."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    edge = provider._store.pending_jobs()[0]["to_ts"]
    provider.sync_turn("deploy the plugin on Windows", "noted")
    message = provider._transcript.messages("session-test")[0]
    assert message["created_at"] > edge
    assert provider._transcript.window("session-test", edge, time.time() + 1), \
        "the turn that triggered the window must still be inside it"


def test_reasoning_can_be_enabled_by_env(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({"reasoning_enabled": False})  # env wins over config
    assert provider._reasoning is True
    assert provider._worker.enabled is True


def test_an_unknown_remember_mode_is_an_error(provider):
    payload = json.loads(provider.handle_tool_call("minir_remember", {
        "type": "tools", "content": "docker prune", "remember_mode": "page"}))

    assert "error" in payload


def test_session_extraction_writes_a_page_and_leaves_the_conclusion_row_alone(
        plugin_package, make_provider, monkeypatch):
    provider = make_provider({})
    session = provider._session_id
    note_id = provider._store.save_conclusion(
        "session_summary", "prefer pytest over unittest" * 3,
        session_id=session, subject="session")
    before = dict(provider._store.conclusion_by_id(note_id))
    before_versions = provider._store._conn.execute(
        "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_id = ?",
        (note_id,)).fetchone()[0]
    page = {"path": "tools/METHOD-pytest-tips.md", "category": "METHOD", "org": "tools",
            "topic": "pytest tips", "summary": "prefer pytest",
            "content": "1. run pytest -q.\n2. keep tests short.",
            "time": "2026-10-25", "objects": [], "relations": []}
    monkeypatch.setattr(plugin_package.session_summary, "summarize_agent",
                        lambda **kwargs: "")
    monkeypatch.setattr(plugin_package.extraction, "extract_pages", lambda **kwargs: [page])

    result = provider._run_session_extraction(
        {"session_id": session, "markers": "{}", "window_to_ts": 0.0})

    after = dict(provider._store.conclusion_by_id(note_id))
    assert result["pages"] == 1
    assert (provider._knowledge.root / "tools" / "METHOD-pytest-tips.md").is_file()
    assert after["content"] == before["content"] and after["version"] == before["version"]
    assert provider._store._conn.execute(
        "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_id = ?",
        (note_id,)).fetchone()[0] == before_versions


def test_remember_without_a_category_needs_a_numbered_procedure(make_provider):
    provider = make_provider({})
    prose = json.loads(provider.handle_tool_call("minir_remember", {
        "type": "tools", "remember_mode": "knowledge", "summary": "daemon restart",
        "content": "The daemon restarted cleanly.\nNo image was lost.\nThe cache stayed warm."}))
    assert "error" in prose

    steps = json.loads(provider.handle_tool_call("minir_remember", {
        "type": "tools", "remember_mode": "knowledge", "summary": "docker prune procedure",
        "topic": "docker prune procedure",
        "content": "1. pull the image.\n2. prune the dangling layers.\n3. restart the daemon."}))
    assert (steps["category"], steps["queued"]) == ("METHOD", True)


def test_the_same_remember_twice_writes_one_page(make_provider):
    provider = make_provider({})
    args = {"type": "tools", "remember_mode": "knowledge", "category": "METHOD",
            "topic": "docker prune", "summary": "prune",
            "content": "1. stop the daemon.\n2. prune the images.\n3. restart it."}

    first = json.loads(provider.handle_tool_call("minir_remember", args))
    second = json.loads(provider.handle_tool_call("minir_remember", args))
    provider._knowledge.stop()

    assert first["queued"] is True and second["queued"] is True
    assert len(list((provider._knowledge.root / "tools").glob("*.md"))) == 1


def test_history_maintenance_runs_with_reasoning_off(make_provider):
    provider = make_provider({"reasoning_enabled": False})

    inventory = json.loads(provider.handle_tool_call("minir_stats", {}))["scheduler"]

    assert "conclusion_history_maintenance" in inventory
    assert "card_review" not in inventory


def test_prefetch_on_empty_store_is_blank(provider):
    assert provider.prefetch("nothing was ever stored") == ""
    assert provider.recall_status() is None


def test_a_failed_base_window_leaves_a_stale_signal(provider):
    provider._store.record_layer_failure(
        "base", provider._worker.user_subject(), window_key="s:0",
        session_id=provider._session_id, window_from_ts=0, window_to_ts=1,
        error="LayerMergeError: boom")
    block = provider.prefetch("x")
    assert block.startswith("## Minir memory gaps")
    assert "boom" in block and "not evidence" in block


def _save_base_row(provider, *, session: str = "", ai_self: str = "", summary: str = "",
                   to_ts: float = 1.0) -> None:
    """One live ``base_refreshes`` row; the columns are JSON strings, as the worker writes them."""
    provider._store.add_base_refresh(
        session or provider._session_id, "user:default",
        window_from_ts=to_ts - 1.0, window_to_ts=to_ts, window_from_turn=1, window_to_turn=1,
        user_representation="", user_peer_card="",
        ai_self_representation=json.dumps(ai_self), ai_identity_card="",
        window_summary=json.dumps(summary))


def test_prefetch_injects_the_live_base_columns(provider):
    _save_base_row(provider, ai_self="- direct\n- terse",
                   summary="## Window Summary\n\n### Objective\n- ship prefetch\n\n"
                           "### Current State\n#### Done\n- read the row")
    block = provider.prefetch("unrelated query")
    assert block.startswith("## Minir layer state")
    assert "### AI self representation" in block and "- terse" in block
    assert "### Window summary" in block and "- ship prefetch" in block
    assert "## Minir recall" not in block and "[knowledge" not in block


def test_prefetch_reads_only_this_sessions_window(provider):
    _save_base_row(provider, session="other", ai_self="other stance",
                   summary="### Objective\n- other goal", to_ts=9.0)
    assert provider.prefetch("x") == "", "another session's live row is never injected"
    _save_base_row(provider, ai_self="mine here", summary="### Objective\n- my goal", to_ts=2.0)
    block = provider.prefetch("x")
    assert "mine here" in block and "my goal" in block and "other goal" not in block


def test_the_window_budget_keeps_the_goal_ledger(provider):
    goals = "\n".join(f"- goal {index}" for index in range(40))
    decisions = "\n".join(f"- decision {index} " + "d" * 120 for index in range(20))
    _save_base_row(provider, summary="\n\n".join([
        "## Window Summary", f"### Objective\n{goals}",
        "### Current State\n#### Done\n#### Doing\n- goal 1\n#### Todo",
        f"### Decisions\n{decisions}", "### Constraints", "### Verified Facts",
        "### Open Issues", "### Next Actions", "### References"]))
    block = provider.prefetch("x")
    assert "- goal 39" in block
    assert "[summary truncated]" in block
    assert "decision 19" not in block
    assert len(block.split("### AI self representation")[0]) < 2200


def test_the_ai_self_cap_is_400(provider):
    _save_base_row(provider, ai_self="a" * 900)
    block = provider.prefetch("x")
    body = block.split("### AI self representation\n", 1)[1]
    assert len(body) == 400


def test_a_marker_row_is_searchable_but_no_tool_writes_or_deletes_it(provider):
    row_id = provider._store.save_conclusion(
        "experiences", "prefers terse output", session_id=provider._session_id)
    found = json.loads(provider.handle_tool_call("minir_search", {"query": "terse output"}))
    assert found["count"] >= 1 and found["scope"] == "session"
    marker = next(row for row in found["results"] if row["kind"] == "experiences")
    assert marker["conclusions_id"] == row_id
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_remember", {"type": "experiences", "content": "prefers terse output"})), \
        "there is no conclusion mode"
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_forget", {"type": "experiences", "conclusions_id": row_id})), \
        "no mode deletes a conclusion row"
    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert "notes" not in stats and "markers" not in stats and "vectors" not in stats
    assert "db_path" in stats
    assert len(stats["targets"]) == 7, "one row per consistency target"
    assert stats["conclusions_by_kind"] == {"experiences": 1}


def test_search_identifies_every_row_by_kind_and_conclusions_id(provider):
    row_id = provider._store.save_conclusion("note", "untyped durable note")
    payload = json.loads(provider.handle_tool_call(
        "minir_search", {"query": "untyped durable note"}))
    note = next(row for row in payload["results"] if row["kind"] == "note")
    assert note["conclusions_id"] == row_id
    assert "marker_type" not in note and "category" not in note
    assert "note_id" not in note and "id" not in note
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_forget", {"conclusions_id": row_id})), "conclusions_id has no mode left"


def test_tool_argument_validation(provider):
    assert "error" in json.loads(provider.handle_tool_call("minir_search", {"query": "  "}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_search", {"query": "x", "scope": "galaxy"}))
    assert "error" in json.loads(provider.handle_tool_call("minir_remember", {}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_remember", {"type": "nonsense", "content": "x"}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_remember", {"content": "x"}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_forget", {"conclusions_id": "abc"}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_forget", {"type": "nonsense", "conclusions_id": 1}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_search", {"query": "x", "type": "nonsense"}))


def test_kb_search_answers_from_the_knowledge_directory(provider, plugin_package):
    root = plugin_package.knowledge.knowledge_dir(provider._hermes_home)
    plugin_package.knowledge.write_unit(
        {"id": "u1", "path": "tools/METHOD-Docker-prune.md", "org": "tools",
         "category": "METHOD", "topic": "Docker prune",
         "content": "docker system prune -af", "time": "2026-10-21", "relations": [],
         "objects": ["tools:Docker"]}, root=root)
    if plugin_package.knowledge.rgtool.find(provider._hermes_home) is None:
        pytest.skip("ripgrep is not installed")

    payload = json.loads(provider.handle_tool_call("minir_kb_search",
                                                   {"query": "docker.*prune"}))

    assert payload["count"] == 1 and payload["total"] == 1
    assert payload["root"] == str(root)
    row = payload["results"][0]
    assert (row["org"], row["category"], row["topic"]) == ("tools", "METHOD", "Docker prune")
    assert row["path"].endswith("METHOD-Docker-prune.md")
    assert not row["match"]["text"].startswith("topic:"), "body lines are quoted first"
    assert any("system prune" in hit["text"] for hit in row["lines"])


def test_kb_search_rejects_bad_arguments_and_never_leaves_the_root(provider, plugin_package,
                                                                  monkeypatch):
    assert "error" in json.loads(provider.handle_tool_call("minir_kb_search", {"query": " "}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_kb_search", {"query": "x", "org": "nope"}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_kb_search", {"query": "x", "category": "NOPE"}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_kb_search", {"query": "x", "limit": "lots"}))

    root = plugin_package.knowledge.knowledge_dir(provider._hermes_home)
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(plugin_package.knowledge.rgtool, "matches", lambda *args, **kwargs: [
        {"path": str(root.parent / "outside.md"), "line": 1, "text": "docker"},
        {"path": str(root / "loose.md"), "line": 1, "text": "docker"}])
    payload = json.loads(provider.handle_tool_call("minir_kb_search", {"query": "docker"}))
    assert payload["count"] == 0, "only <org>/<page>.md inside the knowledge root is a page"

    monkeypatch.setattr(plugin_package.knowledge.rgtool, "matches",
                        lambda *args, **kwargs: None)
    assert "error" in json.loads(provider.handle_tool_call("minir_kb_search", {"query": "x"}))


def test_kb_write_creates_a_page_through_the_tool(provider, plugin_package):
    """The tool path must reach ``edit_page`` with the provider's language, not raise."""
    body = ("docker system prune -af drops stopped containers\n"
            "add --volumes to release unused volumes\n"
            "check free space afterwards")
    payload = json.loads(provider.handle_tool_call(
        "minir_kb_write",
        {"path": "tools/METHOD-Docker-prune.md", "op": "create", "topic": "Docker prune",
         "summary": "reclaim disk space", "content": body}))

    assert payload["status"] == "created" and payload["verified"] is True, payload
    assert provider._language == "en"
    page = plugin_package.knowledge.knowledge_dir(provider._hermes_home) / "tools" / "METHOD-Docker-prune.md"
    assert page.is_file() and "docker system prune -af" in page.read_text(encoding="utf-8")


def test_kb_write_carries_a_page_past_the_old_body_caps(provider, plugin_package):
    """The tool path takes no page-size budget: a long page lands byte for byte."""
    body = "\n".join(f"{index}. claim {index} " + "filler " * 40 for index in range(16))
    payload = json.loads(provider.handle_tool_call(
        "minir_kb_write",
        {"path": "tools/METHOD-long-page.md", "op": "create", "topic": "long page",
         "summary": "a page past the old body caps", "content": body}))

    assert payload["status"] == "created" and payload["verified"] is True, payload
    assert payload["lines"] == 16 and payload["warnings"] == []
    page = (plugin_package.knowledge.knowledge_dir(provider._hermes_home)
            / "tools" / "METHOD-long-page.md")
    assert page.read_text(encoding="utf-8").count("claim ") == 16


def test_search_scope_all_crosses_sessions(provider):
    provider._store.save_conclusion(
        "projects", "cross session fact", session_id=provider._session_id)
    provider.on_session_switch("other-session")
    current = json.loads(provider.handle_tool_call(
        "minir_search", {"query": "cross session fact"}))
    assert current["count"] == 0, "the default scope must not leak another session's marker"
    across = json.loads(provider.handle_tool_call(
        "minir_search", {"query": "cross session fact", "scope": "all"}))
    assert across["scope"] == "all" and across["count"] == 1
    assert across["results"][0]["kind"] == "projects"


def test_on_session_switch_rebinds_and_reset_clears_cache(provider):
    provider._store.save_conclusion(
        "experiences", "switch test fact", session_id=provider._session_id)
    provider.on_session_switch("session-2")
    assert provider._session_id == "session-2"
    provider.on_session_switch("session-3", reset=True)
    # The marker is session-bound: automatic recall must not leak it into session-3.
    assert provider.prefetch("switch test") == ""
    found = json.loads(provider.handle_tool_call(
        "minir_search", {"query": "switch test", "scope": "all"}))
    assert found["count"] == 1, "explicit cross-session search must still find it"


def test_shutdown_is_idempotent(plugin_package, make_provider):
    provider = make_provider({})
    provider.shutdown()
    provider.shutdown()  # second call must not raise


def test_search_results_are_excerpted(provider):
    provider._store.save_conclusion(
        "experiences", "long " * 400, session_id=provider._session_id)
    payload = json.loads(provider.handle_tool_call("minir_search", {"query": "long"}))
    assert payload["results"]
    assert all(len(row["text"]) <= 601 for row in payload["results"])


# -- turn counting (base / dialectic layers) ----------------------------------

def _jobs(provider):
    """The queued layer jobs as ``(layer, turns)`` pairs, oldest first."""
    return [(job["layer"], job["turns"]) for job in provider._store.pending_jobs()]


def test_turn_start_queues_both_layers_on_their_own_cadence(make_provider):
    """Two independent watermarks: each layer queues its own job; base comes first on a tie."""
    provider = make_provider({"base_interval": 3, "dialectic_interval": 2})
    for turn in range(1, 7):
        provider.on_turn_start(turn, f"message {turn}")
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"], job["session_id"]) for job in jobs] == [
        ("base", 3, "session-test"),
        ("dialectic", 5, "session-test"),
        ("base", 6, "session-test"),
    ]
    assert all(job["status"] == "pending" and job["from_ts"] <= job["to_ts"] for job in jobs)


def test_new_session_ordering_keeps_the_tail(plugin_package, make_provider):
    """The host's ``/new`` order: ``on_session_finalize`` (old id) stops the session
    synchronously, then the queued ``on_session_end`` (old id) flushes the tail. The stop
    must not swallow it — and the later ``on_session_reset`` must not duplicate it."""
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail before /new", "noted")
    plugin_package._on_session_finalize(session_id="session-test")   # frozen first
    provider.on_session_end([])                              # ... tail arrives after
    plugin_package._on_session_reset(session_id="session-next", old_session_id="session-test")
    assert _jobs(provider) == [("base", 4), ("dialectic", 4)]


def test_session_end_flushes_the_tail_once(make_provider):
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    provider.on_session_end([])
    provider.on_session_end([])  # a second boundary hook must not re-queue the window
    assert _jobs(provider) == [("base", 4), ("dialectic", 4)]


def test_session_end_on_a_multiple_queues_no_extra_job(make_provider):
    """The spec's "no boundary trigger when the count sits on a multiple" case."""
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    provider.on_session_end([])
    # No review before the first base trigger in the *cadence*; the boundary still queues the
    # dependent review of the base window it just flushed.
    assert _jobs(provider) == [("base", 3), ("dialectic", 3)]


def test_pre_compress_without_reasoning_does_not_queue_a_job(make_provider):
    provider = make_provider({"base_interval": 50, "dialectic_interval": 2})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    assert provider.on_pre_compress([]) == ""
    # No base trigger yet, so the review cadence has not started; pre-compression queues nothing.
    assert _jobs(provider) == [], "no worker means no layer refresh job"
    for turn in (4, 5):
        provider.on_turn_start(turn, "m")
    assert _jobs(provider) == []


def test_pre_compress_pauses_scheduling_and_resets_both_layers(plugin_package, make_provider,
                                                               monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, system, prompt, **kwargs: (
        _dialectic_json(peer=["wants evidence", "no emoji"], changed=True,
                        fields=["user_preferences"])
        if "dialectic reviewer" in system
        else _base_reply(prompt)))
    provider = make_provider({"base_interval": 2, "dialectic_interval": 2})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider.on_pre_compress([])

    snapshot = plugin_package.turn_counter.TRACKER.snapshot("session-test")
    assert snapshot["layers"]["base"]["last_trigger_turn"] == 3
    assert snapshot["layers"]["dialectic"]["last_trigger_turn"] == 3
    assert provider._store.compression_gate_active("session-test") is True

    for turn in (4, 5):
        provider.on_turn_start(turn, "m")
    assert _jobs(provider) == [], "periodic scheduling is paused"

    provider._store.clear_compression_gate("session-test")
    provider.on_turn_start(6, "m")
    assert len(provider._store.pending_jobs()) >= 1, "scheduling resumes after the gate"


def test_pre_compress_drains_base_and_dependent_dialectic(plugin_package, make_provider,
                                                          monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, system, prompt, **kwargs: (
        _dialectic_json(peer=["wants evidence", "no emoji"], changed=True,
                        fields=["user_preferences"])
        if "dialectic reviewer" in system
        else _base_reply(prompt)))

    provider = make_provider({"base_interval": 50, "dialectic_interval": 10})
    for turn in range(1, 4):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail evidence", "noted")
    assert provider.on_pre_compress([]) == ""

    assert provider._store.pending_jobs() == [], "the sync drain finished both layers"
    assert provider._store.layer_state("base", "user:default") is not None
    assert provider._store.layer_state("dialectic", "agent:default:minir") is not None


def test_session_reset_flushes_old_tail_and_opens_new_session(plugin_package, make_provider):
    provider = make_provider({"base_interval": 10, "dialectic_interval": 3, "session_mode": "coding"})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")
    provider._transcript.add_turn("session-test", "tail", "noted")
    provider.on_session_switch("session-next", parent_session_id="session-test", reset=True)
    # The plugin hook fires after the provider switch for the same boundary: still one job.
    plugin_package._on_session_reset(session_id="session-next", old_session_id="session-test")
    assert _jobs(provider) == [("base", 4), ("dialectic", 4)]
    state = plugin_package.turn_counter.TRACKER.snapshot("session-next")
    assert (state["mode"], state["layers"]["base"]["turns"]) == ("coding", 0)
    assert plugin_package.turn_counter.TRACKER.tick("session-test", 5, now=1.0) == []


def test_branch_inherits_parent_counters(make_provider):
    provider = make_provider({"base_interval": 3, "dialectic_interval": 3})
    for turn in range(1, 5):
        provider.on_turn_start(turn, "m")           # base job at turn 3
    provider.on_session_switch("branch-1", parent_session_id="session-test", reset=False)
    assert provider._session_id == "branch-1"
    for turn in (5, 6):
        provider.on_turn_start(turn, "m")
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"]) for job in jobs] == [
        ("base", 3), ("base", 6), ("dialectic", 6)]
    base_jobs = [job for job in jobs if job["layer"] == "base"]
    assert base_jobs[0]["to_ts"] == base_jobs[1]["from_ts"]  # the branch continues the window


def test_non_primary_context_does_not_count(make_provider):
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1}, agent_context="subagent")
    provider.on_turn_start(1, "subagent scratch work")
    assert provider._store.pending_jobs() == []


def test_layer_intervals_can_be_set_by_env(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_BASE_INTERVAL", "2")
    monkeypatch.setenv("MINIR_DIALECTIC_INTERVAL", "1")
    provider = make_provider({"base_interval": 99, "dialectic_interval": 99})
    for turn in (1, 2, 3, 4):
        provider.on_turn_start(turn, "m")
    assert _jobs(provider) == [("base", 2), ("dialectic", 3), ("base", 4), ("dialectic", 4)]


def test_layer_settings_come_from_config_yaml(plugin_package, monkeypatch, tmp_path):
    """``memory.minir`` in config.yaml drives the cadence — the block ``hermes memory setup``
    and the dashboard both persist (see the README's Configuration section).

    Stubs only ``hermes_cli.config`` (the module ``_load_config`` imports), so the provider's
    own read path — not just the ``__init__`` wiring — is under test.
    """
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"memory": {"minir": {
        "base_interval": 4, "dialectic_interval": 2, "session_mode": "from-config"}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    sys.modules["hermes_cli"].__path__ = []

    provider = plugin_package.MinirMemoryProvider()  # no explicit config: read config.yaml
    provider.initialize("session-test", hermes_home=str(tmp_path), platform="cli")
    try:
        tracker = plugin_package.turn_counter.TRACKER
        assert (tracker.base_interval, tracker.dialectic_interval) == (4, 2)
        assert tracker.snapshot("session-test")["mode"] == "from-config"
        for turn in range(1, 7):
            provider.on_turn_start(turn, "m")  # base interval 4, then the review every 2
        assert _jobs(provider) == [("base", 4), ("dialectic", 6)]
    finally:
        provider.shutdown()


def test_subagent_session_start_does_not_disturb_the_bound_session(plugin_package, make_provider):
    """Delegate children are not persist-disabled and carry their own session id, so
    ``on_session_start`` fires for them too — it must only record that id, never move the
    counters of the session this provider is bound to (``on_turn_start`` has no session_id)."""
    provider = make_provider({"base_interval": 2, "dialectic_interval": 2})
    plugin_package._on_session_start(session_id="child-abc", model="m", platform="cli")
    for turn in (1, 2):
        provider.on_turn_start(turn, "m")
    jobs = provider._store.pending_jobs()
    assert jobs and all(job["session_id"] == "session-test" for job in jobs)
    child = plugin_package.turn_counter.TRACKER.snapshot("child-abc")
    assert (child["layers"]["base"]["turns"], child["layers"]["dialectic"]["turns"]) == (0, 0)


def test_stats_report_layer_counters_and_jobs(provider):
    for turn in range(1, 25):
        provider.on_turn_start(turn, "m")
    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert (stats["jobs"], stats["pending_jobs"]) == (1, 1)
    assert stats["layers"]["layers"]["dialectic"]["turns"] == 24
    assert stats["layers"]["layers"]["base"]["interval"] == 24
    assert stats["reasoning_enabled"] is False  # the test bootstrap disables the model calls
    assert stats["artifacts"] == []


def test_a_boundary_drains_content_the_cadence_already_flushed(plugin_package, make_provider):
    """The live defect: a layer fires on a turn's tick, so its watermark moves *before* the
    turn's messages exist — and ``to_ts`` is then stamped behind them. Nothing would ever
    reason over that turn. The boundary drain asks the content watermark instead."""
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    # The tick jobs ran to completion before this turn's messages existed (an initialize-time
    # drain, say), which is what leaves the watermark behind the content.
    for job in provider._store.pending_jobs():
        provider._store.complete_job(job["id"], job["to_ts"])
    provider._transcript.add_turn("session-test", "deploy on Windows", "noted")
    provider.on_session_end([])
    jobs = provider._store.pending_jobs()
    assert [job["layer"] for job in jobs] == ["base", "dialectic"], \
        "both layers owe the turn the cadence counted before it existed"
    # The queued window starts at the content watermark and reaches forward, so the next drain
    # actually sees the turn the cadence had already counted.
    for job in provider._store.pending_jobs():
        covered = provider._transcript.window("session-test", job["from_ts"], time.time() + 1)
        assert [row["content"] for row in covered] == ["deploy on Windows", "noted"]


def test_a_boundary_is_quiet_while_a_queued_job_still_owns_the_content(plugin_package, make_provider):
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    provider._transcript.add_turn("session-test", "deploy on Windows", "noted")
    before = [(job["layer"], job["turns"]) for job in provider._store.pending_jobs()]
    provider.on_session_end([])
    # The two tick jobs are still queued; the boundary adds the dependent review of that base
    # window but must not add a second base job for the same turns.
    after = [(job["layer"], job["turns"]) for job in provider._store.pending_jobs()]
    assert [row for row in after if row[0] == "base"] == [row for row in before if row[0] == "base"]
    assert [row for row in after if row[0] == "dialectic"] == [row for row in before if row[0] == "dialectic"] + \
        [("dialectic", 1)]


def test_prefetch_ignores_the_dialectic_artifact(provider):
    provider._store.put_layer_state("dialectic", "agent:default:minir",
                                    "## Base Revision\n- old advice")
    _save_base_row(provider, ai_self="live ai", summary="### Objective\n- live goal")
    block = provider.prefetch("nothing matches this query")
    assert "live ai" in block and "live goal" in block
    assert "old advice" not in block


def test_an_oversized_message_cannot_empty_or_own_the_budget(plugin_package):
    """One long paste must neither consume the whole budget nor leave an empty bucket (which the
    oldest-session eviction would then index)."""
    buffer = plugin_package.transcript.TranscriptBuffer(max_chars=25)
    buffer.add("s", "user", "x" * 5000)
    assert buffer.messages("s") == []  # it could not fit the budget at all
    buffer.add("s", "user", "short")
    assert [row["content"] for row in buffer.messages("s")] == ["short"]

    async_buffer = plugin_package.transcript.TranscriptBuffer(max_chars=9000)
    async_buffer.add("s", "user", "y" * 5000)
    assert len(async_buffer.messages("s")[0]["content"]) == 4001  # capped, ellipsis included
    async_buffer.add("s", "user", "later")  # must not raise
    assert len(async_buffer.messages("s")) == 2


def test_eviction_skips_a_bucket_a_huge_message_emptied(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer(max_chars=10, max_sessions=1)
    buffer.add("emptied", "user", "z" * 100)   # bigger than the whole budget -> empty bucket
    buffer.add("fresh", "user", "ok")          # evicting the empty one must not raise
    assert buffer.messages("emptied") == []
    assert [row["content"] for row in buffer.messages("fresh")] == ["ok"]


def test_sync_turn_survives_a_transcript_failure(plugin_package, make_provider, monkeypatch):
    """The one hook the host calls after every turn must not be able to raise into its worker."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})

    def explode(*args, **kwargs):
        raise RuntimeError("transcript exploded")

    monkeypatch.setattr(provider._transcript, "add_turn", explode)
    provider.sync_turn("deploy the plugin on Windows", "noted")  # must not raise
    provider.on_turn_start(1, "m")  # and the provider stays usable


def test_sync_turn_records_the_author_from_the_tick(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.on_turn_start(1, "m", author_id="u-42", author_name="Ada")
    provider.sync_turn("deploy the plugin on Windows", "noted")
    rows = provider._transcript.messages("session-test")
    assert [(row["author"], row["author_id"]) for row in rows] == [("Ada", "u-42"), ("", "")]


def test_sync_turn_prefers_the_host_supplied_author(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", _no_model_call)
    provider = make_provider({})
    provider.on_turn_start(1, "m", author_id="stale", author_name="Stale")
    provider.sync_turn("deploy the plugin on Windows", "noted",
                       turn_author={"id": "u-7", "name": "Bo", "is_bot": False})
    assert provider._transcript.messages("session-test")[0]["author_id"] == "u-7"


def test_layer_reasoning_runs_end_to_end(plugin_package, make_provider, monkeypatch):
    """Reasoning enabled: a turn ends, both layers publish, and prefetch carries the two columns."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, system, prompt, **kw: (
        _dialectic_json(peer=["no emoji"], changed=True, fields=["user_preferences"])
        if "dialectic reviewer" in system else
        _base_reply(prompt)))
    monkeypatch.setattr(plugin_package.minir_agents, "run", lambda provider, args: json.dumps(
        {"answer": _base_reply(args["user_prompts"])}))

    provider = make_provider({"base_interval": 1, "dialectic_interval": 1}, agent_identity="Masía")
    provider.on_turn_start(1, "m")
    provider.sync_turn("deploy on Windows", "noted")
    provider._worker.stop()  # join the background drain before asserting

    assert provider._store.pending_jobs() == []
    assert [job["status"] for job in provider._store.claim_jobs(limit=9)] == []  # nothing left to do
    subjects = sorted({state["subject"] for state in provider._store.all_layer_states()})
    assert subjects == ["agent:default:Masía", "user:default"]
    block = provider.prefetch("q")
    assert "### AI self representation" in block and "### Window summary" in block
    assert "wants evidence" not in block, "cards other than the two columns are search-only"


def test_reasoning_disabled_never_calls_the_model(plugin_package, make_provider, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("reasoning is off: no model call may happen")

    monkeypatch.setattr(plugin_package.worker, "call_model", explode)
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    provider.on_turn_start(1, "m")
    provider.sync_turn("deploy on Windows", "noted")
    provider.on_session_end([])
    assert [job["layer"] for job in provider._store.pending_jobs()] == ["base"]  # never drained
    assert provider._store.all_layer_states() == []


def test_register_wires_the_provider_and_its_hooks(plugin_package):
    class Ctx:
        def __init__(self):
            self.provider = None
            self.hooks = {}

        def register_memory_provider(self, provider):
            self.provider = provider

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = Ctx()
    plugin_package.register(ctx)
    assert isinstance(ctx.provider, plugin_package.MinirMemoryProvider)
    assert set(ctx.hooks) == {"on_session_start", "on_session_reset", "on_session_finalize",
                              "pre_llm_call", "post_llm_call", "post_tool_call",
                              "pre_tool_call", "transform_tool_result", "pre_verify",
                              "transform_llm_output", "on_interim_message", "on_stream_delta",
                              "post_api_request", "api_request_error"}
    # A non-None result would be injected into the user message (pre_llm_call) or sent to
    # the chat (on_session_finalize), so those handlers must return None. The two transforms are
    # the hooks whose return value replaces text, so they are checked separately.
    for name, callback in ctx.hooks.items():
        if name in {"transform_llm_output", "transform_tool_result"}:
            continue
        assert callback(session_id="s", old_session_id="o", reason="new_session", turn_id="t",
                        model="m", platform="cli", tool_name="terminal", duration_ms=3,
                        provider=object()) is None
    assert ctx.hooks["transform_llm_output"](
        response_text="answer", session_id="s", provider=object()) is None
    # No rule matches this call: the replacement path must leave the result alone.
    quiet = types.SimpleNamespace(_store=object(), _session_id="s",
                                  _fork=types.SimpleNamespace(transform=lambda *a, **k: None))
    assert ctx.hooks["transform_tool_result"](
        tool_name="terminal", args={"command": "ls"}, result="x" * 9000, session_id="s",
        status="ok", provider=quiet) is None


def test_registered_middleware_accepts_the_host_provider_payload(plugin_package):
    """``llm_request`` carries the host's own provider; the callback must bind this plugin's
    provider instead of raising ``TypeError: got multiple values for keyword argument``."""
    class Ctx:
        def __init__(self):
            self.hooks = {}
            self.middleware = {}

        def register_memory_provider(self, provider):
            self.provider = provider

        def register_hook(self, name, callback):
            self.hooks[name] = callback

        def register_middleware(self, kind, callback):
            self.middleware[kind] = callback

        def register_auxiliary_task(self, *args, **kwargs):
            pass

        def register_tool(self, **kwargs):
            pass

        def register_command(self, *args, **kwargs):
            pass

    ctx = Ctx()
    plugin_package.register(ctx)
    host_provider = object()
    assert ctx.hooks["post_api_request"](session_id="s", provider=host_provider) is None
    assert ctx.middleware["llm_request"](
        request={}, session_id="s", provider=host_provider) is None


# -- cold start, review injection, metrics ------------------------------------

def test_cold_start_is_gated_to_the_primary_bound_session(
        plugin_package, make_provider, monkeypatch, tmp_path):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({"dialectic_depth": 1})
    provider._worker._enabled = False  # inspect the queue, do not race a background drain
    provider._worker.stop()            # join the initialize-time drain before asserting
    provider._identity = "I am Masía."  # the context that makes a cold start meaningful
    connection = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL);")
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("session-test", "user", "existing history", time.time()))
        connection.commit()
    finally:
        connection.close()

    plugin_package._on_session_start(session_id="child", provider=provider)
    assert provider._store.pending_jobs() == [], "a child session must not queue a model job"

    plugin_package._on_session_start(session_id="session-test", provider=provider)
    jobs = provider._store.pending_jobs()
    assert [(job["layer"], job["turns"], job["session_id"]) for job in jobs] == [
        ("base", 0, "session-test")]


def test_cold_start_rejects_a_non_primary_agent_context(plugin_package, make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({}, agent_context="subagent")
    provider._worker._enabled = False
    plugin_package._on_session_start(session_id="session-test", provider=provider)
    assert provider._store.pending_jobs() == []


def test_legacy_review_is_not_injected_at_the_boundary(plugin_package, make_provider):
    provider = make_provider({})
    provider._store.put_layer_state(
        "dialectic", "agent:default:minir",
        "## Adviser Review\n- ran the smoke test\n\n"
        "## Constraints\n- never edit outside the repo\n\n"
        "## Preferences\n- act first\n\n"
        "## Second Brain\n- [tool] pytest",
        session_id="session-test")
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-1", provider=provider) is None
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-2", provider=provider) is None
    assert plugin_package._on_pre_llm_call(
        session_id="other", turn_id="turn-3", provider=provider) is None
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-4", provider=provider,
        parent_session_id="parent") is None


def test_dialectic_advices_remain_candidates(plugin_package, provider):
    provider._store.add_advisories("session-test", [
        {"severity": "nit", "target": "general", "content": "polish the wording",
         "decision": "keep or trim", "evidence": ["m1"]},
        {"severity": "blocker", "target": "user_peer_card",
         "content": "verify the deploy target before deleting", "decision": "confirm the path",
         "evidence": ["m2"]},
    ])
    assert plugin_package._on_pre_llm_call(
        session_id="session-test", turn_id="turn-1", provider=provider) is None
    pending = provider._store.pending_advisories("session-test")
    assert len(pending) == 2, "dialectic advices are candidates, not boundary injections"
    assert all(row["source"] == "dialectic" for row in pending)


def test_post_tool_call_aggregates_metrics(plugin_package, make_provider):
    provider = make_provider({})
    for duration in (10, 30):
        plugin_package._on_post_tool_call(
            tool_name="terminal", session_id="session-test", duration_ms=duration, provider=provider)
    plugin_package._on_post_tool_call(
        tool_name="terminal", session_id="session-test", duration_ms=5, provider=provider,
        error=True)
    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert stats["tool_metrics"]["terminal"] == {
        "count": 3, "total_ms": 45.0, "max_ms": 30.0, "errors": 1}
    provider.flush_metrics()
    assert provider._store.stats()["metrics"] == 1


def test_layers_reason_from_host_history_and_publish_searchable_sections(
        plugin_package, make_provider, tmp_path, monkeypatch):
    """The restart-durable path: a job reads state.db, not the in-process buffer."""
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda task, system, prompt, **kw: (
        _dialectic_json(peer=["wants evidence", "no emoji"], changed=True,
                        fields=["user_preferences"])
        if "dialectic reviewer" in system else
        _base_reply(prompt)))
    monkeypatch.setattr(plugin_package.minir_agents, "run", lambda provider, args: json.dumps(
        {"answer": _base_reply(args["user_prompts"])}))
    provider = make_provider({"base_interval": 1, "dialectic_interval": 1})
    state_path = tmp_path / "state.db"
    connection = sqlite3.connect(str(state_path))
    try:
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, message_uid TEXT,"
            " role TEXT, content TEXT, timestamp REAL, active INTEGER DEFAULT 1,"
            " _compressed_summary INTEGER DEFAULT 0);")
        stamp = time.time()
        connection.executemany(
            "INSERT INTO messages (session_id, message_uid, role, content, timestamp)"
            " VALUES (?, ?, ?, ?, ?)",
            [("session-test", "u1", "user", "deploy the kubernetes cluster", stamp),
             ("session-test", "a1", "assistant", "running the deploy smoke test", stamp + 0.001),
             ("session-test", "u2", "user", "[Assistant Rules] not user intent", stamp + 0.002)])
        connection.commit()
    finally:
        connection.close()

    provider.on_turn_start(1, "m")
    provider.on_session_end([])  # drain the boundary without adding buffer content
    provider._worker.stop()

    assert provider._store.layer_state("base", "user:default") is not None
    assert provider._store.layer_state("dialectic", "agent:default:minir") is not None
    hits = provider._store.search("deploy on Windows", session_id="session-test")
    assert hits and any(row["kind"] == "window_summary" for row in hits)
    assert not any("not user intent" in row["text"] for row in hits)
    review = provider._store.layer_state("dialectic", "agent:default:minir")
    assert "user_preferences" in str((review or {}).get("content") or "")


def test_minir_get_reads_one_exact_key_and_reports_a_miss(provider):
    """A locator is exact: the ranked search must not be able to stand in for it."""
    store = provider._store
    store.save_conclusion("base.user_representation", "## Self\n- Engineer",
                          key="user:default", subject="user:default")
    store.save_conclusion("base.user_representation", "## Self\n- Someone else",
                          key="user:basic:zzh", subject="user:basic:zzh")

    found = json.loads(provider.handle_tool_call(
        "minir_get", {"kind": "base.user_representation", "key": "user:default"}))
    assert found["found"] is True and found["text"].startswith("## Self")
    assert "Someone else" not in found["text"]

    missing = json.loads(provider.handle_tool_call(
        "minir_get", {"kind": "base.user_representation", "key": "user:nobody"}))
    assert missing["found"] is False and missing["text"] == "" and missing["lines"] == []

    refused = json.loads(provider.handle_tool_call(
        "minir_get", {"kind": "base.user_representation", "key": "user:default",
                      "projection": "whole"}))
    assert "error" in refused


def test_minir_get_paths_projects_pages_without_their_bodies(provider):
    markers = {"value_markers": [
        {"path": "tools/KNOWLEDGE-a.md", "topic": "a", "content": "body one"},
        {"path": "projects/METHOD-b.md", "topic": "b", "content": "body two"}]}
    provider._store.save_conclusion(
        "base.value_markers", json.dumps(markers), key="session:s:user:default",
        subject="session:s:user:default")

    payload = json.loads(provider.handle_tool_call(
        "minir_get", {"kind": "base.value_markers", "key": "session:s:user:default",
                      "projection": "paths"}))
    assert payload["found"] is True and payload["text"] == ""
    assert payload["lines"][:2] == ["tools/KNOWLEDGE-a.md", "projects/METHOD-b.md"]
    assert all("body one" not in line for line in payload["lines"])


def test_minir_state_returns_the_current_artifact_only(provider):
    provider._store.put_layer_state("base", "user:default", "## User Card\n- one")
    payload = json.loads(provider.handle_tool_call(
        "minir_state", {"layer": "base", "subject": "user:default"}))
    assert payload["found"] is True and payload["version"] == 1
    assert payload["content"].startswith("## User Card")

    missing = json.loads(provider.handle_tool_call(
        "minir_state", {"layer": "base", "subject": "user:nobody"}))
    assert missing["found"] is False and missing["content"] == ""
    refused = json.loads(provider.handle_tool_call(
        "minir_state", {"layer": "everything", "subject": "user:default"}))
    assert "error" in refused


def test_minir_window_hides_drafts_unless_asked_and_pins_the_window(provider):
    store = provider._store
    session = provider._session_id
    store.add_base_refresh(
        session, "user:default", window_from_ts=0.0, window_to_ts=10.0,
        window_from_turn=1, window_to_turn=1,
        user_representation=json.dumps("- first"), user_peer_card=json.dumps(""),
        ai_self_representation=json.dumps(""), ai_identity_card=json.dumps(""),
        window_summary=json.dumps("### Objective\n- first"), value_markers="")
    store.save_base_draft(session, "user:default", window_from_ts=10.0, window_to_ts=20.0)
    store.save_base_segment(session, "user:default", window_from_ts=10.0,
                            columns={"window_summary": json.dumps("### Objective\n- half")})

    visible = json.loads(provider.handle_tool_call(
        "minir_window", {"session_id": session, "subject": "user:default",
                         "window_from_ts": 10.0}))
    assert visible["found"] is False, "a draft is not a published window"

    drafted = json.loads(provider.handle_tool_call(
        "minir_window", {"session_id": session, "subject": "user:default",
                         "window_from_ts": 10.0, "include_drafts": True,
                         "parts": ["window_summary"]}))
    assert drafted["found"] is True and drafted["rows"][0]["draft"] is True
    assert drafted["rows"][0]["parts"]["window_summary"] == "### Objective\n- half"

    latest = json.loads(provider.handle_tool_call(
        "minir_window", {"session_id": session, "subject": "user:default", "latest": True,
                         "format": "text"}))
    assert latest["rows"][0]["window_from_ts"] == 0.0
    assert latest["rows"][0]["text"].startswith("## User Card")

    drifted = json.loads(provider.handle_tool_call(
        "minir_window", {"session_id": session, "subject": "user:default",
                         "window_from_ts": 10.0, "window_to_ts": 99.0,
                         "include_drafts": True}))
    assert drifted["found"] is False, "a fixed window whose end moved is not the row asked for"

    refused = json.loads(provider.handle_tool_call(
        "minir_window", {"session_id": session, "parts": ["nope"]}))
    assert "error" in refused


def test_search_exposes_one_id_field_for_base_and_tool_rows(plugin_package, provider):
    provider._store.save_conclusion("base.user_representation", "deploy the release",
                                    key="user:default", subject="user:default")
    payload = json.loads(provider.handle_tool_call("minir_search", {"query": "deploy release"}))
    base_row = next(row for row in payload["results"] if row["kind"].startswith("base."))
    assert base_row["conclusions_id"] > 0
    assert "id" not in base_row and "note_id" not in base_row
    assert "conclusion_id" not in base_row

    row_id = provider._store.save_conclusion(
        "experiences", "a forgettable note", session_id=provider._session_id)
    found = json.loads(provider.handle_tool_call("minir_search", {"query": "forgettable"}))
    marker = next(row for row in found["results"] if row["kind"] == "experiences")
    assert marker["conclusions_id"] == row_id


def test_boundary_queues_a_state_db_tail_after_the_buffer_was_lost(
        plugin_package, make_provider, tmp_path):
    provider = make_provider({})
    provider._transcript.clear()
    connection = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL);")
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("session-test", "user", "history-only tail", time.time()))
        connection.commit()
    finally:
        connection.close()

    provider.on_session_end([])
    jobs = provider._store.pending_jobs()
    assert {(job["layer"], job["session_id"]) for job in jobs} == {
        ("base", "session-test"), ("dialectic", "session-test")}


def test_another_sessions_window_is_not_injected_and_search_still_sees_cards(provider):
    _save_base_row(provider, session="other-session", ai_self="other session stance",
                   summary="### Objective\n- old session secret", to_ts=9.0)
    assert provider.prefetch("durable") == "", "another session's window is not injectable"
    provider._store.save_conclusion("base.user_peer_card", "durable peer card secret",
                                    key="user:default", subject="user:default")
    block = provider.prefetch("durable peer card secret")
    assert "durable peer card secret" not in block, "only the live two columns are injected"
    payload = json.loads(provider.handle_tool_call(
        "minir_search", {"query": "durable peer card secret"}))
    kinds = {row["kind"] for row in payload["results"]}
    assert "base.user_peer_card" in kinds, "minir_search still exposes the durable cards"


def test_system_prompt_block_carries_only_memory_and_the_user_cards(make_provider):
    """Craft rules and the adviser contract used to ride the block; both were removed."""
    for mode in ({"enabled": False}, {"enabled": True, "mode": "watch"},
                 {"enabled": True, "mode": "adviser_only"}):
        block = make_provider({"adviser_mode": mode}).system_prompt_block()
        assert block.startswith("# Minir Memory")
        for absent in ("# Craft Rules", "# Advisers", "ADVICE SEVERITY", "ADVICE TARGETS",
                       "ADVICE HANDLING", "NIT REPORTING", "ADVICE LANGUAGE",
                       "[adviser-only mode", "## adviser_consult"):
            assert absent not in block, absent


def test_system_prompt_block_includes_the_global_user_preference(
        plugin_package, make_provider, tmp_path):
    provider = make_provider()
    assert "# User Preferences" not in provider.system_prompt_block()

    card = tmp_path / "minir-memory" / "user" / "preference.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nschema: minir-card/1\n---\n\n- 回复用中文\n- 先给结论\n",
                    encoding="utf-8")

    block = provider.system_prompt_block()
    assert "# User Preferences" in block
    assert "回复用中文" in block
    assert block.index("# Minir Memory") < block.index("# User Preferences")


def test_an_empty_identity_card_adds_no_block(plugin_package, make_provider):
    provider = make_provider({})
    home = Path(provider._hermes_home)
    path = plugin_package.card_review.card_paths(str(home), "default")[
        plugin_package.card_review.USER_PREFERENCE]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Style\n- terse\n", encoding="utf-8")

    block = provider._cards_prompt_block()

    assert "# User Preferences" in block and "# User Identity" not in block


def test_both_cards_empty_returns_empty_block(make_provider):
    provider = make_provider({})

    assert provider._cards_prompt_block() == ""


def test_system_prompt_injects_the_user_identity_card(plugin_package, make_provider, tmp_path):
    provider = make_provider()
    identity = tmp_path / "minir-memory" / "user" / "identity.md"
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text("---\nschema: minir-card/1\n---\n\n- 维护 minir-memory\n",
                        encoding="utf-8")
    block = provider.system_prompt_block()
    assert "# User Identity" in block and "维护 minir-memory" in block
    assert block.index("# Minir Memory") < block.index("# User Identity")
    identity.write_text("---\nschema: minir-card/1\n---\n", encoding="utf-8")
    assert "# User Identity" not in provider.system_prompt_block()


def test_an_oversized_user_preference_is_capped(plugin_package, make_provider, tmp_path):
    provider = make_provider()
    card = tmp_path / "minir-memory" / "user" / "preference.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("x" * (plugin_package._MAX_PREFERENCE_CHARS + 500), encoding="utf-8")

    block = provider.system_prompt_block()
    section = block.split("# User Preferences", 1)[1]
    assert section.count("x") == plugin_package._MAX_PREFERENCE_CHARS
    assert "truncated" in section


# -- conclusions only: type filter, todo surface, final-answer injection -------

def test_search_type_returns_only_that_kind(provider):
    provider._store.save_conclusion(
        "session_summary", "zebraword session fact", key="session_summary:1",
        session_id=provider._session_id)
    provider._store.save_conclusion(
        "dialectic.review", "zebraword review fact", key="review:1",
        session_id=provider._session_id)
    only = json.loads(provider.handle_tool_call(
        "minir_search", {"query": "zebraword", "type": "dialectic.review"}))
    assert [row["kind"] for row in only["results"]] == ["dialectic.review"]
    everything = json.loads(provider.handle_tool_call("minir_search", {"query": "zebraword"}))
    assert {row["kind"] for row in everything["results"]} == {"session_summary",
                                                              "dialectic.review"}


def test_inject_order_puts_known_types_first_then_first_queued(plugin_package, provider):
    session = provider._session_id
    provider.transform_llm_output_inject("entities", "- a marker")
    provider.transform_llm_output_inject("todo", "| t |")
    provider.transform_llm_output_inject("other", "- unknown")

    answer = plugin_package._on_transform_llm_output(
        response_text="the answer", session_id=session, provider=provider)

    assert answer.startswith("the answer\n\n")
    assert answer.index("| t |") < answer.index("- a marker") < answer.index("- unknown")
    assert provider._injects == {}, "the queue is cleared by the trigger"


def test_inject_trigger_clears_even_without_answer_text(plugin_package, provider):
    session = provider._session_id
    provider.transform_llm_output_inject("todo", "| t |")
    assert plugin_package._on_transform_llm_output(
        response_text="", session_id=session, provider=provider) is None
    assert provider._injects == {}, "an empty answer still consumes the queue"
    assert plugin_package._on_transform_llm_output(
        response_text="later answer", session_id=session, provider=provider) is None


def test_stateful_inject_keeps_only_the_last_block(plugin_package, provider):
    session = provider._session_id
    provider.transform_llm_output_inject("todo", "| old |")
    provider.transform_llm_output_inject("todo", "| new |")
    assert len(provider._injects[session]) == 1
    answer = plugin_package._on_transform_llm_output(
        response_text="the answer", session_id=session, provider=provider)
    assert "| new |" in answer and "| old |" not in answer


def test_inject_limits_refuse_instead_of_truncating(provider):
    session = provider._session_id
    for index in range(25):
        provider.transform_llm_output_inject("entity", f"- {index}")
    assert len(provider._injects[session]) == 20, "the per-session cap is 20 items"
    provider.transform_llm_output_inject("entity", "- overflow")
    assert len(provider._injects[session]) == 20
    assert all(item["content"] != "- overflow" for item in provider._injects[session])


def test_todo_validation_refuses_bad_lists_without_storing(provider):
    session = provider._session_id
    nested = json.loads(provider.handle_tool_call("minir_todo", {"todos": [
        {"id": "a", "content": "one"},
        {"id": "b", "content": "two", "parent": "a"},
        {"id": "c", "content": "three", "parent": "b"}]}))
    assert "error" in nested and "two levels" in nested["error"]
    orphan = json.loads(provider.handle_tool_call("minir_todo", {"todos": [
        {"id": "a", "content": "one", "parent": "ghost"}]}))
    assert "error" in orphan and "not in this list" in orphan["error"]
    duplicate = json.loads(provider.handle_tool_call("minir_todo", {"todos": [
        {"id": "a", "content": "one"}, {"id": "a", "content": "two"}]}))
    assert "error" in duplicate and "duplicate" in duplicate["error"]
    assert provider._store.get_conclusion("todo", f"todo:default:{session}") is None


def test_todo_keeps_one_row_per_owner_and_clears_it(provider):
    session = provider._session_id
    provider.handle_tool_call("minir_todo", {"todos": [{"id": "t1", "content": "first"}]})
    provider.handle_tool_call("minir_todo", {"todos": [
        {"id": "t2", "content": "second", "status": "completed"}]})
    row = provider._store.get_conclusion("todo", f"todo:default:{session}")
    assert json.loads(row["content"])["todos"] == [
        {"id": "t2", "content": "second", "status": "completed"}]
    assert provider._store.conclusions_by_kind(session) == {"todo": 1}

    cleared = json.loads(provider.handle_tool_call("minir_todo", {"todos": []}))
    assert cleared["deleted"] is True
    assert provider._store.get_conclusion("todo", f"todo:default:{session}") is None


def test_todo_isolates_a_dispatched_run_from_its_dispatcher(plugin_package, provider):
    """A child run's list is keyed by the agent's name and the run's own id.

    The provider serving the tool call belongs to the session that dispatched the run, so keying
    on its bound session alone would let the child overwrite the lead's list.
    """
    session = provider._session_id
    provider.handle_tool_call("minir_todo", {"todos": [{"id": "lead", "content": "lead step"}]})

    run = f"{session}:minir:explorer:minir-explorer-1-1"
    tokens = plugin_package.agent_history.layer_context("explorer", session, run)
    try:
        child = json.loads(provider.handle_tool_call("minir_todo", {"todos": [
            {"id": "c1", "content": "trace the call", "status": "in_progress"}]}))
        assert (child["name"], child["session_id"]) == ("explorer", run)
        assert run in provider._injects, "the table is queued for the run's own answer"
        block = provider.prefetch("nothing matches this")
        assert "trace the call" in block and "lead step" not in block
    finally:
        plugin_package.agent_history.reset_layer(tokens)

    main = json.loads(provider.handle_tool_call("minir_todo", {"todos": [
        {"id": "lead", "content": "lead step", "status": "completed"}]}))
    assert (main["name"], main["session_id"]) == ("default", session)
    block = provider.prefetch("nothing matches this")
    assert "lead step" in block and "trace the call" not in block
    lead_row = provider._store.get_conclusion("todo", f"todo:default:{session}")
    assert [item["id"] for item in json.loads(lead_row["content"])["todos"]] == ["lead"]
    child_row = provider._store.get_conclusion("todo", f"todo:explorer:{run}")
    assert [item["id"] for item in json.loads(child_row["content"])["todos"]] == ["c1"]


def test_todo_renders_in_prefetch_and_in_the_answer(plugin_package, provider):
    session = provider._session_id
    stored = json.loads(provider.handle_tool_call("minir_todo", {"todos": [
        {"id": "t1", "content": "跑全量测试", "status": "in_progress"},
        {"id": "t1a", "content": "先跑 unit", "status": "pending", "parent": "t1"},
        {"id": "t2", "content": "写迁移说明", "status": "completed"}]}))
    assert (stored["count"], stored["open"]) == (3, 2)

    block = provider.prefetch("nothing matches this")
    assert "[Todos] 2 open / 3 total" in block
    assert "  [~] t1 跑全量测试  (in_progress)" in block
    assert "    [ ] t1a 先跑 unit" in block
    assert "  [x] t2 写迁移说明" in block

    answer = plugin_package._on_transform_llm_output(
        response_text="done", session_id=session, provider=provider)
    assert answer.startswith("done\n\n| # | 任务 | 状态 | 父项 |")
    assert "| 1 | 跑全量测试 | in_progress |  |" in answer
    assert "| 1.1 | 先跑 unit | pending | t1 |" in answer
    assert "| 2 | 写迁移说明 | completed |  |" in answer
    assert answer.rstrip().endswith("Todo: 1/3 done, 1 in progress, 1 pending.")

def test_todo_accepts_blocked_as_a_separate_status(plugin_package, provider):
    session = provider._session_id
    stored = json.loads(provider.handle_tool_call("minir_todo", {"todos": [
        {"id": "t1", "content": "waiting on access", "status": "blocked"},
        {"id": "t2", "content": "done", "status": "completed"}]}))
    assert (stored["count"], stored["open"]) == (2, 1)

    block = provider.prefetch("nothing matches this")
    assert "[Todos] 1 open / 2 total" in block
    assert "  [!] t1 waiting on access  (blocked)" in block

    answer = plugin_package._on_transform_llm_output(
        response_text="done", session_id=session, provider=provider)
    assert "| 1 | waiting on access | blocked |  |" in answer
    assert answer.rstrip().endswith("Todo: 1/2 done, 0 in progress, 0 pending. 1 blocked.")

def test_remember_knowledge_queues_a_page_and_writes_no_conclusion(provider, plugin_package):
    before = provider._store.conclusions_by_kind(provider._session_id).get("tools", 0)
    payload = json.loads(provider.handle_tool_call("minir_remember", {
        "type": "tools", "content": "line one\nline two\nline three",
        "remember_mode": "knowledge", "category": "METHOD", "topic": "queue probe"}))
    assert payload["queued"] is True and payload["remember_mode"] == "knowledge"
    provider._knowledge.stop()
    root = plugin_package.knowledge.knowledge_dir(provider._hermes_home)
    assert (root / "tools" / "METHOD-queue-probe.md").is_file()
    assert provider._store.conclusions_by_kind(provider._session_id).get("tools", 0) == before


def test_remember_card_overlays_one_part_and_requires_a_reason(provider):
    payload = json.loads(provider.handle_tool_call("minir_remember", {
        "type": "user_preferences", "content": "## Preferences\n\n- be terse",
        "remember_mode": "card", "reason": "the operator asked for it"}))
    assert payload["stored"] is True and payload["revisions"]
    path = Path(provider._hermes_home) / "minir-memory" / "user" / "preference.md"
    assert "be terse" in path.read_text(encoding="utf-8")
    missing = json.loads(provider.handle_tool_call("minir_remember", {
        "type": "user_preferences", "content": "x", "remember_mode": "card"}))
    assert "reason" in missing.get("error", "")


def test_link_and_unmerge_tools_guard_their_endpoints(provider):
    provider._store.save_conclusion("projects", "project row", key="p1")
    provider._store.save_conclusion("experiences", "lesson row", key="e1")
    bad = json.loads(provider.handle_tool_call("minir_link", {
        "src": "projects:p1", "rel": "part_of", "dst": "tools:missing"}))
    assert bad["linked"] is False
    good = json.loads(provider.handle_tool_call("minir_link", {
        "src": "experiences:e1", "rel": "part_of", "dst": "projects:p1"}))
    assert good["linked"] is True
    assert provider._store.merge_into(("experiences", "e1"), ("projects", "p1"))
    undone = json.loads(provider.handle_tool_call("minir_unmerge", {
        "kind": "experiences", "key": "e1", "reason": "it was a test"}))
    assert undone["unmerged"] is True
