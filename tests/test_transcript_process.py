"""Process locks: first after a clean shutdown, last at close, stale files are reclaimed."""

from __future__ import annotations

from pathlib import Path


def test_the_first_process_is_alone_and_the_last_is_reported(tmp_path, plugin_package):
    locks = plugin_package.transcript_process

    first = locks.ProcessLock(tmp_path / "run")
    assert first.start() is True
    second = locks.ProcessLock(tmp_path / "run")
    assert second.start() is False  # a live sibling holds its lock

    assert first.finish() is False  # the sibling is still alive
    assert second.finish() is True   # nobody remains, so this one finalses
    assert list((tmp_path / "run").glob("*.lock")) == []


def test_a_stale_lock_file_is_removed_not_treated_as_live(tmp_path, plugin_package):
    locks = plugin_package.transcript_process
    run = tmp_path / "run"
    run.mkdir()
    stale = run / "999999-abcdef.lock"
    stale.write_bytes(b"0")  # a crashed process: the file stayed, the OS lock did not

    lock = locks.ProcessLock(run)
    assert lock.start() is True
    assert not stale.exists()
    lock.finish()
    assert not lock._path.exists()


def test_a_stream_lock_serialises_and_survives_release(tmp_path, plugin_package):
    locks = plugin_package.transcript_process
    path = tmp_path / "stream.lock"
    with locks.StreamLock(path):
        assert path.exists()
    with locks.StreamLock(path):
        assert path.exists()
    assert isinstance(path, Path)


def test_the_buffer_creates_one_lock_per_live_process(plugin_package, tmp_path):
    first = plugin_package.transcript.TranscriptBuffer(
        persist_dir=tmp_path / "sessions", run_dir=tmp_path / "run", profile="default")
    second = plugin_package.transcript.TranscriptBuffer(
        persist_dir=tmp_path / "sessions", run_dir=tmp_path / "run", profile="default")
    try:
        assert first.bind("s1") is True
        assert second.bind("s1") is False
        locks = list((tmp_path / "run").glob("*.lock"))
        assert len(locks) >= 2  # one per process, plus any stream lock
    finally:
        second.close()
        first.close()
    assert list((tmp_path / "run").glob("*.lock")) == []
