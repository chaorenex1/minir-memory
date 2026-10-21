"""Knowledge production pipeline (P6): naming, idempotency, budget, queue and path selection."""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def knowledge(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.knowledge")


def _unit(**over):
    unit = {
        "id": "k1", "org": "tools", "category": "METHOD", "topic": "Docker 镜像清理",
        "epistemic": "",
        "content": "Run docker system prune to remove stopped containers and dangling images.",
        "time": "2026-10-21", "relations": [], "objects": ["tools:Docker"],
    }
    unit.update(over)
    return unit


def _seed(knowledge, root, count=8):
    for index in range(count):
        knowledge.write_unit(_unit(id=f"k{index}", topic=f"Docker cleanup {index}",
                                   summary="docker image cleanup",
                                   content=f"docker cleanup note {index}"), root=root)


def test_filename_is_stable_normalized_and_bounded(knowledge):
    assert knowledge.render_filename("METHOD", "Docker 镜像清理: <prune>?") == \
        "METHOD-Docker-镜像清理-prune.md"
    assert knowledge.slugify("  spaced  topic ") == "spaced-topic"
    assert len(knowledge.slugify("长" * 200)) == knowledge.SLUG_MAX
    assert knowledge.slugify("CON") == "_CON"
    assert "/" not in knowledge.slugify("../../etc/passwd")
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.render_filename("NOPE", "topic")


def test_same_identity_is_one_file_and_front_matter_counts_revisions(knowledge, tmp_path):
    first = knowledge.write_unit(_unit(), root=tmp_path)
    assert first["status"] == "created" and first["revisions"] == 1

    updated = _unit(content="Run docker system prune -af instead.", time="2026-10-22")
    second = knowledge.write_unit(updated, root=tmp_path)
    assert second["status"] == "updated" and second["revisions"] == 2
    assert second["path"] == first["path"]
    assert len(list((tmp_path / "tools").glob("METHOD-*.md"))) == 1

    meta = knowledge.read_front_matter(second["path"])
    for key in ("schema", "category", "org", "topic", "epistemic", "time", "revisions",
                "updated_at"):
        assert key in meta, f"missing front-matter key {key}"
    assert meta["schema"] == knowledge.SCHEMA
    assert meta["category"] == "METHOD" and meta["org"] == "tools"
    assert meta["revisions"] == 2 and meta["time"] == "2026-10-22"
    assert meta["epistemic"] == ""

    text = second["path"].read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "# Docker 镜像清理" in text and "docker system prune -af" in text


def test_rewriting_unchanged_content_does_not_bump_revisions(knowledge, tmp_path):
    knowledge.write_unit(_unit(), root=tmp_path)
    again = knowledge.write_unit(_unit(), root=tmp_path)
    assert again["status"] == "unchanged" and again["revisions"] == 1


def test_different_topics_produce_two_files(knowledge, tmp_path):
    knowledge.write_unit(_unit(topic="Docker 镜像清理"), root=tmp_path)
    knowledge.write_unit(_unit(id="k2", org="experiences", topic="Postgres 备份"), root=tmp_path)
    assert len(list((tmp_path / "tools").glob("*.md"))) == 1
    assert len(list((tmp_path / "experiences").glob("*.md"))) == 1


def test_slug_collisions_keep_both_topics_in_separate_files(knowledge, tmp_path):
    knowledge.write_unit(_unit(topic="a/b"), root=tmp_path)
    knowledge.write_unit(_unit(id="k2", topic="a:b"), root=tmp_path)
    pages = sorted((tmp_path / "tools").glob("METHOD-*.md"))
    assert len(pages) == 2
    topics = sorted(knowledge.read_front_matter(page)["topic"] for page in pages)
    assert topics == ["a/b", "a:b"]


def test_stale_evidence_is_skipped_and_the_page_kept(knowledge, tmp_path):
    fresh = knowledge.write_unit(_unit(content="fresh fact", time="2026-10-20"), root=tmp_path)
    stale = knowledge.write_unit(_unit(content="contradicting older fact", time="2026-10-01"),
                                 root=tmp_path)
    assert stale["status"] == "stale"
    assert stale["path"] == fresh["path"]
    text = fresh["path"].read_text(encoding="utf-8")
    assert "fresh fact" in text and "contradicting" not in text
    assert knowledge.read_front_matter(fresh["path"])["revisions"] == 1


def test_over_budget_write_fails_keeps_the_page_and_records_the_unit(knowledge, tmp_path):
    knowledge.write_unit(_unit(content="the original short body"), root=tmp_path)
    huge = _unit(content="word " * 400, time="2026-10-22")
    result = knowledge.write_unit(huge, budget=50, root=tmp_path)

    assert result["status"] == "failed"
    assert "budget" in result["error"]
    page = tmp_path / "tools" / "METHOD-Docker-镜像清理.md"
    assert "the original short body" in page.read_text(encoding="utf-8")
    assert knowledge.read_front_matter(page)["revisions"] == 1

    failure = tmp_path / "_failed" / "METHOD-Docker-镜像清理.json"
    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert payload["error"] and payload["unit"]["content"] == huge["content"]


def test_over_budget_retry_drops_the_oldest_evidence_lines(knowledge, tmp_path):
    body = "\n".join([f"- evidence line {index} " + "x" * 120 for index in range(8)])
    unit = _unit(content=body)
    result = knowledge.write_unit(unit, budget=200, root=tmp_path)

    assert result["status"] == "created", result["error"]
    text = result["path"].read_text(encoding="utf-8")
    assert "\n- evidence line 0" not in text and "\n- evidence line 7" in text


def test_invalid_unit_fails_with_a_unit_record(knowledge, tmp_path):
    result = knowledge.write_unit(_unit(category="NOPE"), root=tmp_path)
    assert result["status"] == "failed"
    assert (tmp_path / "_failed" / "UNIT-Docker-镜像清理.json").exists()


def test_render_callable_supplies_the_body_and_language(knowledge, tmp_path):
    calls = []

    def render(unit, language):
        calls.append(language)
        return f"Rendered in {language}: {unit['content']}"

    result = knowledge.write_unit(_unit(), language="zh", render=render, root=tmp_path)
    assert result["status"] == "created"
    assert calls == ["zh"]
    assert "Rendered in zh:" in result["path"].read_text(encoding="utf-8")


def test_queue_drops_the_oldest_and_counts_it(knowledge, tmp_path):
    pipeline = knowledge.KnowledgePipeline(str(tmp_path), autostart=False, queue_size=2)
    try:
        units = [_unit(id=f"k{index}", topic=f"topic {index}") for index in range(5)]
        assert pipeline.enqueue_from_refresh("s1", units) == 5
        assert pipeline.pending == 2
        assert pipeline.dropped == 3
        assert pipeline.stats()["dropped"] == 3
    finally:
        pipeline.stop()


def test_pipeline_thread_writes_and_skips_already_produced_units(knowledge, tmp_path):
    pipeline = knowledge.KnowledgePipeline(str(tmp_path))
    try:
        assert pipeline.enqueue_from_refresh("session-1", [_unit()]) == 1
    finally:
        pipeline.stop()

    # ``hermes_home`` layout: the pipeline owns the knowledge root, not the profile root.
    page = tmp_path / "minir-memory" / "knowledge" / "tools" / "METHOD-Docker-镜像清理.md"
    assert page.exists()
    assert knowledge.read_front_matter(page)["source_session"] == "session-1"
    assert pipeline.stats()["created"] == 1

    idle = knowledge.KnowledgePipeline(str(tmp_path), autostart=False)
    try:
        assert idle.enqueue_from_refresh("session-1", [_unit()]) == 0
        changed = _unit(content="A rewritten body.", time="2026-10-22")
        assert idle.enqueue_from_refresh("session-1", [changed]) == 1
    finally:
        idle.stop()


def test_selection_caps_rows_and_renders_the_block(knowledge, tmp_path):
    _seed(knowledge, tmp_path)
    rows = knowledge.select_paths("docker image cleanup", tmp_path, limit=3)

    assert len(rows) == 3
    assert all(set(row) == {"category", "org", "topic", "path", "updated_at"} for row in rows)
    assert all(row["category"] == "METHOD" and row["org"] == "tools" for row in rows)

    block = knowledge.render_paths_block(rows)
    assert block.startswith("[knowledge — retrieve on demand]")
    assert block.endswith("[end knowledge]")
    assert block.count("\n- ") == len(rows)
    assert len(block) <= 700
    assert knowledge.render_paths_block([]) == ""


def test_selection_prefers_object_hits(knowledge, tmp_path):
    knowledge.write_unit(_unit(topic="Docker cleanup", objects=["tools:Docker"]), root=tmp_path)
    knowledge.write_unit(_unit(id="k2", topic="Kubernetes rollout",
                               objects=["tools:Kubernetes"]), root=tmp_path)
    rows = knowledge.select_paths("how do I docker prune", tmp_path)
    assert rows[0]["topic"] == "Docker cleanup"


def test_selection_respects_the_character_budget(knowledge, tmp_path):
    _seed(knowledge, tmp_path)
    rows = knowledge.select_paths("docker", tmp_path, limit=6, max_chars=180)
    assert 0 < len(rows) < 6
    assert len(knowledge.render_paths_block(rows)) <= 700


def test_recent_paths_lose_ties_but_stay_selectable(knowledge, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "_now", lambda: "2026-10-21T00:00:00+00:00")
    _seed(knowledge, tmp_path, count=2)
    rows = knowledge.select_paths("docker image cleanup", tmp_path, limit=2)
    recent = rows[0]["path"]
    reranked = knowledge.select_paths("docker image cleanup", tmp_path, limit=2, recent=(recent,))
    assert set(row["path"] for row in reranked) == set(row["path"] for row in rows)
    assert reranked[-1]["path"] == recent


def test_selection_is_empty_without_a_query_or_directory(knowledge, tmp_path):
    _seed(knowledge, tmp_path, count=2)
    assert knowledge.select_paths("", tmp_path) == []
    assert knowledge.select_paths("docker", tmp_path / "missing") == []


def test_block_never_exceeds_its_cap(knowledge):
    rows = [{"category": "METHOD", "org": "tools", "topic": "t" * 400,
             "path": "tools/x.md", "updated_at": ""} for _ in range(6)]
    block = knowledge.render_paths_block(rows)
    assert block.startswith("[knowledge — retrieve on demand]")
    assert block.endswith("[end knowledge]")
    assert len(block) <= 700
