"""The conclusion-kinds contract: one registry, two tool destinations, lexical recall.

Covers the design's acceptance list (``docs/designs/conclusion-kinds.md`` §10, T1-T10). The
store stays permissive by design (r4): these tests exercise the vocabulary, the derived tables,
the migration, the lexical search path and the tool boundary — not the store's kind validation,
which does not exist.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin" / "minir"
PLUGIN_DIR = PLUGIN_ROOT.parent
REPO_ROOT = PLUGIN_ROOT.parents[1]


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


def test_t1_the_vocabulary_is_the_registry(plugin_package):
    module = plugin_package.store
    kinds = module.CONCLUSION_KINDS
    assert len(kinds) == 9 and len(set(kinds)) == 9
    assert tuple(module._CONCLUSION_SPECS) == kinds, "the registry order is the vocabulary order"
    assert "note" not in kinds and "base.window_summary" not in kinds
    assert not set(kinds) & set(module._MARKER_KINDS)
    assert len(module._RETIRED_SPECS) == 4
    assert not set(module._RETIRED_SPECS) & set(kinds)
    for spec in module._CONCLUSION_SPECS.values():
        assert spec.writer in {"operator", "layer"}
        assert spec.recall in {"cross_session", "session"}
        assert spec.meaning and spec.weight > 0


def test_t2_the_derived_tables_cover_every_kind(plugin_package):
    module = plugin_package.store
    every_kind = {*module.CONCLUSION_KINDS, *module._MARKER_KINDS, *module._RETIRED_SPECS}
    # 9 conclusions + 5 markers + the 2 retired kinds that still carry rows.
    assert len(module._KIND_WEIGHT) == 16
    assert module._KIND_WEIGHT["window_summary"] == 1.0
    assert module._KIND_WEIGHT["base.window_summary"] == 1.0
    assert len(module._DURABLE_KINDS) == 9
    assert set(module._DURABLE_KINDS) == {
        "base.user_representation", "base.user_peer_card", "base.ai_identity_card",
        *module._MARKER_KINDS, "note"}
    assert set(module._SESSION_BOUND_KINDS) == {
        "base.ai_self_representation", "base.value_markers"}
    assert set(module._LIVE_WHEN_SUPERSEDED) == {"window_summary"}
    assert {kind for kind in every_kind if module._versioned(kind)} == set(module._MARKER_KINDS)
    assert not any(module._versioned(kind) for kind in module.CONCLUSION_KINDS)
    assert not any(module._versioned(kind) for kind in module._RETIRED_SPECS)


def test_t3_a_base_refresh_publishes_five_rows(store):
    store.add_base_refresh(
        "s1", "user:ada", window_from_ts=0.0, window_to_ts=10.0,
        window_from_turn=1, window_to_turn=2,
        user_representation="- [goal] ship", user_peer_card="- terse",
        ai_self_representation="- [done] x", ai_identity_card="- direct",
        window_summary=json.dumps("## Window Summary\n\n### Objective\n- ship"),
        value_markers='{"value_markers": []}')

    assert store.publish_base_conclusions("s1", user_subject="user:ada") == 5

    published = {row["kind"] for row in store._conn.execute(
        "SELECT kind FROM conclusions WHERE kind LIKE 'base.%'")}
    assert published == {
        "base.user_representation", "base.user_peer_card", "base.ai_self_representation",
        "base.ai_identity_card", "base.value_markers"}
    assert "base.window_summary" not in published


def _artifact(body: str) -> str:
    return f"## Window Summary\n\n{body}"


def test_t4_the_migration_deletes_inserts_and_retires(plugin_package, tmp_path):
    """A duplicate goes, a window with no chunk becomes one, an orphan stays readable."""
    path = tmp_path / "memory.db"
    seed = plugin_package.store.MinirStore(path)
    duplicate = _artifact("### Objective\n- ship it")
    missing_chunk = _artifact("### Objective\n- next window")
    orphan = _artifact("### Objective\n- no window to attribute this to")
    for window_from_ts, text in ((10.0, duplicate), (20.0, missing_chunk)):
        seed.add_base_refresh(
            "s1", "user:ada", window_from_ts=window_from_ts, window_to_ts=window_from_ts + 5.0,
            window_from_turn=1, window_to_turn=1,
            user_representation="", user_peer_card="", ai_self_representation="",
            ai_identity_card="", window_summary=json.dumps(text))
    seed.add_chunk_summary("s1", 10.0, "### Objective\n- ship it")
    for key, text in (("session:s1:a", duplicate), ("session:s1:b", missing_chunk),
                      ("session:s2:c", orphan)):
        seed.save_conclusion(
            "base.window_summary", json.dumps(text), key=key,
            session_id="s1" if ":s1:" in key else "s2", subject="user:ada")
    seed.close()
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()

    migrated = plugin_package.store.MinirStore(path)
    try:
        assert migrated._conn.execute(
            "SELECT COUNT(*) FROM conclusions WHERE kind = 'base.window_summary'"
        ).fetchone()[0] == 1, "only the row with no window stays"
        assert migrated._conn.execute(
            "SELECT COUNT(*) FROM conclusions WHERE kind = 'window_summary'"
        ).fetchone()[0] == 2, "the window without a chunk became one"
        inserted = migrated.get_conclusion("window_summary", "chunk:s1:20.000000")
        assert inserted is not None
        assert inserted["content"] == "### Objective\n- next window"
        assert inserted["session_id"] == "s1"
    finally:
        migrated.close()

    again = plugin_package.store.MinirStore(path)
    try:
        assert again._conn.execute(
            "SELECT COUNT(*) FROM conclusions WHERE kind IN "
            "('base.window_summary', 'window_summary')").fetchone()[0] == 3
    finally:
        again.close()


def test_t5_search_is_lexical_and_scores_by_overlap_plus_weight(store):
    with pytest.raises(TypeError):
        store.search("docker", session_id="s1", vector_ranks={1: 0})
    store.save_conclusion("entities", "docker prune keeps images", key="a", session_id="s1")
    store.save_conclusion("window_summary", "docker prune removes images",
                          key="chunk:s1:0.000000", session_id="s1")

    rows = store.search("docker", session_id="s1")

    assert [(row["kind"], row["score"]) for row in rows] == [
        ("window_summary", pytest.approx(2.0)), ("entities", pytest.approx(1.9))]


def test_t5_the_expansion_tail_follows_the_direct_hits(store):
    store.save_conclusion("projects", "docker parent row", key="p", session_id="s1")
    store.save_conclusion("projects", "the child", key="c", session_id="s1")
    assert store.link(("projects", "p"), "part_of", ("projects", "c"))

    expanded = store.search("docker parent", session_id="s1", expand=1)

    assert [row["key"] for row in expanded] == ["p", "c"]
    assert expanded[0].get("via") in (None, ""), "the direct hit keeps its place"
    assert expanded[1]["via"] == "projects:p", "the expansion is appended after the direct hits"


def test_t5_a_two_character_cjk_query_still_matches(store):
    store.save_conclusion("entities", "部署完成", key="zh", session_id="s1")

    assert [row["key"] for row in store.search("部署", session_id="s1")] == ["zh"], \
        "trigram cannot index two characters; the LIKE fallback still answers"


def test_t6_only_conclusion_kinds_are_a_search_filter(plugin_package, provider):
    for kind in plugin_package.store.CONCLUSION_KINDS:
        payload = json.loads(provider.handle_tool_call(
            "minir_search", {"query": "x", "type": kind, "session_id": provider._session_id}))
        assert "error" not in payload, kind
    for kind in ("entities", "note", "base.window_summary"):
        payload = json.loads(provider.handle_tool_call(
            "minir_search", {"query": "x", "type": kind}))
        assert "error" in payload, kind


def test_t7_the_tools_have_no_conclusion_mode(provider):
    missing = json.loads(provider.handle_tool_call(
        "minir_remember", {"type": "tools", "content": "x"}))
    assert "error" in missing, "remember_mode is required"
    legacy = json.loads(provider.handle_tool_call(
        "minir_remember", {"type": "tools", "content": "x", "remember_mode": "conclusion"}))
    assert "error" in legacy
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_forget", {"remember_mode": "conclusion", "type": "tools"}))
    assert "error" in json.loads(provider.handle_tool_call(
        "minir_forget", {"conclusions_id": 1})), "there is no conclusion branch to address"

    schemas = {schema["name"]: schema for schema in provider.get_tool_schemas()}
    forget = schemas["minir_forget"]["parameters"]
    assert "conclusions_id" not in forget["properties"]
    assert forget["required"] == ["remember_mode"]


def test_t8_production_kind_literals_stay_inside_the_vocabulary(plugin_package):
    module = plugin_package.store
    allowed = {*module.CONCLUSION_KINDS, *module._MARKER_KINDS, *module._RETIRED_SPECS}
    written = re.compile(r'(?:save_conclusion|_put_conclusion_locked)\(\s*"([^"]+)"')
    seen: set[str] = set()
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        for kind in written.findall(path.read_text(encoding="utf-8")):
            seen.add(kind)
            assert kind in allowed, f"{path.name} writes the unregistered kind {kind!r}"
    assert "note" not in seen, "no production code writes a note row"


def test_t9_the_marker_vocabulary_matches_the_prompting_table(plugin_package):
    assert {key for key, _ in plugin_package.prompting.MARKER_TYPES} == \
        set(plugin_package.store._MARKER_KINDS)


def test_t10_the_vector_subsystem_is_gone(plugin_package, provider):
    assert not (PLUGIN_ROOT / "vector.py").exists()
    assert not (REPO_ROOT / "tests" / "test_vector.py").exists()
    assert not hasattr(plugin_package, "vector"), "nothing imports the module"
    for path in sorted(PLUGIN_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not re.search(r"^\s*(?:from\s+\.\s+)?import\s+vector\b|sqlite_vec", text, re.M), \
            path

    stats = json.loads(provider.handle_tool_call("minir_stats", {}))
    assert "vector" not in stats
    assert not any(name.startswith("vector") for name in stats["scheduler"])
    manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8-sig").lower()
    assert "vector" not in manifest and "sqlite-vec" not in manifest
