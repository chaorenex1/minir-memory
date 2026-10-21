"""In-memory transcript — bounds, windows, and the guarantees the worker relies on.

The persistence half is the audit trail of doc/base-dialectic-layers-draft.md §4.9: a host
session per file, a plugin layer session under its layer directory, a debounced atomic rewrite,
a second shard past 2000 messages, and prune as the only deleter.
"""

from __future__ import annotations

import json
import os
import time

import pytest


@pytest.fixture()
def persisted(plugin_package, tmp_path):
    """Factory: ``make(**kwargs)`` -> a buffer writing under ``tmp_path``.

    Every buffer is closed at teardown, so its writer thread is joined and cannot outlive the
    test's temporary directory.
    """
    buffers = []

    def _make(**kwargs):
        buffer = plugin_package.transcript.TranscriptBuffer(persist_dir=tmp_path, **kwargs)
        buffers.append(buffer)
        return buffer

    yield _make, tmp_path
    for buffer in buffers:
        buffer.close()


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


# -- persistence: the audit trail (§4.9) --------------------------------------

CONTRACT_KEYS = {"session_id", "layer", "started_at", "updated_at", "model", "provider",
                 "toolsets", "system_message", "messages"}


def test_a_host_session_is_one_json_file_with_the_contract_keys(persisted):
    make, root = persisted
    buffer = make()

    buffer.add_turn("session-1", "deploy it", "done", author="Ada", author_id="u-1",
                    created_at=10.0)
    assert buffer.flush("session-1") is True

    data = json.loads((root / "session-1.json").read_text(encoding="utf-8"))
    assert set(data) == CONTRACT_KEYS
    assert data["session_id"] == "session-1"
    assert data["layer"] == "host"
    assert data["started_at"] == 10.0
    assert data["updated_at"] >= 10.0
    assert (data["model"], data["provider"], data["toolsets"], data["system_message"]) \
        == ("", "", [], "")
    assert [(row["role"], row["content"], row["author_id"]) for row in data["messages"]] \
        == [("user", "deploy it", "u-1"), ("assistant", "done", "")]
    assert not list(root.glob("*.tmp"))              # temp + os.replace leaves nothing behind


def test_a_plugin_layer_session_lands_under_its_layer_directory(persisted):
    make, root = persisted
    buffer = make()

    buffer.record_aux("dialectic", "job-7-2", [{"role": "assistant", "content": "critique"}],
                      model="aux-model", provider="deepseek", toolsets=("file", "web"),
                      system_message="be blunt")
    assert buffer.flush() is True

    data = json.loads((root / "dialectic" / "job-7-2.json").read_text(encoding="utf-8"))
    assert set(data) == CONTRACT_KEYS
    assert (data["session_id"], data["layer"]) == ("job-7-2", "dialectic")
    assert (data["model"], data["provider"], data["toolsets"], data["system_message"]) \
        == ("aux-model", "deepseek", ["file", "web"], "be blunt")
    assert data["messages"] == [{"role": "assistant", "content": "critique"}]


def test_flush_aux_waits_for_one_custom_agent_snapshot(persisted):
    make, root = persisted
    buffer = make()

    buffer.record_aux("researcher", "layer-1",
                      [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
                      model="m", provider="p", toolsets=("file",), system_message="sys")
    assert buffer.flush_aux("researcher", "layer-1") is True

    path = buffer.path_for_aux("researcher", "layer-1")
    assert path == root / "researcher" / "layer-1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["layer"], data["session_id"]) == ("researcher", "layer-1")
    assert [row["role"] for row in data["messages"]] == ["user", "assistant"]


def test_a_hostile_agent_name_cannot_escape_the_persist_directory(persisted):
    make, root = persisted
    buffer = make()
    path = buffer.path_for_aux("../../evil", "layer-1")
    assert path is not None and path.parent == root / "evil"


def test_a_hostile_session_id_cannot_escape_the_persist_directory(persisted):
    make, root = persisted
    buffer = make()

    buffer.record_aux("base", "../escape", [{"role": "user", "content": "x"}])
    assert buffer.flush() is True

    assert (root / "base" / "escape.json").exists()
    assert not (root.parent / "escape.json").exists()


def test_enqueue_never_blocks_the_turn_path(persisted):
    """The writer is busy on every message and the caller still stays far under 5 ms."""
    make, _root = persisted
    buffer = make(debounce_messages=1, debounce_seconds=0.0)

    worst = 0.0
    for index in range(200):
        started = time.perf_counter()
        buffer.add("hot", "user", f"message {index}", created_at=float(index))
        worst = max(worst, time.perf_counter() - started)
    assert worst < 0.005, f"slowest add took {worst * 1000:.2f} ms"


def test_past_two_thousand_messages_the_overflow_is_a_second_shard(persisted):
    make, root = persisted
    # The queue is 2000 deep by default too; give it room so this test exercises the file
    # shard rather than the (separately specified) drop-the-oldest queue policy.
    buffer = make(debounce_messages=2000, queue_max=4000)

    for index in range(2001):
        buffer.add("long", "user", f"m{index}", created_at=float(index))
    assert buffer.flush("long") is True
    assert buffer.dropped == 0

    first = json.loads((root / "long.json").read_text(encoding="utf-8"))
    second = json.loads((root / "long.part2.json").read_text(encoding="utf-8"))
    assert len(first["messages"]) == 2000            # already-written content is not truncated
    assert first["messages"][0]["content"] == "m0"
    assert len(second["messages"]) == 1
    assert second["messages"][0]["content"] == "m2000"


def test_the_debounce_holds_until_flush_ends_it(persisted):
    make, root = persisted
    buffer = make()                                  # 30 s / 50 messages

    buffer.add("quiet", "user", "one", created_at=1.0)
    time.sleep(0.2)
    assert not (root / "quiet.json").exists()        # still inside the debounce window

    assert buffer.flush("quiet") is True
    data = json.loads((root / "quiet.json").read_text(encoding="utf-8"))
    assert [row["content"] for row in data["messages"]] == ["one"]


def test_fifty_messages_write_without_a_flush(persisted):
    make, root = persisted
    buffer = make(debounce_seconds=30.0, debounce_messages=50)

    for index in range(50):
        buffer.add("burst", "user", f"m{index}", created_at=float(index))

    deadline = time.time() + 5.0
    while time.time() < deadline and not (root / "burst.json").exists():
        time.sleep(0.01)
    data = json.loads((root / "burst.json").read_text(encoding="utf-8"))
    assert len(data["messages"]) == 50


def test_close_flushes_what_is_still_queued(persisted):
    make, root = persisted
    buffer = make()

    buffer.add("last", "user", "unsaved", created_at=1.0)
    assert not (root / "last.json").exists()
    buffer.close()
    buffer.close()                                   # idempotent
    data = json.loads((root / "last.json").read_text(encoding="utf-8"))
    assert data["messages"][0]["content"] == "unsaved"


def test_a_full_queue_drops_the_oldest_and_counts_it(persisted, monkeypatch):
    make, _root = persisted
    buffer = make(queue_max=8)
    monkeypatch.setattr(buffer, "_drain_locked", lambda *args, **kwargs: None)

    for index in range(20):
        buffer.add("q", "user", f"m{index}", created_at=float(index))
    assert buffer.dropped == 12


def test_prune_deletes_only_expired_files(plugin_package, persisted):
    make, root = persisted
    assert plugin_package.transcript.DEFAULT_RETENTION_SECONDS == 30 * 24 * 60 * 60
    buffer = make()

    buffer.add("old", "user", "old", created_at=1.0)
    buffer.add("fresh", "user", "fresh", created_at=1.0)
    buffer.record_aux("base", "old-aux", [{"role": "user", "content": "x"}])
    assert buffer.flush() is True

    expired = [root / "old.json", root / "base" / "old-aux.json"]
    for path in expired:
        os.utime(path, (1_000.0, 1_000.0))

    assert buffer.prune(plugin_package.transcript.DEFAULT_RETENTION_SECONDS) == 2
    assert all(not path.exists() for path in expired)
    assert (root / "fresh.json").exists()
    assert buffer.prune() == 0                       # the default is the 30-day window
