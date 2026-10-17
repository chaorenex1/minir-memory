"""In-memory transcript — bounds, windows, and the guarantees the worker relies on."""

from __future__ import annotations

import time


def test_window_is_half_open_and_ordered(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.add("s", "user", "first", created_at=10.0)
    buffer.add("s", "assistant", "second", created_at=20.0)
    buffer.add("s", "user", "third", created_at=30.0)
    assert [row["content"] for row in buffer.window("s", 10.0, 30.0)] == ["second", "third"]
    assert [row["content"] for row in buffer.window("s", 10.0, 20.0)] == ["second"]
    assert buffer.window("s", 30.0, 40.0) == []
    assert buffer.window("other", 0.0, 100.0) == []


def test_add_turn_keeps_the_pair_with_one_timestamp(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.add_turn("s", "deploy it", "done", author="Ada", author_id="u-1", created_at=5.0)
    rows = buffer.messages("s")
    assert [(row["role"], row["content"]) for row in rows] == [("user", "deploy it"),
                                                              ("assistant", "done")]
    assert rows[0]["author"] == "Ada" and rows[0]["author_id"] == "u-1" and rows[1]["author"] == ""


def test_blank_content_is_ignored(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.add("s", "assistant", "   \n  ")
    assert buffer.messages("s") == []


def test_has_after_tracks_the_newest_message(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer()
    assert buffer.has_after("s", 0.0) is False
    buffer.add("s", "user", "hello there", created_at=10.0)
    assert buffer.has_after("s", 5.0) is True
    assert buffer.has_after("s", 10.0) is False


def test_message_count_is_bounded_per_session(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer(max_messages=3)
    for index in range(6):
        buffer.add("s", "user", f"message {index}", created_at=float(index))
    assert [row["content"] for row in buffer.messages("s")] == ["message 3", "message 4", "message 5"]


def test_character_budget_evicts_the_oldest(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer(max_chars=25)
    buffer.add("s", "user", "a" * 20, created_at=1.0)
    buffer.add("s", "user", "b" * 20, created_at=2.0)
    assert [row["content"][0] for row in buffer.messages("s")] == ["b"]


def test_only_the_newest_sessions_are_kept(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer(max_sessions=2)
    buffer.add("old", "user", "one", created_at=1.0)
    buffer.add("mid", "user", "two", created_at=2.0)
    buffer.add("new", "user", "three", created_at=3.0)
    assert buffer.messages("old") == []
    assert [row["content"] for row in buffer.messages("mid")] == ["two"]
    assert [row["content"] for row in buffer.messages("new")] == ["three"]


def test_extend_carries_messages_to_another_session(plugin_package):
    """/branch: the branch inherits the conversation, timestamps and authors included."""
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.add("parent", "user", "original", author="Ada", author_id="u-1", created_at=7.0)
    buffer.extend("branch", buffer.messages("parent"))
    assert [(row["content"], row["author_id"], row["created_at"]) for row in buffer.messages("branch")] \
        == [("original", "u-1", 7.0)]


def test_clear_drops_one_session_or_all(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.add("a", "user", "one", created_at=1.0)
    buffer.add("b", "user", "two", created_at=2.0)
    buffer.clear("a")
    assert buffer.messages("a") == [] and buffer.messages("b") != []
    buffer.clear()
    assert buffer.messages("b") == []


def test_the_process_does_not_share_content_across_sessions(plugin_package):
    """Sessions are keyed, so one session's window can never leak into another's."""
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.add("a", "user", "secret from a", created_at=1.0)
    assert buffer.window("b", 0.0, time.time() + 1) == []
