"""adviser_session_search — five query shapes over a stand-in host ``state.db``."""

from __future__ import annotations

import json
import sqlite3

import pytest


SESSION_ROWS = [
    # id, source, model, title, started, ended, last_active, messages, tools, parent, reason,
    # profile, cwd, archived, hidden
    ("s1", "cli", "deepseek-v4-flash", "Deploy planning", 100.0, 500.0, 400.0, 8, 1, None,
     None, "default", "C:/work", 0, 0),
    ("s2", "cron", "deepseek-v4-flash", "Nightly kubernetes report", 200.0, 210.0, 210.0, 2, 0,
     None, "completed", "default", None, 0, 0),
    ("s3", "subagent", "deepseek-v4-flash", "scratch kubernetes", 300.0, 310.0, 310.0, 1, 0,
     None, None, "default", None, 0, 0),
    ("s4", "kanban", "deepseek-v4-flash", "kanban board", 400.0, 410.0, 410.0, 1, 0,
     None, None, "default", None, 0, 0),
    ("s5", "cli", "deepseek-v4-flash", "Old deploy notes", 50.0, 60.0, 60.0, 1, 0,
     None, None, "work", None, 1, 0),
    ("s6", "cli", "deepseek-v4-flash", "Hidden session", 60.0, 70.0, 70.0, 1, 0,
     None, None, "default", None, 0, 1),
]

MESSAGE_ROWS = [
    # id, session, role, content, timestamp, active, compacted, summary, tool_name, uid,
    # display_kind
    (1, "s1", "user", "how do we deploy kubernetes to staging?", 101.0, 1, 0, 0, None,
     "u1", None),
    (2, "s1", "assistant", "run kubectl apply -f deploy.yaml", 102.0, 1, 0, 0, None, "a1",
     None),
    (3, "s1", "tool", '{"pods": 3}', 103.0, 1, 0, 0, "terminal", "t1", None),
    (4, "s1", "user", "[Assistant Rules]\nskills prompt", 104.0, 1, 0, 0, None, "u2", None),
    (5, "s1", "user", "[minir fold · turns 1-2]", 105.0, 1, 0, 0, None, "u3", None),
    (6, "s1", "assistant", "compacted summary", 106.0, 1, 0, 1, None, "a2", None),
    (7, "s1", "user", "rewind row", 107.0, 0, 0, 0, None, "u4", None),
    (8, "s1", "assistant", "archived display row", 108.0, 0, 1, 0, None, "a3", None),
    (9, "s1", "user", "hidden scaffolding", 109.0, 1, 0, 0, None, "u5", "hidden"),
    (10, "s1", "user", "deploy history notes", 110.0, 1, 0, 0, None, "u6", None),
    (11, "s2", "user", "kubernetes nightly report", 201.0, 1, 0, 0, None, "u7", None),
    (12, "s2", "assistant", "cron done", 202.0, 1, 0, 0, None, "a4", None),
    (13, "s3", "user", "kubernetes subagent scratch", 301.0, 1, 0, 0, None, "u8", None),
    (14, "s4", "user", "kanban kubernetes", 401.0, 1, 0, 0, None, "u9", None),
    (15, "s5", "user", "old kubernetes deploy notes", 51.0, 1, 0, 0, None, "u10", None),
    (16, "s6", "user", "hidden kubernetes", 61.0, 1, 0, 0, None, "u11", None),
]


def _make_db(path):
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT, model TEXT, title TEXT,
                started_at REAL, ended_at REAL, last_activity_at REAL,
                message_count INTEGER, tool_call_count INTEGER,
                parent_session_id TEXT, end_reason TEXT, profile_name TEXT, cwd TEXT,
                archived INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
                content TEXT, timestamp REAL, active INTEGER DEFAULT 1,
                compacted INTEGER DEFAULT 0, _compressed_summary INTEGER DEFAULT 0,
                tool_name TEXT, message_uid TEXT, display_kind TEXT
            );
        """)
        connection.executemany(
            "INSERT INTO sessions (id, source, model, title, started_at, ended_at, "
            "last_activity_at, message_count, tool_call_count, parent_session_id, end_reason, "
            "profile_name, cwd, archived, hidden) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            SESSION_ROWS)
        connection.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp, active, compacted, "
            "_compressed_summary, tool_name, message_uid, display_kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", MESSAGE_ROWS)
        connection.commit()
    finally:
        connection.close()


def _add_fts(path, *, trigram=False):
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            "CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name);"
            "INSERT INTO messages_fts (rowid, content, tool_name) "
            "SELECT id, content, tool_name FROM messages;")
        if trigram:
            connection.executescript(
                "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, "
                "tokenize='trigram');"
                "INSERT INTO messages_fts_trigram (rowid, content) "
                "SELECT id, content FROM messages;")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    _make_db(path)
    return path


@pytest.fixture()
def search(plugin_package):
    return plugin_package.adviser.session_search


def _query(search, path, **args):
    return search.query(args, state_db=str(path))


# -- search -------------------------------------------------------------------

def test_search_defaults_hide_noise_and_returns_session_context(search, db):
    data = _query(search, db, mode="search", query="kubernetes")

    ids = [match["message_id"] for match in data["matches"]]
    assert ids == [11, 1], "newest first; user+assistant, live, non-synthetic, non-hidden"
    assert (data["total"], data["sessions"], data["index"]) == (2, 2, "like")
    assert data["matches"][0]["session"]["title"] == "Nightly kubernetes report"
    assert data["matches"][0]["session"]["source"] == "cron"
    assert "kubernetes" in data["matches"][0]["snippet"]


def test_search_roles_sources_and_visibility_flags(search, db):
    tool_only = _query(search, db, mode="search", query="pods", role="tool")
    assert [match["message_id"] for match in tool_only["matches"]] == [3]
    assert tool_only["matches"][0]["tool_name"] == "terminal"

    subagent = _query(search, db, mode="search", query="kubernetes", source="subagent")
    assert [match["message_id"] for match in subagent["matches"]] == [13]

    archived = _query(search, db, mode="search", query="kubernetes", include_archived=True)
    assert 15 in [match["message_id"] for match in archived["matches"]]

    hidden = _query(search, db, mode="search", query="kubernetes", include_hidden=True)
    assert {13, 14, 16} <= {match["message_id"] for match in hidden["matches"]}

    synthetic = _query(search, db, mode="search", query="Assistant Rules",
                       include_synthetic=True)
    assert [match["message_id"] for match in synthetic["matches"]] == [4]


def test_search_time_bounds_and_paging(search, db):
    data = _query(search, db, mode="search", query="deploy", after=102,
                  before=300, sort="newest", limit=1)
    assert [match["message_id"] for match in data["matches"]] == [10]
    assert data["total"] == 2
    assert data["has_more"] is True and data["next_offset"] == 1

    second = _query(search, db, mode="search", query="deploy", after=102, before=300,
                    sort="newest", limit=1, offset=1)
    assert [match["message_id"] for match in second["matches"]] == [2]
    assert second["has_more"] is False


def test_search_match_modes_and_content_toggle(search, db):
    assert _query(search, db, mode="search", query="deploy staging", match="all")["total"] == 1
    assert _query(search, db, mode="search", query="deploy staging",
                  match="any")["total"] == 3
    assert _query(search, db, mode="search", query="deploy staging",
                  match="phrase")["total"] == 0
    assert _query(search, db, mode="search", query="deploy",
                  match="phrase")["total"] == 3

    entry = _query(search, db, mode="search", query="kubectl",
                   include_content=False)["matches"][0]
    assert "content" not in entry
    assert entry["snippet"]


def test_search_uses_fts_when_available(search, db):
    _add_fts(db)

    data = _query(search, db, mode="search", query="kubectl")

    assert data["index"] == "messages_fts"
    assert [match["message_id"] for match in data["matches"]] == [2]
    assert data["matches"][0]["snippet"].startswith("run")


def test_search_auto_selects_trigram_for_cjk_substrings(search, db):
    connection = sqlite3.connect(str(db))
    try:
        connection.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) "
            "VALUES (17, 's1', 'user', '插件工具集的使用说明', 120.0)")
        connection.commit()
    finally:
        connection.close()
    _add_fts(db, trigram=True)

    data = _query(search, db, mode="search", query="插件工具")

    assert data["index"] == "messages_fts_trigram"
    assert [match["message_id"] for match in data["matches"]] == [17]


def test_empty_fts_index_falls_back_to_like(search, db):
    connection = sqlite3.connect(str(db))
    try:
        connection.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
        connection.commit()
    finally:
        connection.close()

    data = _query(search, db, mode="search", query="kubernetes")

    assert data["index"] == "like"
    assert [match["message_id"] for match in data["matches"]] == [11, 1]


def test_raw_match_reports_a_bad_fts_expression(search, db):
    _add_fts(db)

    with pytest.raises(search.QueryError) as caught:
        _query(search, db, mode="search", query='"unterminated', match="raw")

    assert caught.value.kind == "invalid_query"


def test_search_query_cannot_inject_sql(search, db):
    data = _query(search, db, mode="search",
                  query="x'; DROP TABLE messages; --")

    assert data["matches"] == []
    connection = sqlite3.connect(str(db))
    try:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 16
    finally:
        connection.close()


# -- sessions -----------------------------------------------------------------

def test_sessions_browse_filters_and_sorts(search, db):
    default = _query(search, db, mode="sessions")
    assert [row["id"] for row in default["sessions"]] == ["s1", "s2"]

    by_title = _query(search, db, mode="sessions", query="deploy")
    assert [row["id"] for row in by_title["sessions"]] == ["s1"]

    archived = _query(search, db, mode="sessions", query="deploy", include_archived=True)
    assert {row["id"] for row in archived["sessions"]} == {"s1", "s5"}

    cron = _query(search, db, mode="sessions", source="cron")
    assert [row["id"] for row in cron["sessions"]] == ["s2"]

    recent = _query(search, db, mode="sessions", after=150)
    assert [row["id"] for row in recent["sessions"]] == ["s2"]

    oldest = _query(search, db, mode="sessions", sort="oldest")
    assert [row["id"] for row in oldest["sessions"]] == ["s1", "s2"]


def test_sessions_paging_reports_total(search, db):
    data = _query(search, db, mode="sessions", limit=1, offset=1)

    assert [row["id"] for row in data["sessions"]] == ["s2"]
    assert data["total"] == 2 and data["has_more"] is False


# -- messages / around --------------------------------------------------------

def test_messages_reads_the_transcript_with_filters(search, db):
    data = _query(search, db, mode="messages", session_id="s1")
    assert [message["message_id"] for message in data["messages"]] == [1, 2, 3, 10]
    assert data["session"]["title"] == "Deploy planning"
    assert data["total"] == 4

    conversation = _query(search, db, mode="messages", session_id="s1", role="user,assistant")
    assert [message["message_id"] for message in conversation["messages"]] == [1, 2, 10]

    compacted = _query(search, db, mode="messages", session_id="s1", include_compacted=True)
    assert [message["message_id"] for message in compacted["messages"]] == [1, 2, 3, 6, 8, 10]

    everything = _query(search, db, mode="messages", session_id="s1", include_inactive=True)
    assert [message["message_id"] for message in everything["messages"]] == [
        1, 2, 3, 6, 7, 8, 10]


def test_messages_pages_from_the_newest_end_but_returns_chronological(search, db):
    data = _query(search, db, mode="messages", session_id="s1", order="newest", limit=2)

    assert [message["message_id"] for message in data["messages"]] == [3, 10]
    assert data["has_more"] is True and data["next_offset"] == 2


def test_messages_are_truncated(search, db):
    data = _query(search, db, mode="messages", session_id="s1", max_chars=5)

    message = next(row for row in data["messages"] if row["message_id"] == 1)
    assert message["content"] == "how d…"
    assert message["content_truncated"] is True
    assert message["original_chars"] == len("how do we deploy kubernetes to staging?")


def test_messages_unknown_session_is_an_error(search, db):
    with pytest.raises(search.QueryError) as caught:
        _query(search, db, mode="messages", session_id="missing")

    assert caught.value.kind == "session_not_found"


def test_around_returns_the_anchor_window(search, db):
    data = _query(search, db, mode="around", session_id="s1", message_id=2, window=1)

    assert [message["message_id"] for message in data["messages"]] == [1, 2, 3]
    assert data["messages_before"] == 1 and data["messages_after"] == 1
    assert data["messages"][1]["anchor"] is True
    assert data["session"]["id"] == "s1"


def test_around_refuses_a_filtered_or_foreign_anchor(search, db):
    with pytest.raises(search.QueryError) as inactive:
        _query(search, db, mode="around", session_id="s1", message_id=7)
    assert inactive.value.kind == "message_filtered"

    with pytest.raises(search.QueryError) as foreign:
        _query(search, db, mode="around", session_id="s1", message_id=11)
    assert foreign.value.kind == "message_not_found"


# -- stats / resolution / handler --------------------------------------------

def test_stats_reports_the_store_shape(search, db):
    data = _query(search, db, mode="stats")

    assert data["sessions"] == 6
    assert data["messages"] == 16
    assert data["sessions_archived"] == 1
    assert data["messages_compacted"] == 1
    assert data["by_role"][0]["role"] == "user"
    assert data["by_source"][0]["sessions"] == 3
    assert data["fts"] == {"messages_fts": False, "messages_fts_trigram": False}
    assert data["db_bytes"] > 0


def test_mode_is_inferred_from_the_arguments(search, db):
    assert _query(search, db, query="kubectl")["mode"] == "search"
    assert _query(search, db, session_id="s1")["mode"] == "messages"
    assert _query(search, db, session_id="s1", message_id=2)["mode"] == "around"
    assert _query(search, db)["mode"] == "sessions"


def test_query_rejects_invalid_arguments(search, db):
    for args, kind in (
        ({"mode": "search"}, "invalid_args"),
        ({"mode": "nope"}, "invalid_args"),
        ({"mode": "search", "query": "x", "match": "near"}, "invalid_args"),
        ({"mode": "search", "query": "x", "after": "yesterday-ish"}, "invalid_args"),
        ({"mode": "around", "session_id": "s1"}, "invalid_args"),
        ({"mode": "messages", "session_id": "s1", "order": "sideways"}, "invalid_args"),
    ):
        with pytest.raises(search.QueryError) as caught:
            search.query(args, state_db=str(db))
        assert caught.value.kind == kind

    with pytest.raises(search.QueryError) as caught:
        search.query({"mode": "stats", "profile": "../escape"}, hermes_home_path=db.parent)
    assert caught.value.kind == "invalid_args"


def test_missing_database_is_reported(search, tmp_path):
    with pytest.raises(search.QueryError) as caught:
        search.query({"mode": "stats"}, state_db=str(tmp_path / "missing.db"))

    assert caught.value.kind == "state_db_missing"


def test_minimal_schema_is_supported(search, tmp_path):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL);"
            "INSERT INTO sessions (id) VALUES ('s1');"
            "INSERT INTO messages VALUES (1, 's1', 'user', 'old store question', 10.0);")
        connection.commit()
    finally:
        connection.close()

    assert _query(search, path, mode="search", query="question")["total"] == 1
    assert _query(search, path, mode="sessions")["sessions"][0]["id"] == "s1"
    assert _query(search, path, mode="messages", session_id="s1")["count"] == 1
    assert _query(search, path, mode="stats")["messages"] == 1


def test_profile_path_resolution(search, tmp_path):
    home = tmp_path / "home"
    assert search.state_db_path(hermes_home_path=home) == home / "state.db"
    assert search.state_db_path(hermes_home_path=home, profile="work") == \
        home / "profiles" / "work" / "state.db"
    assert search.state_db_path(hermes_home_path=home, profile="default") == home / "state.db"


def test_handler_returns_json_and_never_raises(search, db):
    payload = json.loads(search.handle({"mode": "stats"}, state_db=str(db), task_id="t",
                                       session_id="s", user_task="u"))
    assert payload["success"] is True
    assert payload["data"]["mode"] == "stats"

    failed = json.loads(search.handle({"mode": "nope"}, state_db=str(db)))
    assert failed["success"] is False
    assert failed["error"]["type"] == "invalid_args"

    missing = json.loads(search.handle({"mode": "stats"}, state_db=str(db / "nope.db")))
    assert missing["success"] is False
    assert missing["error"]["type"] == "state_db_missing"


def test_handler_resolves_hermes_home_kwarg(search, tmp_path):
    _make_db(tmp_path / "state.db")

    payload = json.loads(search.handle({"mode": "stats"}, hermes_home=str(tmp_path)))

    assert payload["success"] is True
    assert payload["data"]["sessions"] == 6
