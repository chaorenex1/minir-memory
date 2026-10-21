"""Cross-module wiring: units -> knowledge, knowledge -> prefetch, runs -> transcripts."""

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

    def complete(task, system, prompt, **kwargs):
        if task == plugin_package.worker.DIALECTIC_LAYER_TASK:
            return "{}"
        if "baseline_user_card" in prompt:
            return json.dumps({
                "user_self": {"occupation": "Engineer", "skills": [], "thinking_models": [],
                              "interests": [], "communication": [], "values_motivation": []},
                "user_preferences": {field: [] for field in (
                    "communication", "presentation", "method", "decision", "technology",
                    "collaboration", "taboos")},
            })
        if "plugin_yaml" in prompt:
            return json.dumps({"ai_self": {**plugin_package.layers.empty_ai_self(),
                                          "state": "idle"},
                               "ai_identity": {**plugin_package.layers.empty_ai_identity(),
                                               "name": "minir", "version": "0.8.0"}})
        return json.dumps({"window_summary": plugin_package.window_summary.empty_summary(),
            "value_markers": {"units": [{
                "id": "u1", "org": "tools", "category": "KNOWLEDGE", "epistemic": "fact",
                "topic": "Docker restart policy", "content": "Use unless-stopped.",
                "time": "2026-10-21", "relations": [], "objects": ["tools:docker"]}]}})

    transcript.add("s1", "user", "restart docker", created_at=10.0)
    store.record_job("base", "s1", 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(
        store, transcript, complete=complete,
        on_units=lambda session, units: captured.append((session, units)))
    assert instance.run_once() == 1
    assert captured and captured[0][0] == "s1"
    assert captured[0][1][0]["topic"] == "Docker restart policy"


def test_prefetch_injects_only_a_knowledge_path_block(plugin_package, make_provider, tmp_path):
    provider = make_provider({})
    unit = {"id": "u1", "org": "tools", "category": "KNOWLEDGE", "epistemic": "fact",
            "topic": "Docker restart policy", "content": "Use unless-stopped.",
            "time": "2026-10-21", "relations": [], "objects": ["tools:docker", "tools:restart"]}
    plugin_package.knowledge.write_unit(
        unit, root=plugin_package.knowledge.knowledge_dir(provider._hermes_home))
    block = provider.prefetch("how do I set a docker restart policy")
    assert "[knowledge — retrieve on demand]" in block
    assert "tools · Docker restart policy" in block
    assert "Use unless-stopped." not in block, "paths, never file contents"


def test_an_aux_run_writes_its_own_transcript(plugin_package, store, transcript, tmp_path):
    instance = plugin_package.worker.LayerWorker(store, transcript)
    result = SimpleNamespace(answer="{}", messages=({"role": "user", "content": "hi"},))
    instance._record_aux("minir_base_layer", "system", result,
                        {"model": "m", "provider": "p"}, ["file", "adviser"])
    transcript.flush()
    deadline = time.time() + 2.0
    base = tmp_path / "sessions"
    while time.time() < deadline and not any(base.rglob("*.json")):
        time.sleep(0.02)
    files = list(base.rglob("*.json"))
    assert files, "the aux pass wrote no transcript"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["layer"] == "base" and payload["messages"][0]["content"] == "hi"


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
    files = list(sessions.glob("*.json"))
    assert files, "the host session was not persisted"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["layer"] == "host"
    assert any("deploy on Windows" in json.dumps(message) for message in payload["messages"])


def test_the_card_scheduler_anchors_at_install(make_provider, monkeypatch):
    monkeypatch.setenv("MINIR_REASONING_ENABLED", "true")
    provider = make_provider({})
    state = provider._store.layer_state("card_review", "user:default")
    assert state is not None and json.loads(state["content"])["attempts"] == 0
    provider.shutdown()
