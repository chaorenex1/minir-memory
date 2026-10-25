"""Host history reads — active rows, synthetic-message filtering, profile, identity files."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _state_db(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL,
                message_uid TEXT,
                tool_name TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                _compressed_summary INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE sessions (id TEXT PRIMARY KEY, profile_name TEXT);
        """)
        rows = [
            ("s1", "user", "real question", 10.0, "u1", 1, 0),
            ("s1", "assistant", "real answer", 10.5, "a1", 1, 0),
            ("s1", "user", "[Assistant Rules]\nskills prompt", 11.0, "u2", 1, 0),
            ("s1", "user", "[CONTEXT COMPACTION — REFERENCE ONLY] old", 11.5, "u3", 1, 0),
            ("s1", "user", "compacted duplicate", 12.0, "u4", 1, 1),
            ("s1", "user", "inactive row", 12.5, "u5", 0, 0),
            ("s1", "user", "outside the window", 20.0, "u6", 1, 0),
            ("s2", "user", "other session", 10.0, "u7", 1, 0),
        ]
        connection.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, message_uid, active, "
            "_compressed_summary) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, message_uid, tool_name,"
            " active, _compressed_summary) VALUES ('s1', 'tool', 'pytest: 4 passed', 10.7,"
            " 't1', 'terminal', 1, 0)")
        connection.execute("INSERT INTO sessions (id, profile_name) VALUES ('s1', 'work')")
        connection.commit()
    finally:
        connection.close()


def test_reads_only_active_real_user_and_assistant_rows_in_the_window(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    rows = plugin_package.history.read_messages(path, "s1", 9.0, 13.0)
    assert [row["content"] for row in rows] == ["real question", "real answer"]
    assert rows[0]["message_uid"] == "u1" and rows[0]["created_at"] == 10.0


def test_assistant_summary_and_notice_never_enter_a_layer_window(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    connection = sqlite3.connect(str(path))
    connection.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, message_uid) "
        "VALUES ('s1', ?, ?, ?, ?)",
        [("assistant", "[minir summary #r1]\n## Goal\n- derived", 30.0, "s1a"),
         ("assistant", "[minir memory notice] derived context", 30.1, "s1b"),
         ("assistant", "real follow-up", 13.2, "s1c")])
    connection.commit()
    connection.close()

    rows = plugin_package.history.read_messages(path, "s1", 12.9, 14.0)
    assert [row["content"] for row in rows] == ["real follow-up"]
    assert plugin_package.history.latest_ts(path, "s1") == 13.2, \
        "the newer synthetic assistant rows are skipped"


def test_latest_timestamp_skips_a_newest_summary_row(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    connection = sqlite3.connect(str(path))
    connection.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, message_uid) "
        "VALUES ('s1', 'assistant', '[minir summary #r1] derived', 30.0, 's1a')")
    connection.commit()
    connection.close()
    assert plugin_package.history.latest_ts(path, "s1") == 20.0


def test_adviser_injection_is_stripped_from_tool_evidence(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, message_uid, tool_name,"
            " active, _compressed_summary) VALUES ('s1', 'tool', ?, 10.8, 't2', 'terminal', 1, 0)",
            ("pytest: 4 passed\n\n[adviser — advisory only]\n"
             "- [concern] check stderr\n[end adviser]",))
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, message_uid,"
            " active, _compressed_summary) VALUES ('s1', 'assistant', ?, 10.9, 'a2', 1, 0)",
            ("[adviser — advisory only]\n- only an injection\n[end adviser]",))
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, message_uid,"
            " active, _compressed_summary) VALUES ('s1', 'user', ?, 10.95, 'u9', 1, 0)",
            ("[adviser-only mode — advisory only]\nPropose the change.\n"
             "[end adviser-only mode]",))
        connection.commit()
    finally:
        connection.close()

    rows = plugin_package.history.read_messages(
        path, "s1", 10.0, 11.0, roles=("user", "assistant", "tool"))
    tool = next(row for row in rows if row["message_uid"] == "t2")
    assert tool["content"] == "pytest: 4 passed"
    assert all("adviser" not in row["content"] for row in rows)


def test_role_filter_can_include_tool_rows_for_the_dialectic_review(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    default = plugin_package.history.read_messages(path, "s1", 9.0, 13.0)
    assert [row["role"] for row in default] == ["user", "assistant"]
    widened = plugin_package.history.read_messages(
        path, "s1", 9.0, 13.0, roles=("user", "assistant", "tool"))
    assert [row["role"] for row in widened] == ["user", "assistant", "tool"]
    assert widened[-1]["tool_name"] == "terminal"


def test_window_is_half_open(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    assert [row["content"] for row in plugin_package.history.read_messages(path, "s1", 10.0, 10.5)] \
        == ["real answer"]
    assert plugin_package.history.read_messages(path, "s1", 12.5, 19.9) == []
    assert [row["content"] for row in plugin_package.history.read_messages(path, "s1", 12.5, 20.0)] == ["outside the window"]


def test_latest_timestamp_ignores_synthetic_and_inactive(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    assert plugin_package.history.latest_ts(path, "s1") == 20.0


def test_profile_name_and_identity_files(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    _state_db(path)
    assert plugin_package.history.profile_name(path, "s1") == "work"
    assert plugin_package.history.profile_name(path, "missing") == ""

    identity, preference = plugin_package.history.identity_paths(tmp_path, "work")
    identity.parent.mkdir(parents=True)
    identity.write_text("---\nsource: review\nreviewer: masia\n---\n\nI am Masía.", encoding="utf-8")
    preference.write_text("---\nsource: review\n---\n\nPrefer evidence.", encoding="utf-8")
    assert plugin_package.history.load_identity(tmp_path, "work") == (
        "I am Masía.", "Prefer evidence.")


def test_identity_profile_is_sanitised(plugin_package, tmp_path):
    identity, preference = plugin_package.history.identity_paths(tmp_path, "../escape/profile")
    assert ".." not in str(identity)
    assert identity.parent.name == "escape_profile"
    assert not identity.exists() and not preference.exists()


def test_long_window_keeps_the_newest_messages(plugin_package, tmp_path):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL, message_uid TEXT, active INTEGER DEFAULT 1,"
            " _compressed_summary INTEGER DEFAULT 0);")
        connection.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, message_uid)"
            " VALUES ('s1', 'user', ?, ?, ?)",
            [(f"message {index}", float(index), f"u{index}") for index in range(1, 451)])
        connection.commit()
    finally:
        connection.close()

    rows = plugin_package.history.read_messages(path, "s1", 0.0, 1_000.0)
    assert len(rows) == 400
    assert rows[0]["content"] == "message 51"
    assert rows[-1]["content"] == "message 450"
