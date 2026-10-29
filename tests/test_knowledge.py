"""Knowledge production pipeline (P6): naming, idempotency, budget, queue and path selection."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture()
def knowledge(plugin_package):
    return importlib.import_module(f"{plugin_package.__name__}.knowledge.knowledge")


def _page_path(org, category, topic):
    """The canonical relative path, from the package under test (the writer's own resolver)."""
    import sys

    package = next(module for name, module in sys.modules.items()
                   if name.startswith("minir_pkg_"))
    return package.knowledge.resolve_path(org, category, topic)


def _unit(**over):
    unit = {
        "id": "k1", "org": "tools", "category": "METHOD", "topic": "Docker 镜像清理",
        "epistemic": "",
        "content": "Run docker system prune to remove stopped containers and dangling images.",
        "time": "2026-10-21", "relations": [], "objects": ["tools:Docker"],
        "anchor": {"from_ts": 0.0, "to_ts": 1e12, "message_ids": []},
    }
    unit.update(over)
    if "path" not in unit:
        try:
            unit["path"] = _page_path(unit["org"], unit["category"], unit["topic"])
        except Exception:
            unit["path"] = ""  # the writer records the failure this unit is meant to produce
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

    meta = knowledge.read_front_matter(tmp_path / second["path"])
    for key in ("schema", "category", "org", "topic", "epistemic", "time", "revisions",
                "updated_at"):
        assert key in meta, f"missing front-matter key {key}"
    assert meta["schema"] == knowledge.SCHEMA
    assert meta["category"] == "METHOD" and meta["org"] == "tools"
    assert meta["revisions"] == 2 and meta["time"] == "2026-10-22"
    assert meta["epistemic"] == ""

    text = (tmp_path / second["path"]).read_text(encoding="utf-8")
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


def test_events_org_is_a_valid_knowledge_page(plugin_package, knowledge, tmp_path):
    layers = importlib.import_module(f"{plugin_package.__name__}.passes.layers")
    page = knowledge.write_unit(
        _unit(id="k3", org="events", category="KNOWLEDGE", epistemic="hypothesis",
              topic="2026-10-23 发布无事故",
              content="released v0.9 at 18:40.\nthe publish finished with no incident."), root=tmp_path)
    assert "events" in layers.ORGS
    assert Path(page["path"]).parent.name == "events"
    meta = knowledge.read_front_matter(tmp_path / page["path"])
    assert (meta["org"], meta["epistemic"]) == ("events", "fact")
    rows = knowledge.search_pages("release", tmp_path, org="events")
    assert [row["org"] for row in rows["results"]] == ["events"]


def test_events_pages_are_facts(plugin_package):
    pages = plugin_package.extraction.validate_pages([{
        "category": "KNOWLEDGE", "org": "events", "topic": "release v0.9",
        "content": "the release went out at 18:40.", "epistemic": "hypothesis"}])
    assert pages[0]["epistemic"] == "fact"


def test_a_slug_collision_is_one_page_whose_metadata_is_refreshed(knowledge, tmp_path):
    first = knowledge.write_unit(_unit(topic="a/b"), root=tmp_path)
    second = knowledge.write_unit(_unit(id="k2", topic="a:b", content="the second body"),
                                  root=tmp_path)
    assert second["path"] == first["path"] and second["status"] == "updated"
    assert len(list((tmp_path / "tools").glob("METHOD-*.md"))) == 1
    assert knowledge.read_front_matter(tmp_path / first["path"])["topic"] == "a:b"


def test_path_is_the_page_identity_not_the_metadata(knowledge, tmp_path):
    first = knowledge.write_unit(_unit(path="tools/METHOD-docker-a.md", topic="docker a"),
                                 root=tmp_path)
    other = knowledge.write_unit(_unit(path="tools/METHOD-docker-b.md", topic="docker b"),
                                 root=tmp_path)
    assert (first["status"], other["status"]) == ("created", "created")
    assert first["path"] != other["path"]
    assert len(list((tmp_path / "tools").glob("METHOD-docker-*.md"))) == 2

    # A page whose front matter disagrees is that one page: the path wins, the metadata follows.
    legacy = tmp_path / "entities" / "KNOWLEDGE-legacy.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\ncategory: METHOD\norg: tools\ntopic: legacy\n---\n\n# legacy\n\n"
                      "the old body\n", encoding="utf-8")
    result = knowledge.write_unit(_unit(path="entities/KNOWLEDGE-legacy.md", topic="legacy"),
                                  root=tmp_path)
    assert result["status"] == "updated"
    assert result["path"] == "entities/KNOWLEDGE-legacy.md"
    assert not (tmp_path / "tools" / "METHOD-legacy.md").exists()
    meta = knowledge.read_front_matter(legacy)
    assert (meta["org"], meta["category"]) == ("entities", "KNOWLEDGE")


@pytest.mark.parametrize("path", ["", "tools", "/tools/METHOD-x.md", "tools/../METHOD-x.md",
                                  "tools\\METHOD-x.md", "C:/tools/METHOD-x.md",
                                  "nope/METHOD-x.md", "tools/METHOD-x.txt",
                                  "tools/loose.md"])
def test_an_invalid_page_path_is_a_failed_write(knowledge, tmp_path, path):
    result = knowledge.write_unit(_unit(path=path), root=tmp_path)
    assert result["status"] == "failed"
    assert list((tmp_path / "_failed").glob("*.json"))


def test_a_unit_without_a_path_is_a_failure_not_a_new_page(knowledge, tmp_path):
    result = knowledge.write_unit(_unit(path=""), root=tmp_path)
    assert result["status"] == "failed" and "path is required" in result["error"]
    payload = json.loads((tmp_path / "_failed" / "METHOD-Docker-镜像清理.json")
                         .read_text(encoding="utf-8"))
    assert (payload["op"], payload["path"], payload["sha256"]) == ("write", "", "")
    assert not list((tmp_path / "tools").glob("*.md"))


def test_a_topic_that_does_not_match_the_path_is_refused(knowledge, tmp_path):
    result = knowledge.write_unit(_unit(path="tools/METHOD-docker-a.md"), root=tmp_path)
    assert result["status"] == "failed" and "does not match" in result["error"]


def test_stale_evidence_is_skipped_and_the_page_kept(knowledge, tmp_path):
    fresh = knowledge.write_unit(_unit(content="fresh fact", time="2026-10-20"), root=tmp_path)
    stale = knowledge.write_unit(_unit(content="contradicting older fact", time="2026-10-01"),
                                 root=tmp_path)
    assert stale["status"] == "stale"
    assert stale["path"] == fresh["path"]
    text = (tmp_path / fresh["path"]).read_text(encoding="utf-8")
    assert "fresh fact" in text and "contradicting" not in text
    assert knowledge.read_front_matter(tmp_path / fresh["path"])["revisions"] == 1


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
    text = (tmp_path / result["path"]).read_text(encoding="utf-8")
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
    assert "Rendered in zh:" in (tmp_path / result["path"]).read_text(encoding="utf-8")


def test_queue_drops_the_oldest_and_counts_it(knowledge, tmp_path):
    pipeline = knowledge.KnowledgePipeline(str(tmp_path), autostart=False, queue_size=2)
    try:
        value_markers = [_unit(id=f"k{index}", topic=f"topic {index}") for index in range(5)]
        assert pipeline.enqueue_from_refresh("s1", value_markers,
                                             from_ts=0.0, to_ts=1e12) == 5
        assert pipeline.pending == 2
        assert pipeline.dropped == 3
        assert pipeline.stats()["dropped"] == 3
    finally:
        pipeline.stop()


def test_pipeline_thread_writes_and_skips_already_produced_units(knowledge, tmp_path):
    pipeline = knowledge.KnowledgePipeline(str(tmp_path))
    try:
        assert pipeline.enqueue_from_refresh("session-1", [_unit()],
                                             from_ts=0.0, to_ts=1e12) == 1
    finally:
        pipeline.stop()

    # ``hermes_home`` layout: the pipeline owns the knowledge root, not the profile root.
    page = tmp_path / "minir-memory" / "knowledge" / "tools" / "METHOD-Docker-镜像清理.md"
    assert page.exists()
    assert knowledge.read_front_matter(page)["source_session"] == "session-1"
    assert pipeline.stats()["created"] == 1

    idle = knowledge.KnowledgePipeline(str(tmp_path), autostart=False)
    try:
        assert idle.enqueue_from_refresh("session-1", [_unit()],
                                         from_ts=0.0, to_ts=1e12) == 0
        changed = _unit(content="A rewritten body.", time="2026-10-22")
        assert idle.enqueue_from_refresh("session-1", [changed],
                                         from_ts=0.0, to_ts=1e12) == 1
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


def test_selection_scores_only_the_pages_ripgrep_returned(knowledge, monkeypatch, tmp_path):
    _seed(knowledge, tmp_path, count=3)
    calls = {}

    def fake_search(pattern, root, **kwargs):
        calls.update(pattern=pattern, root=root, **kwargs)
        return [tmp_path / "tools" / "METHOD-Docker-cleanup-1.md"]

    monkeypatch.setattr(knowledge.rgtool, "search", fake_search)
    rows = knowledge.select_paths("docker cleanup", tmp_path, hermes_home="profile-home")

    assert [row["topic"] for row in rows] == ["Docker cleanup 1"]
    assert calls["root"] == tmp_path and calls["glob"] == "*.md"
    assert calls["ignore_case"] is True
    assert calls["hermes_home"] == "profile-home"
    assert calls["pattern"] == "cleanup|docker"


def test_selection_drops_hits_that_are_not_org_pages(knowledge, monkeypatch, tmp_path):
    _seed(knowledge, tmp_path, count=1)
    monkeypatch.setattr(knowledge.rgtool, "search", lambda *args, **kwargs: [
        tmp_path / "_failed" / "page.md",
        tmp_path / "tools" / "nested" / "page.md",
        tmp_path / "loose.md"])
    assert knowledge.select_paths("docker", tmp_path) == []


def test_selection_scans_every_page_when_ripgrep_is_unavailable(knowledge, monkeypatch, tmp_path):
    _seed(knowledge, tmp_path, count=2)
    knowledge.write_unit(_unit(id="k9", topic="Postgres 备份", content="pg_dump nightly"),
                         root=tmp_path)
    monkeypatch.setattr(knowledge.rgtool, "search", lambda *args, **kwargs: None)

    rows = knowledge.select_paths("docker", tmp_path, limit=6)
    assert len(rows) == 3, "the org-folder scan keeps pages ripgrep could not pre-filter"


def test_edit_page_creates_then_edits_by_line_and_text(knowledge, tmp_path):
    path = "tools/METHOD-edit-page.md"
    created = knowledge.edit_page(
        root=tmp_path, path=path, op="create",
        header={"topic": "edit page", "summary": "edit a page"},
        content="first line\nsecond line\nthird line")
    assert created["status"] == "created" and created["verified"]
    target = tmp_path / path
    sha = created["sha256"]

    replaced = knowledge.edit_page(root=tmp_path, path=path, op="replace",
                                   old="second line", content="second line changed")
    assert replaced["status"] == "written" and replaced["verified"]
    appended = knowledge.edit_page(root=tmp_path, path=path, op="append", content="fourth line")
    assert appended["status"] == "written" and appended["lines"] == 4
    assert knowledge.edit_page(root=tmp_path, path=path, op="append",
                               content="fourth line")["status"] == "unchanged"
    assert knowledge.edit_page(root=tmp_path, path=path, op="append", content="fifth line",
                               expect_sha=sha)["status"] == "stale"
    assert knowledge.edit_page(root=tmp_path, path=path, op="delete",
                               old="first line")["status"] == "written"
    assert "first line" not in target.read_text(encoding="utf-8")


def test_create_has_no_line_count_length_or_token_cap(knowledge, tmp_path):
    path = "tools/METHOD-long-page.md"
    body = "\n".join(f"{index}. claim {index} " + "filler " * 40 for index in range(14))
    assert len(body.splitlines()) > 8 and len(body) > 1200 and max(map(len, body.splitlines())) > 200
    created = knowledge.edit_page(
        root=tmp_path, path=path, op="create", budget=0,
        header={"topic": "long page", "summary": "a page past the old body caps"},
        content=body)
    assert created["status"] == "created" and created["verified"], created
    assert created["warnings"] == []
    _meta, page_body = knowledge._read_document(tmp_path / path)
    _heading, page_content = knowledge._split_heading(page_body)
    claim_lines = page_content.splitlines()
    assert len(claim_lines) == 14 and claim_lines[-1].startswith("13. claim 13")


def test_a_failed_verification_restores_the_previous_bytes(knowledge, tmp_path, monkeypatch):
    path = "tools/METHOD-restore.md"
    knowledge.edit_page(root=tmp_path, path=path, op="create",
                        header={"topic": "restore", "summary": "restore a page"},
                        content="one\ntwo\nthree")
    target = tmp_path / path
    original = target.read_text(encoding="utf-8")
    monkeypatch.setattr(knowledge, "_set_front_matter", lambda text, updates: text)
    result = knowledge.edit_page(root=tmp_path, path=path, op="append", content="four")
    assert result["status"] == "unverified" and result["restored"] is True
    assert target.read_text(encoding="utf-8") == original


def test_anchor_in_window_rejects_inherited_and_unanchored_units(knowledge):
    unit = {"anchor": {"from_ts": 10, "to_ts": 20}}
    assert knowledge.anchor_in_window(unit, 15, 25)
    assert knowledge.anchor_in_window(unit, 20, 30)
    assert not knowledge.anchor_in_window(unit, 21, 30)
    assert not knowledge.anchor_in_window({}, 0, 100)
    assert knowledge.anchor_in_window({"anchor": {"from_ts": "20", "to_ts": "10"}}, 0, 100)


def test_search_pages_returns_matches_and_front_matter(knowledge, tmp_path):
    if knowledge.rgtool.find(str(tmp_path)) is None:
        pytest.skip("ripgrep is not installed")
    knowledge.write_unit(_unit(id="k1", topic="Docker prune"), root=tmp_path)
    archived = tmp_path / "_archive" / "tools" / "METHOD-Docker-prune.md"
    archived.parent.mkdir(parents=True)
    archived.write_text((tmp_path / "tools" / "METHOD-Docker-prune.md").read_text(
        encoding="utf-8"), encoding="utf-8")

    found = knowledge.search_pages("docker.*prune", tmp_path, hermes_home=str(tmp_path))

    assert found["count"] == 1 and found["total"] == 1, "an archived page is not recallable"
    row = found["results"][0]
    assert (row["org"], row["category"], row["topic"]) == ("tools", "METHOD", "Docker prune")
    assert row["path"] == "tools/METHOD-Docker-prune.md"
    assert (tmp_path / row["path"]).is_file()
    assert row["file"] == "METHOD-Docker-prune.md" and len(row["sha256"]) == 64
    assert row["revisions"] == 1 and row["summary"]
    assert any("system prune" in line["text"] for line in row["lines"])
    assert row["match"] and row["match"]["text"].lstrip()[:5] != "topic:"


def test_search_pages_filters_and_caps(knowledge, monkeypatch, tmp_path):
    knowledge.write_unit(_unit(id="k1", topic="Docker cleanup"), root=tmp_path)
    knowledge.write_unit(_unit(id="k2", org="entities", category="KNOWLEDGE",
                               topic="Docker compose", content="docker compose up"),
                         root=tmp_path)
    hits = [
        {"path": str(tmp_path / "tools" / "METHOD-Docker-cleanup.md"), "line": 9,
         "text": "docker prune one"},
        {"path": str(tmp_path / "tools" / "METHOD-Docker-cleanup.md"), "line": 10,
         "text": "docker prune two"},
        {"path": str(tmp_path / "tools" / "METHOD-Docker-cleanup.md"), "line": 11,
         "text": "docker prune three"},
        {"path": str(tmp_path / "entities" / "KNOWLEDGE-Docker-compose.md"), "line": 2,
         "text": "docker compose up"},
        {"path": str(tmp_path / "_failed" / "notes.md"), "line": 1, "text": "docker prune"},
    ]
    seen = {}

    def fake_matches(pattern, root, **kwargs):
        seen.update(kwargs)
        return hits

    monkeypatch.setattr(knowledge.rgtool, "matches", fake_matches)

    everyone = knowledge.search_pages("docker", tmp_path, limit=5)
    assert [row["org"] for row in everyone["results"]] == ["tools", "entities"]
    assert everyone["results"][0]["match"]["text"] == "docker prune one"
    assert seen["ignore_case"] is True and seen["max_per_file"] >= 12

    only_entities = knowledge.search_pages("docker", tmp_path, org="entities")
    assert [row["org"] for row in only_entities["results"]] == ["entities"]
    only_knowledge = knowledge.search_pages("docker", tmp_path, category="knowledge")
    assert [row["category"] for row in only_knowledge["results"]] == ["KNOWLEDGE"]

    capped = knowledge.search_pages("docker", tmp_path, limit=1)
    assert capped["count"] == 1 and capped["total"] == 2


def test_search_pages_rejects_unknown_filters(knowledge, tmp_path):
    with pytest.raises(ValueError):
        knowledge.search_pages("docker", tmp_path, org="nope")
    with pytest.raises(ValueError):
        knowledge.search_pages("docker", tmp_path, category="NOPE")


def test_search_pages_is_none_without_ripgrep(knowledge, monkeypatch, tmp_path):
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(knowledge.rgtool, "matches", lambda *args, **kwargs: None)
    assert knowledge.search_pages("docker", tmp_path) is None


def test_block_never_exceeds_its_cap(knowledge):
    rows = [{"category": "METHOD", "org": "tools", "topic": "t" * 400,
             "path": "tools/x.md", "updated_at": ""} for _ in range(6)]
    block = knowledge.render_paths_block(rows)
    assert block.startswith("[knowledge — retrieve on demand]")
    assert block.endswith("[end knowledge]")
    assert len(block) <= 700


def test_an_events_unit_needs_a_date_an_object_and_a_result(knowledge, tmp_path):
    shape = {"org": "events", "category": "KNOWLEDGE", "topic": "Docker cleanup",
             "path": "events/KNOWLEDGE-Docker-cleanup.md"}

    undated = knowledge.write_unit(_unit(**shape, time=""), root=tmp_path)
    assert undated["status"] == "failed" and "dated time" in undated["error"]

    objectless = knowledge.write_unit(_unit(**shape, objects=[]), root=tmp_path)
    assert objectless["status"] == "failed" and "object" in objectless["error"]

    resultless = knowledge.write_unit(_unit(**shape, content="Pruned the images."), root=tmp_path)
    assert resultless["status"] == "failed" and "result" in resultless["error"]


def test_a_projects_unit_needs_a_goal_a_deliverable_and_a_state(knowledge, tmp_path):
    result = knowledge.write_unit(
        _unit(org="projects", category="INTENT", topic="minir", path="projects/INTENT-minir.md",
              content="Ship the strict alignment."), root=tmp_path)
    assert result["status"] == "failed" and "3 lines" in result["error"]


def test_pages_under_the_archive_folder_are_not_searchable(knowledge, tmp_path):
    archived = tmp_path / "_archive" / "tools" / "KNOWLEDGE-docker.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("---\ncategory: KNOWLEDGE\norg: tools\ntopic: docker prune\n---\n\n"
                        "# docker prune\n\ndocker prune images\n", encoding="utf-8")

    assert knowledge.search_pages("docker", tmp_path)["count"] == 0


def test_a_page_declares_its_edges_and_a_file_rebuild_replaces_them(knowledge, plugin_package,
                                                                   tmp_path):
    knowledge.write_unit(_unit(
        path="tools/METHOD-docker.md", topic="docker",
        objects=["tools:Docker", "not-a-ref"],
        relations=[{"type": "derived_from", "ref": "events:release"},
                   {"type": "nonsense", "ref": "tools:X"}]), root=tmp_path)
    edges = knowledge.derived_edges(tmp_path)
    assert {(edge["src"], edge["rel"], edge["dst"]) for edge in edges} == {
        (("page", "tools/METHOD-docker.md"), "instance_of", ("tools", "Docker")),
        (("page", "tools/METHOD-docker.md"), "derived_from", ("events", "release"))}

    instance = plugin_package.store.MinirStore(tmp_path / "memory.db")
    try:
        assert instance.rebuild_page_edges(edges) == 2
        assert instance.rebuild_page_edges(edges) == 2  # idempotent, not duplicated
        assert instance._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 2

        (tmp_path / "tools" / "METHOD-docker.md").rename(
            tmp_path / "tools" / "METHOD-docker-moved.md")
        moved = knowledge.derived_edges(tmp_path)
        assert {edge["src"][1] for edge in moved} == {"tools/METHOD-docker-moved.md"}
        instance.rebuild_page_edges(moved)
        assert instance._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 2
    finally:
        instance.close()


def test_run_value_markers_is_the_one_writer_and_is_idempotent(knowledge, tmp_path):
    units = [_unit(), _unit(id="k2", path="tools/METHOD-docker-prune.md", topic="docker prune")]

    first = knowledge.run_value_markers(units, root=tmp_path, session_id="s1", source="test")
    second = knowledge.run_value_markers(units, root=tmp_path, session_id="s1", source="test")

    assert [(item["status"], item["topic"]) for item in first] == [
        ("created", "Docker 镜像清理"), ("created", "docker prune")]
    assert [item["path"] for item in first] == ["tools/METHOD-Docker-镜像清理.md",
                                                "tools/METHOD-docker-prune.md"]
    assert [item["status"] for item in second] == ["unchanged", "unchanged"]
