"""Cross-module wiring: value_markers -> knowledge, knowledge -> prefetch, runs -> transcripts."""

from __future__ import annotations

import json
import pathlib
import time
from types import SimpleNamespace

import pytest


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


@pytest.fixture()
def transcript(plugin_package, tmp_path):
    return plugin_package.transcript.TranscriptBuffer(persist_dir=tmp_path / "sessions")


def test_a_base_publish_hands_its_units_to_the_knowledge_pipeline(plugin_package, store, transcript):
    captured: list[tuple[str, list[dict]]] = []

    marker = {
        "id": "u1", "category": "KNOWLEDGE", "org": "tools",
        "topic": "Docker restart policy",
        "content": "restart policy unless-stopped\nsurvives a daemon restart\nverified on the host",
        "summary": "Docker restart policy for deploys", "epistemic": "fact",
        "relations": [], "objects": ["tools:docker"],
        "anchor": {"from_ts": 0.0, "to_ts": 20.0, "message_ids": []}}

    def complete(task, system, prompt, **kwargs):
        if "dialectic reviewer" in system:
            return "{}"
        if "<window_template>" in prompt:
            return json.dumps({
                "user_self_representation": "# Who this person is\n\nAn Engineer.\n",
                "user_preferences": "# How they want answers\n\n- Terse first.\n",
                "window_summary": "## Window Summary\n\n### Objective\n- restart docker\n\n"
                                  "### Current State\n#### Done\n- read the restart policy",
                "value_markers": [marker]})
        return json.dumps({"ai_self_representation": "# What this task is\n\nidle.\n",
                           "ai_identity": "# Who the assistant is\n\nminir 0.8.0.\n"})

    transcript.add("s1", "user", "restart docker", created_at=10.0)
    store.record_job("base", "s1", 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(
        store, transcript, complete=complete,
        on_value_markers=lambda session, value_markers, **kwargs: captured.append(
            (session, value_markers)))
    assert instance.run_once() == 1
    assert captured and captured[0][0] == "s1"
    assert captured[0][1][0]["topic"] == "Docker restart policy"


def test_prefetch_never_injects_knowledge_paths_or_recall(plugin_package, make_provider):
    provider = make_provider({})
    unit = {"id": "u1", "org": "tools", "category": "KNOWLEDGE", "epistemic": "fact",
            "topic": "Docker restart policy", "content": "Use unless-stopped.",
            "time": "2026-10-21", "relations": [], "objects": ["tools:docker", "tools:restart"]}
    plugin_package.knowledge.write_unit(
        unit, root=plugin_package.knowledge.knowledge_dir(provider._hermes_home))
    provider._store.save_conclusion(
        "tools", "docker restart policy note", session_id=provider._session_id)
    block = provider.prefetch("how do I set a docker restart policy")
    assert block == "", "neither knowledge paths nor explicit recall ride the turn"
    assert provider.handle_tool_call("minir_kb_search", {"query": "Docker restart policy"})


def test_an_aux_run_writes_its_own_transcript(plugin_package, store, transcript, tmp_path):
    instance = plugin_package.worker.LayerWorker(store, transcript)
    result = SimpleNamespace(answer="{}", messages=({"role": "user", "content": "hi"},))
    instance._record_aux(plugin_package.worker.BASE_LAYER, "system", result,
                        {"model": "m", "provider": "p"}, ["file", "adviser"])
    transcript.flush()
    deadline = time.time() + 2.0
    base = tmp_path / "sessions"
    while time.time() < deadline and not any(base.rglob("*.jsonl")):
        time.sleep(0.02)
    files = list(base.rglob("*.jsonl"))
    assert files, "the aux pass wrote no transcript"
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    messages = [row for row in rows
                if row["kind"] == "msg" and row["role"] != "system"]
    assert rows[0]["layer"] == "base" and messages[0]["text"] == "hi"


def test_a_primary_turn_persists_the_host_transcript(plugin_package, make_provider,
                                                    monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model",
                        lambda *args, **kwargs: "")
    provider = make_provider({})
    provider._worker.stop()            # join the cold-start drain first
    provider._worker._enabled = False  # the transcript is independent of the drain
    provider.on_turn_start("1", "m")
    provider.sync_turn("deploy on Windows", "noted")
    provider._transcript.flush()
    sessions = pathlib.Path(provider._hermes_home) / "minir-memory" / "sessions"
    files = list(sessions.glob("default/host/*.jsonl"))
    assert files, "the host session was not persisted"
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert rows[0]["layer"] == "host"
    assert any("deploy on Windows" in json.dumps(row) for row in rows)


def test_a_primary_turn_and_the_plugin_block_cover_the_five_roles(plugin_package, make_provider,
                                                                  monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    monkeypatch.setattr(plugin_package.worker, "call_model", lambda *args, **kwargs: "")
    provider = make_provider({})
    provider._worker.stop()
    provider._worker._enabled = False
    provider.sync_turn("deploy the tag", "done", messages=[
        {"role": "user", "content": "deploy the tag"},
        {"role": "assistant", "content": "checking",
         "tool_calls": [{"id": "c1", "function": {"name": "exec", "arguments": "{}"}}],
         "reasoning_content": "read the manifest first"},
        {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "R" * 6000},
    ])
    provider._transcript.flush()
    rows = provider._transcript.read_events("default", "host", "session-test", limit=100,
                                            kinds=("msg", "gap"))["rows"]
    roles = {row["role"] for row in rows}
    assert {"system", "user", "assistant", "tool", "reasoning"} <= roles
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    tool_row = next(row for row in rows if row["role"] == "tool")
    assert len(tool_row["result"]) == 6000


def test_the_card_scheduler_anchors_at_install(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({})
    state = provider._store.layer_state("card_review", "user:default")
    assert state is not None and json.loads(state["content"])["attempts"] == 0
    provider.shutdown()


def test_the_unified_scheduler_owns_every_recurring_task(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({})

    tasks = provider._scheduler.snapshot()
    assert {"layers", "card_review", "session_extraction"} <= set(tasks)
    assert tasks["layers"]["kind"] == "turn"
    assert tasks["card_review"]["kind"] == "time"
    assert tasks["session_extraction"]["kind"] == "time"
    assert provider._scheduler.start() is False, "the initialise path already started the clock"

    provider.on_turn_start(1, "a real turn")
    assert provider._scheduler.snapshot()["layers"]["runs"] >= 1

    payload = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert set(payload["scheduler"]) == set(tasks)
