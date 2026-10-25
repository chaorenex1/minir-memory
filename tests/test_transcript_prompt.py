"""Transcript digest: per-pass projections, hard budget, elisions and the coverage line."""

from __future__ import annotations

import pytest


@pytest.fixture()
def buffer(plugin_package, tmp_path):
    instance = plugin_package.transcript.TranscriptBuffer(
        persist_dir=tmp_path / "sessions", run_dir=tmp_path / "run", profile="default")
    yield instance
    instance.close()


def _fill(buffer):
    buffer.append_event("host", "s1", "system", text="be terse", profile="default")
    buffer.append_event("host", "s1", "user", text="deploy the new tag", profile="default")
    buffer.append_event("host", "s1", "assistant", text="reading the manifest",
                        tool_calls=[{"id": "c1", "name": "exec", "args": "{}"}], profile="default")
    buffer.append_event("host", "s1", "tool", text="", result="R" * 900, name="exec",
                        tool_call_id="c1", ok=True, duration_ms=1200, profile="default")
    buffer.append_event("host", "s1", "reasoning", text="T" * 500, profile="default")
    buffer.append_event("host", "s1", "user", text="now verify", profile="default")


def _render(buffer, plugin_package, **kwargs):
    return plugin_package.transcript_prompt.render_digest(
        buffer, "default", "host", "s1", **kwargs)


def test_a_digest_is_plain_text_with_marked_elisions(buffer, plugin_package):
    _fill(buffer)
    text = _render(buffer, plugin_package, roles=("system", "user", "assistant", "tool",
                                                  "reasoning"),
                   include_tool_results=True, include_reasoning=True, tool_result_chars=120)
    lines = text.splitlines()
    assert lines[0] == "[system] be terse"
    assert lines[1] == "[user t1] deploy the new tag"
    assert "calls=exec" in lines[2]
    assert lines[3].startswith("[tool t1 s1 exec ok 1.2s chars=900]")
    assert "elided, 900 chars" in lines[3]
    assert lines[4].startswith("[reasoning t1]") and "elided, 500 chars" in lines[4]
    assert lines[-1].startswith("[coverage] seq ")
    assert "event(s) shown" in lines[-1]


def test_tool_results_and_reasoning_are_opt_in(buffer, plugin_package):
    _fill(buffer)
    text = _render(buffer, plugin_package)  # card-shaped projection
    assert "R" * 20 not in text
    assert "reasoning" not in text
    assert "calls=exec" in text  # the tool name survives; the payload does not


def test_the_character_budget_drops_whole_events_oldest_first(buffer, plugin_package):
    for index in range(30):
        buffer.append_event("host", "s1", "user", text=f"{index:02d} " + "x" * 40,
                            profile="default")
    text = _render(buffer, plugin_package, max_chars=200, max_events=50)
    lines = text.splitlines()
    body = lines[:-1]
    assert body and all(len(line) <= 200 for line in body)
    assert body[-1].startswith("[user") and "29" in body[-1]
    assert lines[-1].startswith("[coverage]")
    assert "earlier event(s) elided" in lines[-1]


def test_an_empty_projection_returns_nothing(buffer, plugin_package):
    assert _render(buffer, plugin_package) == ""
    block = plugin_package.transcript_prompt.digest_block(
        "base_user", buffer, "default", "host", "missing")
    assert block == ""


def test_each_pass_has_its_own_projection_and_block_name(plugin_package):
    prompt = plugin_package.transcript_prompt
    assert prompt.PRESETS["extraction"]["roles"] == ("user",)
    assert prompt.PRESETS["base_user"]["include_tool_results"] is False
    assert prompt.PRESETS["dialectic"]["include_tool_results"] is True
    assert prompt.PRESETS["adviser"]["include_reasoning"] is True
    assert len(set(prompt.BLOCK_NAMES.values())) == len(prompt.BLOCK_NAMES)
    assert prompt.DIGEST_MAX_CHARS == 4000


def test_the_window_uses_the_message_timestamp_when_the_host_supplies_one(buffer, plugin_package):
    buffer.ingest("host", "s3", [{"role": "user", "content": "old question", "timestamp": 100.0},
                                 {"role": "assistant", "content": "old answer",
                                  "timestamp": 101.0},
                                 {"role": "user", "content": "new question", "timestamp": 300.0}])
    rows = buffer.read_events("default", "host", "s3", limit=10, kinds=("msg",))["rows"]
    assert [row["ts"] for row in rows] == [100.0, 101.0, 300.0]
    in_window = plugin_package.transcript_prompt.render_digest(
        buffer, "default", "host", "s3", since=90.0, until=150.0)
    assert "old question" in in_window and "new question" not in in_window


def test_the_prompt_assets_declare_the_blocks_the_builders_emit(plugin_package):
    from pathlib import Path

    base = Path(plugin_package.__file__).parent / "agents"
    expected = {
        "b1_user_card.md": "<base_user_transcript>",
        "b2_ai_card.md": "<base_ai_transcript>",
        "b3_window_summary.md": "<window_summary_transcript>",
        "b4_value_markers.md": "<value_markers_transcript>",
        "dialectic.md": "<dialectic_transcript>",
    }
    for name, block in expected.items():
        text = (base / name).read_text(encoding="utf-8")
        assert block in text, name
        if name != "b4_value_markers.md":
            assert "coverage" in text, name
    assert "<session_transcript>" in (base / "knowledge_render.md").read_text(encoding="utf-8")
    reviewer = plugin_package.adviser_mode.review_prompt()
    assert "minir_hermes_session_search" in reviewer
    assert "adviser_consult" not in reviewer
