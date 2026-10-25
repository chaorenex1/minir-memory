"""Transcript event store: JSONL rows, dedupe, reads, cache replay and process locks.

The in-memory window keeps its own contract (bounded, half-open timestamps); the file is the
permanent record. These tests are the acceptance table of doc/transcript-jsonl-draft.md §11.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture()
def persisted(plugin_package, tmp_path):
    """Factory: ``make(**kwargs)`` -> a buffer writing under ``tmp_path``; every one is closed."""
    buffers = []

    def _make(**kwargs):
        kwargs.setdefault("persist_dir", tmp_path / "sessions")
        kwargs.setdefault("run_dir", tmp_path / "run")
        kwargs.setdefault("profile", "default")
        buffer = plugin_package.transcript.TranscriptBuffer(**kwargs)
        buffers.append(buffer)
        return buffer

    yield _make, tmp_path
    for buffer in buffers:
        buffer.close()


def _rows(buffer, session="s1", **kwargs):
    return buffer.read_events("default", "host", session, **kwargs)["rows"]


def _conversation(turn: int = 1) -> list[dict]:
    return [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": f"turn {turn}: deploy it"},
        {"role": "assistant", "content": "reading the manifest",
         "tool_calls": [{"id": f"c{turn}", "function": {"name": "exec",
                                                        "arguments": '{"cmd":"ls"}'}}],
         "reasoning_content": "check the manifest first"},
        {"role": "tool", "tool_call_id": f"c{turn}", "name": "exec", "content": "a" * 6000},
    ]


# -- rows ---------------------------------------------------------------------


def test_one_event_per_line_with_all_five_roles(persisted):
    make, root = persisted
    buffer = make()
    buffer.ingest("host", "s1", _conversation())
    assert buffer.flush() is True
    path = root / "sessions" / "default" / "host" / "s1.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert path.read_text(encoding="utf-8").endswith("\n")
    rows = [json.loads(line) for line in lines]
    assert [row["role"] for row in rows] == ["system", "user", "assistant", "reasoning", "tool"]
    assert [row["seq"] for row in rows] == [1, 2, 3, 4, 5]
    assert all(row["kind"] == "msg" and row["v"] == 1 for row in rows)
    assert all(row["profile"] == "default" and row["layer"] == "host"
               and row["session_id"] == "s1" for row in rows)
    # The 6000-character tool result is on disk in full: no flatten, no 4000 cut.
    assert len(rows[4]["result"]) == 6000
    assert rows[1]["text"] == "turn 1: deploy it"
    assert rows[2]["tool_calls"][0]["name"] == "exec"
    assert rows[3]["text"] == "check the manifest first"


def test_two_syncs_do_not_repeat_the_first_turn(persisted):
    make, _root = persisted
    buffer = make()
    first = _conversation(1)
    second = _conversation(2)
    assert buffer.ingest("host", "s1", first) == 5
    assert buffer.ingest("host", "s1", first) == 0  # the same slice twice is a no-op
    assert buffer.ingest("host", "s1", first + second) == 5
    rows = _rows(buffer)
    assert [row["text"] for row in rows if row["role"] == "user"] == [
        "turn 1: deploy it", "turn 2: deploy it"]


def test_a_lost_anchor_writes_a_gap_then_the_slice(persisted):
    make, _root = persisted
    buffer = make()
    buffer.ingest("host", "s1", _conversation(1))
    # Compaction rewrote the conversation: none of the old fingerprints survive.
    rewritten = [{"role": "system", "content": "summary of earlier work"},
                 {"role": "user", "content": "continue"}]
    assert buffer.ingest("host", "s1", rewritten) == 2
    gap = [row for row in _rows(buffer) if row["kind"] == "gap"]
    assert len(gap) == 1 and gap[0]["reason"] == "compaction"
    assert "summary of earlier work" in [row.get("text") for row in _rows(buffer)]


def test_a_stale_in_memory_anchor_is_recovered_from_disk(persisted):
    """The anchor in memory can be behind the file; the file's tail is the authority."""
    make, _root = persisted
    buffer = make()
    first = _conversation(1)
    assert buffer.ingest("host", "s1", first) == 5
    assert buffer.flush() is True
    buffer._streams[("default", "host", "s1")].last_seen = {
        "fp": "gone", "fps": ["gone"], "role": "user", "ts": 0.0}
    assert buffer.ingest("host", "s1", first) == 0
    assert len([row for row in _rows(buffer) if row.get("kind") == "msg"]) == 5


def test_a_prefix_replay_appends_only_the_rows_the_file_lacks(persisted):
    """A history that starts inside the file matches no anchor: do not replay it wholesale."""
    make, _root = persisted
    buffer = make()
    first = _conversation(1)
    buffer.ingest("host", "s1", first)
    assert buffer.flush() is True
    buffer._streams[("default", "host", "s1")].last_seen = {
        "fp": "gone", "fps": ["gone"], "role": "user", "ts": 0.0}
    replay = first[:3] + [{"role": "user", "content": "turn 2: deploy it"}]
    assert buffer.ingest("host", "s1", replay) == 1
    users = [row.get("text") for row in _rows(buffer) if row.get("role") == "user"]
    assert users == ["turn 1: deploy it", "turn 2: deploy it"]
    gap = [row for row in _rows(buffer) if row.get("kind") == "gap"]
    assert gap and gap[-1]["reason"] == "compaction" and gap[-1]["dropped"] == 3


def test_a_resumed_process_appends_only_the_new_tail(persisted):
    make, _root = persisted
    first = make()
    first.bind("s1")
    first.ingest("host", "s1", _conversation(1))
    first.close()

    second = make()
    second.bind("s1")
    rows = _rows(second)
    assert len(rows) == 5  # replayed from the last_cache marker, not re-read as new
    assert second.ingest("host", "s1", _conversation(1) + _conversation(2)) == 5
    assert len(_rows(second)) == 10


# -- reads --------------------------------------------------------------------


def test_cursor_rolling_and_cross_part_reads(persisted):
    make, _root = persisted
    buffer = make(events_per_part=2)
    for index in range(1, 6):
        buffer.append_event("host", "s1", "user", text=f"m{index}", profile="default")
    assert buffer.flush() is True

    assert [row["text"] for row in _rows(buffer, cursor=2)] == ["m3", "m4", "m5"]
    rolling = buffer.read_events("default", "host", "s1", last_n=2)
    assert [row["text"] for row in rolling["rows"]] == ["m4", "m5"]
    assert rolling["dropped_before"] == 3
    assert rolling["next_cursor"] == 5

    paged = buffer.read_events("default", "host", "s1", limit=2, page=1)
    assert [row["text"] for row in paged["rows"]] == ["m3", "m4"]
    assert paged["truncated"] is True
    parts = sorted(path.name for path in (persisted[1] / "sessions" / "default" / "host").glob("*.jsonl"))
    assert parts == ["s1.jsonl", "s1.part2.jsonl", "s1.part3.jsonl"]


def test_a_truncated_final_line_is_skipped_not_raised(persisted):
    make, root = persisted
    buffer = make()
    buffer.append_event("host", "s1", "user", text="kept")
    assert buffer.flush() is True
    path = root / "sessions" / "default" / "host" / "s1.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "seq": 99, "role": "ass')
    rows = _rows(buffer)
    assert [row["text"] for row in rows] == ["kept"]
    # The next append survives the fragment: the writer keeps one line per event.
    buffer.append_event("host", "s1", "assistant", text="after")
    assert buffer.flush() is True
    content = path.read_text(encoding="utf-8")
    assert content.count("kept") == 1 and content.count("after") == 1
    assert json.loads(content.splitlines()[-1])["text"] == "after"


def test_evicted_rows_are_read_from_disk_and_reported(persisted):
    make, _root = persisted
    buffer = make(buffer_events_per_stream=2)
    for index in range(4):
        buffer.append_event("host", "s1", "user", text=f"m{index}", profile="default")
    assert buffer.flush() is True
    result = buffer.read_events("default", "host", "s1", limit=100, kinds=("msg",))
    assert [row["text"] for row in result["rows"]] == ["m0", "m1", "m2", "m3"]
    assert result["coverage"] == (3, 4)
    assert result["evicted_before"] == 2
    assert result["source"] == "mixed"


def test_stream_count_and_byte_bounds_never_evict_the_pinned_stream(persisted):
    make, _root = persisted
    buffer = make(max_sessions=2, buffer_events_per_stream=100, buffer_bytes=1024 * 1024)
    buffer.bind("s1")
    buffer.append_event("host", "s1", "user", text="pinned", profile="default")
    buffer.append_event("host", "s2", "user", text="two", profile="default")
    buffer.append_event("host", "s3", "user", text="three", profile="default")
    assert len(buffer.stats()["streams"]) == 2
    assert buffer.read_events("default", "host", "s1", last_n=1)["rows"][0]["text"] == "pinned"

    tiny = make(buffer_events_per_stream=100, buffer_bytes=512)
    tiny.bind("s9")
    tiny.append_event("host", "s9", "user", text="P" * 100, profile="default")
    for index in range(5):
        tiny.append_event("host", f"b{index}", "user", text="x" * 300, profile="default")
    sessions = {row["session_id"] for row in tiny.stats()["streams"]}
    assert "s9" in sessions                 # the pinned session is never evicted
    assert "b0" not in sessions             # byte pressure evicted the oldest stream whole
    assert tiny.stats()["bytes"] > 0        # disk bytes are reported after cache eviction


# -- cache replay and multi-process -------------------------------------------


def test_first_process_replays_only_the_marked_range(persisted):
    make, _root = persisted
    first = make(buffer_events_per_stream=3)
    first.bind("s1")
    for index in range(5):
        first.append_event("host", "s1", "user", text=f"m{index}", profile="default")
    first.close()
    assert [row["seq"] for row in _rows(first, kinds=("msg",))][-1] == 5

    second = make()
    assert second.bind("s1") is True
    replayed = _rows(second, kinds=("msg",))
    assert [row["text"] for row in replayed] == ["m2", "m3", "m4"]
    assert second.read_events("default", "host", "s1", last_n=1, kinds=("msg",))["source"] == "buffer"
    # The old rows are still readable from the file, and the answer says so.
    full = second.read_events("default", "host", "s1", limit=100, kinds=("msg",))
    assert full["source"] == "mixed" and len(full["rows"]) == 5
    # The marker row keeps its seq; a post-replay append must not reuse it.
    assert second.append_event("host", "s1", "user", text="m5", profile="default")["seq"] == 7


def test_a_second_live_process_does_not_rebuild(persisted):
    make, _root = persisted
    first = make()
    first.bind("s1")
    first.append_event("host", "s1", "user", text="one", profile="default")
    second = make()
    assert second.bind("s1") is False
    assert first.flush() is True
    assert second.read_events("default", "host", "s1", limit=100)["rows"][0]["text"] == "one"


def test_each_live_process_marks_its_own_streams(persisted):
    make, _root = persisted
    first = make()
    second = make()
    first.bind("s1")
    second.bind("s2")
    first.append_event("host", "s1", "user", text="one", profile="default")
    second.append_event("host", "s2", "user", text="two", profile="default")
    assert first.flush() is True and second.flush() is True
    second.close()
    first.close()  # last: its own marker, plus a final fsync sweep

    third = make()
    assert third.bind("s1") is True
    assert [row["text"] for row in _rows(third, session="s1", kinds=("msg",))] == ["one"]
    assert [row["text"] for row in _rows(third, session="s2", kinds=("msg",))] == ["two"]


def test_rebuild_takes_the_newest_marker_of_each_stream(persisted):
    make, _root = persisted
    first = make()
    first.bind("s1")
    first.append_event("host", "s1", "user", text="old", profile="default")
    first.close()

    second = make()
    second.bind("s1")
    second.append_event("host", "s1", "user", text="new", profile="default")
    second.close()

    third = make()
    assert third.bind("s1") is True
    # The newest marker covers this process's own writes; the earlier row stays on disk and is
    # filled by a paged read, which reports where it came from.
    assert [row["text"] for row in _rows(third, kinds=("msg",))] == ["new"]
    full = third.read_events("default", "host", "s1", limit=100, kinds=("msg",))
    assert [row["text"] for row in full["rows"]] == ["old", "new"]
    assert full["source"] == "mixed"


def test_a_crash_leaves_readable_rows_and_no_replay(persisted):
    make, root = persisted
    crashed = make()
    crashed.bind("s1")
    crashed.append_event("host", "s1", "user", text="before crash", profile="default")
    assert crashed.flush() is True
    # Simulate a kill: release the OS lock, write no marker, stop the writer without close().
    crashed._process.release()
    crashed._process = None
    crashed._closing = True

    restarted = make()
    assert restarted.bind("s1") is True
    # No last_cache marker: the in-memory cache is empty, and the answer says so.
    cached = restarted.read_events("default", "host", "s1", last_n=1)
    assert cached["coverage"] == (0, 0)
    disk = restarted.read_events("default", "host", "s1", limit=100)
    assert [row["text"] for row in disk["rows"]] == ["before crash"]
    assert disk["source"] == "disk"


def test_legacy_json_files_are_not_read_migrated_or_deleted(persisted):
    make, root = persisted
    buffer = make()
    legacy = root / "sessions" / "s1.json"
    legacy.write_text(json.dumps({"session_id": "s1", "messages": [
        {"role": "user", "content": "old"}]}), encoding="utf-8")
    buffer.bind("s1")
    assert buffer.read_events("default", "host", "s1", limit=100)["rows"] == []
    assert buffer.read_aux("host", "s1") == []
    assert legacy.exists() and json.loads(legacy.read_text(encoding="utf-8"))["session_id"] == "s1"


# -- aux streams ---------------------------------------------------------------


def test_record_aux_appends_deduped_and_read_aux_round_trips(persisted):
    make, root = persisted
    buffer = make()
    conversation = [{"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"}]
    buffer.record_aux("adviser", "s1--quality", conversation, model="m", provider="p",
                      toolsets=("file",), system_message="sys",
                      usage={"total_tokens": 1234})
    buffer.record_aux("adviser", "s1--quality", conversation + [
        {"role": "user", "content": "follow-up"}], usage={"total_tokens": 99})
    assert buffer.flush_aux("adviser", "s1--quality") is True

    assert buffer.path_for_aux("adviser", "s1--quality") == (
        root / "sessions" / "default" / "adviser" / "s1--quality.jsonl")
    rows = buffer.read_all("default", "adviser", "s1--quality")
    assert [row.get("text") for row in rows
            if row.get("kind") == "msg" and row.get("role") != "system"] == [
        "q", "a", "follow-up"]
    assert [row for row in rows
            if row.get("role") == "system" and row.get("text") == "sys"]
    assert [row["content"] for row in buffer.read_aux("adviser", "s1--quality")] == [
        "q", "a", "follow-up"]
    run_ends = [row for row in rows if row["kind"] == "run_end"]
    assert run_ends[0]["usage"] == {"total_tokens": 1234}


def test_a_hostile_layer_name_cannot_escape_the_persist_directory(persisted):
    make, root = persisted
    buffer = make()
    path = buffer.path_for_aux("../../evil", "layer-1")
    assert path is not None and path.parent == root / "sessions" / "default" / "evil"
    buffer.record_aux("../../evil", "layer-1", [{"role": "user", "content": "x"}])
    assert buffer.flush() is True
    assert (root / "sessions" / "default" / "evil" / "layer-1.jsonl").exists()
    assert not (root / "sessions" / "evil.jsonl").exists()


# -- window (unchanged contract) ----------------------------------------------


def test_window_is_half_open_and_ordered(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer()
    buffer.add("s", "user", "first", created_at=10.0)
    buffer.add("s", "assistant", "second", created_at=20.0)
    buffer.add("s", "user", "third", created_at=30.0)
    assert [row["content"] for row in buffer.window("s", 10.0, 30.0)] == ["second", "third"]
    assert buffer.window("s", 30.0, 40.0) == []


def test_message_count_and_character_budgets_bound_the_window(plugin_package):
    buffer = plugin_package.transcript.TranscriptBuffer(max_messages=3, max_chars=25)
    for index in range(6):
        buffer.add("s", "user", f"message {index}", created_at=float(index))
    assert len(buffer.messages("s")) <= 3
    buffer.clear()
    buffer.add("s", "user", "a" * 20, created_at=1.0)
    buffer.add("s", "user", "b" * 20, created_at=2.0)
    assert [row["content"][0] for row in buffer.messages("s")] == ["b"]
