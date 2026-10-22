"""Session end: the minir toolset, per-turn token totals, the idle timer, summary + extraction."""

from __future__ import annotations

import json
import time

import pytest

SUMMARY = "\n".join([
    "## Session Goal", "- ship the plugin",
    "## Work Done", "- wired the toolset",
    "## Decisions", "- plugin tools are explicit",
    "## Durable Knowledge", "- FTS5 trigram handles CJK",
    "## Open Threads", "- none",
])


def _page():
    return {"category": "KNOWLEDGE", "org": "tools", "topic": "SQLite FTS5 trigram",
            "content": "Trigram tokenization makes FTS5 recall CJK substrings.",
            "epistemic": "fact", "objects": ["SQLite"], "relations": [],
            "summary": "use the trigram tokenizer for CJK recall"}


class _Ctx:
    """Plugin context stub: only the tool-registration surface."""

    def __init__(self):
        self.tools: dict[str, tuple[str, dict, object]] = {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = (toolset, schema, handler)


def test_the_minir_toolset_registers_every_tool(make_provider, plugin_package):
    provider = make_provider()
    ctx = _Ctx()

    registered = plugin_package.plugin_tools.register(ctx, provider)

    assert registered == list(plugin_package.plugin_tools.TOOL_NAMES)
    assert {toolset for toolset, _schema, _handler in ctx.tools.values()} == {"minir"}
    assert ctx.tools["minir_remember"][1]["parameters"]["required"] == ["type", "content"]
    result = json.loads(ctx.tools["minir_remember"][2](
        {"type": "tools", "content": "gopass backs secrets"}))
    assert result["stored"] is True
    stats = json.loads(ctx.tools["minir_stats"][2]({}))
    assert stats["conclusions_by_kind"] == {"tools": 1}
    assert "session_usage" in stats and "turn_usage" in stats


def test_an_uninitialized_provider_is_reported_not_raised(plugin_package):
    ctx = _Ctx()

    plugin_package.plugin_tools.register(ctx, None)

    payload = json.loads(ctx.tools["minir_search"][2]({"query": "anything"}))
    assert "not initialized" in payload["error"]


def test_the_provider_and_the_toolset_share_one_schema_source(provider, plugin_package):
    """The provider surface is the five store tools; the layer-scoped search is agent-only."""
    assert [schema["name"] for schema in provider.get_tool_schemas()] == \
        [schema["name"] for schema in plugin_package.plugin_tools.SCHEMAS]
    assert provider.get_tool_schemas()[0] is plugin_package.plugin_tools.SCHEMAS[0]
    assert "minir_aiagent_search" not in [schema["name"]
                                          for schema in provider.get_tool_schemas()]


def test_agent_toolsets_always_carry_the_plugin_surfaces(plugin_package):
    expected = ["file", "minir", "skills"]
    assert plugin_package.plugin_tools.agent_toolsets(["file"]) == expected
    assert plugin_package.plugin_tools.agent_toolsets(None) == expected
    assert plugin_package.plugin_tools.agent_toolsets("web") == ["web", *expected]


def test_session_end_finalises_each_turns_token_usage(make_provider):
    provider = make_provider()
    provider.record_api_usage(
        "session-test", "t1",
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
         "cache_read_tokens": 30, "reasoning_tokens": 5}, api_calls=2, model="m")
    provider.record_api_usage(
        "session-test", "t1",
        {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}, api_calls=1)
    provider.record_api_usage(
        "session-test", "t2",
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, api_calls=1)
    provider.record_api_usage("other-session", "t9", {"prompt_tokens": 999}, api_calls=1)

    provider.on_session_end([])

    usage = provider._store.session_usage("session-test")
    assert usage["turns"] == 2
    assert usage["api_calls"] == 4
    assert usage["prompt_tokens"] == 160
    assert usage["completion_tokens"] == 35
    assert usage["total_tokens"] == 195
    rows = {row["turn_id"]: row for row in provider._store.turn_usage("session-test")}
    assert rows["t1"]["api_calls"] == 3 and rows["t1"]["prompt_tokens"] == 150
    assert rows["t1"]["final_at"] and rows["t2"]["final_at"]
    assert provider._store.turn_usage("other-session") == []

    # A resumed turn is open again: new usage clears the previous final stamp, and the next
    # boundary closes it with the new total.
    provider.record_api_usage(
        "session-test", "t1", {"prompt_tokens": 1, "completion_tokens": 1}, api_calls=1)
    rows = {row["turn_id"]: row for row in provider._store.turn_usage("session-test")}
    assert rows["t1"]["final_at"] is None
    provider.on_session_end([])
    assert provider._store.session_usage("session-test")["total_tokens"] == 197


def test_session_end_arms_the_idle_timer_with_the_sessions_markers(make_provider):
    provider = make_provider({"knowledge_idle_hours": 24})
    provider.handle_tool_call("minir_remember", {
        "type": "tools", "category": "KNOWLEDGE", "content": "gopass backs the secrets store"})
    provider._reasoning = True  # enable the timer path without starting the layer worker

    provider.on_session_end([])

    row = provider._store.extraction("session-test")
    assert row is not None and row["status"] == "pending"
    assert row["due_at"] >= time.time() + 23 * 3600
    markers = json.loads(row["markers"])
    assert any("gopass" in note["content"] for note in markers["notes"])


def test_a_session_without_markers_arms_no_timer(make_provider):
    provider = make_provider({"knowledge_idle_hours": 24})
    provider._reasoning = True

    provider.on_session_end([])

    assert provider._store.extraction("session-test") is None


def test_the_marker_snapshot_is_shrunk_without_breaking_json(plugin_package):
    snapshot = {
        "notes": [{"content": "n" * 400} for _ in range(50)],
        "value_markers": [{"content": "u" * 160} for _ in range(16)],
        "summary": "s" * 4_000,
        "turn_usage": [{"turn_id": str(index)} for index in range(50)],
        "window_from_ts": 0.0, "window_to_ts": 0.0,
    }

    payload = plugin_package.MinirMemoryProvider._encode_marker_snapshot(snapshot)

    assert len(payload) <= plugin_package._MAX_MARKER_SNAPSHOT
    assert isinstance(json.loads(payload), dict)


def test_a_new_user_turn_cancels_the_idle_timer(make_provider):
    provider = make_provider({"knowledge_idle_hours": 24})
    provider.handle_tool_call("minir_remember", {
        "type": "tools", "category": "KNOWLEDGE", "content": "the timer needs a marker"})
    provider._reasoning = True
    provider.on_session_end([])
    assert provider._store.extraction("session-test")["status"] == "pending"

    provider.on_turn_start(1, "I am back")

    assert provider._store.extraction("session-test") is None


def test_the_scheduler_runs_a_due_session(provider, plugin_package, monkeypatch, tmp_path):
    provider._store.schedule_extraction(
        "s-due", due_at=0, markers=json.dumps({"notes": [], "value_markers": []}), window_to_ts=1.0)
    provider._store.add_base_refresh(
        "s-due", "user:default", window_from_ts=0.0, window_to_ts=1.0,
        window_from_turn=0, window_to_turn=1, user_representation="",
        user_peer_card="", ai_self_representation="", ai_identity_card="",
        window_summary="## Window Summary\n- did things",
        value_markers=json.dumps({"value_markers": []}))
    seen: dict = {}

    monkeypatch.setattr(plugin_package.handoff, "summarize_agent",
                        lambda *args, **kwargs: "HANDOFF BODY")

    def fake_summary(**kwargs):
        seen.update(kwargs)
        return SUMMARY

    monkeypatch.setattr(plugin_package.session_summary, "summarize_agent", fake_summary)
    monkeypatch.setattr(plugin_package.extraction, "extract_pages",
                        lambda **kwargs: [_page()])

    scheduler = plugin_package.extraction.ExtractionScheduler(
        provider._store, provider._run_session_extraction)
    assert scheduler.tick(now=time.time()) == 1

    row = provider._store.extraction("s-due")
    assert row["status"] == "done" and row["pages"] == 1 and row["summary_chars"] > 0
    assert seen["handoff"] == "HANDOFF BODY"
    assert seen["session_id"] == "s-due"
    conclusion = provider._store.newest_conclusion("session_summary", session_id="s-due")
    assert "## Session Goal" in conclusion["content"]
    files = list((tmp_path / "minir-memory" / "knowledge").rglob("*.md"))
    assert files and "trigram" in files[0].read_text(encoding="utf-8")


def test_a_failed_extraction_retries_once_then_parks(provider, plugin_package):
    provider._store.schedule_extraction("s-fail", due_at=0)
    scheduler = plugin_package.extraction.ExtractionScheduler(
        provider._store, lambda row: (_ for _ in ()).throw(RuntimeError("model down")))

    scheduler.tick(now=time.time())
    row = provider._store.extraction("s-fail")
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["due_at"] > time.time()

    scheduler.tick(now=row["due_at"] + 1)
    row = provider._store.extraction("s-fail")
    assert row["status"] == "failed" and row["attempts"] == 2
    assert "model down" in row["error"]


def test_the_page_pass_reads_the_marker_rules(plugin_package, monkeypatch, tmp_path):
    """The page pass classifies its own pages, so the marker rules are in its system prompt.

    The profile override is the text under test: it proves the rules are read at assembly time
    and that ``hermes_home`` reaches the loader, not only the packaged copy.
    """
    monkeypatch.delenv(plugin_package.rules.OVERRIDE_ENV, raising=False)
    override = tmp_path / "minir-memory" / "rules"
    override.mkdir(parents=True)
    (override / "markers-org.md").write_text(
        "## CONTRACT\n\nOVERRIDE MARKER ORG\n", encoding="utf-8")
    seen: dict = {}
    route_keys: list[str] = []

    class _Run:
        answer = ""

    def fake_run(message, **kwargs):
        seen["system"] = kwargs.get("system_message")
        return _Run()

    def fake_config(task_key=plugin_package.adviser.TASK_KEY):
        route_keys.append(task_key)
        return {"model": "", "provider": "", "toolsets": []}

    monkeypatch.setattr(plugin_package.agent_loop, "run", fake_run)
    monkeypatch.setattr(plugin_package.adviser, "load_config", fake_config)
    plugin_package.extraction.extract_pages(
        session_id="s1", summary=SUMMARY, handoff="HANDOFF BODY", hermes_home=str(tmp_path))

    system = seen["system"]
    assert route_keys == [plugin_package.adviser.KNOWLEDGE_TASK], \
        "the page pass rides its own route, not the adviser chain"
    assert "OVERRIDE MARKER ORG" in system, "the profile override must reach the page pass"
    assert "GOVERNANCE: what binds a subject." in system
    assert system.rstrip().endswith("no prose."), "the output contract closes the system prompt"


def test_the_summary_agent_receives_the_session_handoff(plugin_package, monkeypatch):
    seen: dict = {}

    class _Run:
        answer = SUMMARY

    def fake_run(message, **kwargs):
        seen["message"] = message
        seen["toolsets"] = kwargs.get("toolsets")
        seen["system"] = kwargs.get("system_message")
        return _Run()

    monkeypatch.setattr(plugin_package.agent_loop, "run", fake_run)

    text = plugin_package.session_summary.summarize_agent(
        session_id="s1", handoff="HANDOFF BODY", markers={"notes": []},
        usage={"total_tokens": 5}, window_end=123.0)

    assert text == SUMMARY
    assert "HANDOFF BODY" in seen["message"]
    assert "<session_handoff>" in seen["message"]
    assert {"minir"} <= set(seen["toolsets"])
    assert "## Durable Knowledge" in seen["system"]


def test_the_summary_agent_rejects_a_wrong_shape(plugin_package):
    assert plugin_package.session_summary.normalize("## Session Goal\n- only one") == ""
    assert plugin_package.session_summary.normalize(
        "```\n" + SUMMARY + "\n```").startswith("## Session Goal")


def test_the_hand_off_agent_also_gets_the_plugin_toolset(plugin_package, monkeypatch):
    seen: dict = {}

    class _Run:
        answer = ""

    def fake_run(message, **kwargs):
        seen["toolsets"] = kwargs.get("toolsets")
        return _Run()

    monkeypatch.setattr(plugin_package.agent_loop, "run", fake_run)
    monkeypatch.setattr(plugin_package.handoff, "load_template", lambda: "# Task Handoff")

    plugin_package.handoff.summarize_agent("chunk", "review", session_id="s1")

    assert {"minir"} <= set(seen["toolsets"])
