"""Layer worker — job lifecycle, window coverage, merge contract, and publication.

The worker reads its evidence from the in-memory transcript, never from the store: the provider
persists no conversation content.
"""

from __future__ import annotations

import time

import pytest

BASE = "base"
DIALECTIC = "dialectic"
SESSION = "session-1"


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


@pytest.fixture()
def transcript(plugin_package):
    return plugin_package.transcript.TranscriptBuffer()


@pytest.fixture()
def worker(plugin_package, store, transcript):
    """A worker whose model call is captured instead of performed."""
    calls: list[dict] = []

    def complete(task, system, prompt, **kwargs):
        calls.append({"task": task, "system": system, "prompt": prompt})
        if task == plugin_package.worker.DIALECTIC_LAYER_TASK:
            return ("## Adviser Review\n- ran the smoke test\n\n"
                    "## Constraints\n- never edit outside the repo\n\n"
                    "## Preferences\n- act first\n\n"
                    "## Second Brain\n- [tool] pytest -q")
        return ("## Session Summary\n- Ships a Hermes memory plugin from Windows.\n\n"
                "## User Representation\n- [goal] ship the plugin\n\n"
                "## User Peer Card\n- Wants terse answers.\n\n"
                "## AI Self Representation\n- direct\n\n"
                "## AI Identity Card\n- careful with user files")

    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete)
    instance.calls = calls
    return instance


def _dispatch(store, transcript, session, layer, from_ts, to_ts):
    """Queue a job exactly the way the provider does: the edge is registered with the transcript
    first, so a message that shares the stamp is pushed strictly past it."""
    transcript.mark_edge(session, to_ts)
    return store.record_job(layer, session, from_ts, to_ts)


# -- window resolution --------------------------------------------------------

def test_a_turn_buffered_after_the_dispatch_stamp_is_still_reasoned_over(store, transcript, worker):
    """The defect this worker exists for: ``on_turn_start`` stamps ``to_ts`` before the turn
    exists, so the window that counted a turn cannot contain it."""
    transcript.add(SESSION, "user", "deploy on Windows", created_at=100.0)
    transcript.add(SESSION, "assistant", "noted", created_at=100.5)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 99.0)  # stamped one second too early
    assert worker.run_once() == 1
    assert worker.calls, "the window content must reach the model despite the stale stamp"
    assert "deploy on Windows" in worker.calls[0]["prompt"]


def test_content_is_never_reasoned_over_twice(store, transcript, worker):
    transcript.add(SESSION, "user", "first window", created_at=50.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 100.0)
    transcript.add(SESSION, "user", "second window", created_at=150.0)
    _dispatch(store, transcript, SESSION, BASE, 100.0, 140.0)
    assert worker.run_once() == 2
    first, second = (call["prompt"] for call in worker.calls)
    assert "first window" in first and "second window" not in first
    assert "second window" in second and "first window" not in second


def test_the_newest_job_drains_everything_buffered_by_now(store, transcript, worker):
    """No later job exists to inherit the open end, so this one must extend to ``now``."""
    now = time.time()
    transcript.add(SESSION, "user", "written after dispatch", created_at=now - 5)
    _dispatch(store, transcript, SESSION, BASE, 0.0, now - 10)  # window ends before the turn
    assert worker.run_once() == 1
    assert "written after dispatch" in worker.calls[0]["prompt"]
    assert store.last_processed_ts(BASE, SESSION) >= now - 5


def test_empty_window_costs_no_model_call(store, worker):
    store.record_job(BASE, SESSION, from_ts=0.0, to_ts=1.0)
    assert worker.run_once() == 1
    assert worker.calls == []
    assert store.claim_jobs(limit=1) == []          # claimed and finished
    assert store.pending_jobs() == []
    assert store.last_processed_ts(BASE, SESSION) > 0


def test_a_window_already_drained_is_skipped(store, transcript, worker):
    """The content watermark, not the window's own bounds, decides what a job reads."""
    transcript.add(SESSION, "user", "already seen", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    worker.run_once()
    assert len(worker.calls) == 1
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)  # same window queued again
    assert worker.run_once() == 1
    assert len(worker.calls) == 1, "no second model call for content already reasoned over"
    assert store.last_processed_ts(BASE, SESSION) >= 10.0


def test_oldest_job_first_and_one_claim_each(store):
    for index in range(3):
        store.record_job(BASE, SESSION, from_ts=float(index), to_ts=float(index))
    assert [job["id"] for job in store.claim_jobs(limit=2)] == [1, 2]
    assert [job["id"] for job in store.claim_jobs(limit=2)] == [3]


# -- publication --------------------------------------------------------------

def test_base_layer_publishes_one_artifact_per_participant(store, transcript, worker):
    transcript.add(SESSION, "user", "I prefer terse answers", author="Ada", author_id="u-1",
                   created_at=10.0)
    transcript.add(SESSION, "user", "ship it", author="Bo", author_id="u-2", created_at=11.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert worker.run_once() == 1
    assert sorted(state["subject"] for state in store.all_layer_states()) == ["user:u-1", "user:u-2"]
    ada = store.layer_state(BASE, "user:u-1")
    assert ada["version"] == 1 and ada["content"].startswith("## Session Summary")
    assert "user:u-1" in worker.calls[0]["prompt"]


def test_an_anonymous_turn_falls_back_to_the_profile_user(plugin_package, store, transcript):
    def complete(task, system, prompt, **kwargs):
        return ("## Session Summary\n- anonymous\n\n## User Representation\n- [fact] anonymous\n\n"
                "## User Peer Card\n- none\n\n## AI Self Representation\n- none\n\n"
                "## AI Identity Card\n- none")

    transcript.add(SESSION, "user", "no author fields", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete,
                                                user_id="profile-user")
    assert instance.run_once() == 1
    assert store.layer_state(BASE, "user:profile-user") is not None


def test_merge_bumps_the_version_and_feeds_the_previous_artifact_forward(store, transcript, worker):
    transcript.add(SESSION, "user", "first")
    _dispatch(store, transcript, SESSION, BASE, 0.0, time.time())
    worker.run_once()
    first = store.layer_state(BASE, "user:default")

    time.sleep(0.002)  # the next turn arrives a moment later, as a real one does
    tick = time.time()  # the tick stamps the window...
    _dispatch(store, transcript, SESSION, BASE, tick, tick)
    transcript.add(SESSION, "user", "second")  # ...and the turn completes afterwards
    worker.run_once()
    second = store.layer_state(BASE, "user:default")

    assert (first["version"], second["version"]) == (1, 2)
    assert "Wants terse answers" in worker.calls[1]["prompt"], \
        "the merge pass must see the durable previous sections"
    assert "## Session Summary" not in worker.calls[1]["prompt"], \
        "the rolling projection must be recomputed, not fed back"


def test_dialectic_layer_publishes_constraints_preferences_review(plugin_package, store, transcript):
    def complete(task, system, prompt, **kwargs):
        return ("## Adviser Review\n- the last window skipped a smoke test: run one next time\n\n"
                "## Constraints\n- never edit files outside the repo\n\n"
                "## Preferences\n- act first, report briefly\n\n"
                "## Second Brain\n- [tool] pytest -q")

    transcript.add(SESSION, "user", "review this", created_at=10.0)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete,
                                                agent_identity="Masía")
    assert instance.run_once() == 1
    state = store.layer_state(DIALECTIC, "agent:Masía")
    assert state["version"] == 1
    assert state["content"].startswith("## Adviser Review") and "## Second Brain" in state["content"]


def test_an_unusable_artifact_fails_the_job_and_keeps_the_previous_version(plugin_package, store,
                                                                           transcript):
    replies = ["## Session Summary\n- keeps this\n\n## User Representation\n- [fact] this\n\n"
               "## User Peer Card\n- terse\n\n## AI Self Representation\n- direct\n\n"
               "## AI Identity Card\n- careful", "no headings at all"]
    seen: list[str] = []

    def complete(task, system, prompt, **kwargs):
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete)
    transcript.add(SESSION, "user", "first")
    _dispatch(store, transcript, SESSION, BASE, 0.0, time.time())
    instance.run_once()
    good = store.layer_state(BASE, "user:default")
    assert good["content"].startswith("## Session Summary")
    drained = store.last_processed_ts(BASE, SESSION)

    time.sleep(0.002)
    tick = time.time()
    _dispatch(store, transcript, SESSION, BASE, tick, tick)
    transcript.add(SESSION, "user", "second")
    instance.run_once()  # attempt 1: output rejected -> re-queued
    assert store.layer_state(BASE, "user:default") == good
    instance.run_once()  # attempt 2: rejected again -> failed
    assert store.layer_state(BASE, "user:default") == good
    assert store.pending_jobs() == []
    assert store.stats()["failed_jobs"] == 1
    assert store.last_processed_ts(BASE, SESSION) == drained, "a failed window must keep the watermark put"


def test_a_failed_window_is_recovered_by_the_next_job(plugin_package, store, transcript):
    def complete(task, system, prompt, **kwargs):
        return "" if "unusable" in prompt else (
            "## Session Summary\n- ok\n\n## User Representation\n- [fact] ok\n\n"
            "## User Peer Card\n- ok\n\n## AI Self Representation\n- ok\n\n"
            "## AI Identity Card\n- ok")

    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete)
    transcript.add(SESSION, "user", "unusable evidence", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    instance.run_once()
    instance.run_once()  # exhausts the attempts
    assert store.layer_state(BASE, "user:default") is None

    transcript.add(SESSION, "user", "recovered evidence", created_at=30.0)
    _dispatch(store, transcript, SESSION, BASE, 20.0, 40.0)
    assert instance.run_once() == 1
    assert store.layer_state(BASE, "user:default")["content"].startswith("## Session Summary")


# -- lifecycle ----------------------------------------------------------------

def test_disabled_worker_touches_nothing(plugin_package, store, transcript):
    transcript.add(SESSION, "user", "content", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    idle = plugin_package.worker.LayerWorker(store, transcript, enabled=False)
    assert idle.run_once() == 0
    assert len(store.pending_jobs()) == 1
    assert store.all_layer_states() == []


def test_expire_orphans_closes_jobs_whose_content_is_gone(store, transcript, worker):
    """A job that outlives its process can never be drained — it must not be retried."""
    job_id = _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert [job["id"] for job in store.claim_jobs(limit=1)] == [job_id]  # the process died here
    restart = time.time() + 0.001  # this process started just after the job was queued
    assert worker.expire_orphans(before=restart) == 1
    assert worker.run_once() == 0, "an expired job must not be re-queued"
    assert store.pending_jobs() == []
    assert store.all_layer_states() == []
    row = store._conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "expired" and "content unavailable" in row["error"]


def test_catch_up_is_bounded_per_run(plugin_package, store, transcript, worker):
    limit = plugin_package.worker._MAX_JOBS_PER_RUN
    for index in range(limit + 2):
        store.record_job(BASE, SESSION, from_ts=float(index), to_ts=float(index))
    assert worker.run_once() == limit
    assert len(store.pending_jobs()) == 2


# -- prompt/evidence shaping --------------------------------------------------

def test_evidence_keeps_the_newest_messages_when_over_budget(store, transcript, worker):
    for index in range(4):
        transcript.add(SESSION, "user", f"filler {index} " + "z" * 3400, created_at=10.0 + index)
    transcript.add(SESSION, "user", "newest message", created_at=20.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 30.0)
    assert worker.run_once() == 1
    prompt = worker.calls[0]["prompt"]
    assert "newest message" in prompt and "…earlier messages omitted" in prompt


def test_sections_are_validated_in_order(plugin_package):
    normalize = plugin_package.worker._normalize_state
    base = ("## Session Summary\n- a\n\n## User Representation\n- [goal] a\n\n"
            "## User Peer Card\n- a\n\n## AI Self Representation\n- a\n\n"
            "## AI Identity Card\n- a")
    assert normalize(BASE, "## User Peer Card\n- a\n\n## Session Summary\n- a") == ""  # wrong order
    assert normalize(BASE, "## Session Summary\n\n## User Representation") == ""  # no items
    assert normalize(BASE, "") == ""
    # A section may legitimately be empty; one bullet somewhere is the contract.
    empty_peer = base.replace("- a\n\n## AI Self Representation", "\n## AI Self Representation")
    assert normalize(BASE, empty_peer).startswith("## Session Summary")
    assert normalize(BASE, f"```markdown\n{base}\n```").startswith("## Session Summary")
    assert normalize(BASE, f"Sure! Here you go:\n\n{base}").startswith("## Session Summary")


# -- the host model call ------------------------------------------------------
# ``call_model`` is the only place the worker touches the host, so its contract — the task key,
# the request shape, and the response it accepts — is pinned here instead of against a live
# provider (which would be slow, paid and flaky).

class _FakeAuxiliary:
    """Stands in for ``agent.auxiliary_client``: records the request, returns a canned response."""

    def __init__(self, content=None, error=None):
        self.calls: list[dict] = []
        self._content, self._error = content, error

    def call_llm(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        message = type("Message", (), {"content": self._content})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


def _install_fake_aux(plugin_package, monkeypatch, fake):
    import sys
    import types

    module = sys.modules.get("agent.auxiliary_client")
    if module is None:
        monkeypatch.setitem(sys.modules, "agent.auxiliary_client", types.ModuleType("agent.auxiliary_client"))
        module = sys.modules["agent.auxiliary_client"]
    monkeypatch.setattr(module, "call_llm", fake.call_llm, raising=False)


def test_call_model_passes_the_task_key_and_returns_the_text(plugin_package, monkeypatch):
    fake = _FakeAuxiliary(content="## Identity\n- x")
    _install_fake_aux(plugin_package, monkeypatch, fake)
    text = plugin_package.worker.call_model(
        plugin_package.worker.BASE_LAYER_TASK, "SYS", "USER", timeout=12.5, max_tokens=64)
    assert text == "## Identity\n- x"
    call = fake.calls[0]
    assert call["task"] == "minir_base_layer"
    assert [message["role"] for message in call["messages"]] == ["system", "user"]
    assert [message["content"] for message in call["messages"]] == ["SYS", "USER"]
    assert (call["temperature"], call["max_tokens"], call["timeout"]) == (0, 64, 12.5)


def test_call_model_reads_a_content_part_list(plugin_package, monkeypatch):
    fake = _FakeAuxiliary(content=[{"text": "## Identity"}, {"text": "\n- x"}])
    _install_fake_aux(plugin_package, monkeypatch, fake)
    assert plugin_package.worker.call_model("t", "s", "u") == "## Identity\n- x"


def test_call_model_never_raises(plugin_package, monkeypatch):
    """A provider outage, a missing host module or a malformed response must all come back empty."""
    fake = _FakeAuxiliary(error=RuntimeError("provider down"))
    _install_fake_aux(plugin_package, monkeypatch, fake)
    assert plugin_package.worker.call_model("t", "s", "u") == ""
    assert plugin_package.worker.call_model("t", "s", "u") == ""
    assert len(fake.calls) == 2, "a transport failure is not retried inside the call"

    for response in (None, object(), type("R", (), {"choices": []})()):
        assert plugin_package.worker._response_text(response) == ""


def test_call_model_without_the_host_module_returns_empty(plugin_package, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)  # `import x` -> ImportError
    assert plugin_package.worker.call_model("t", "s", "u") == ""


# -- dialectic depth and cold start ------------------------------------------

def test_dialectic_depth_runs_three_passes(plugin_package, store, transcript):
    calls: list[str] = []

    def complete(task, system, prompt, **kwargs):
        calls.append(system)
        return ("## Adviser Review\n- review\n\n## Constraints\n- c\n\n"
                "## Preferences\n- p\n\n## Second Brain\n- [tool] t")

    store.put_layer_state("dialectic", "agent:minir",
                          "## Adviser Review\n- prior\n\n## Constraints\n- c\n\n"
                          "## Preferences\n- p\n\n## Second Brain\n- [tool] t")
    transcript.add(SESSION, "user", "review this", created_at=10.0)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, depth=3, complete=complete)
    assert instance.run_once() == 1
    assert len(calls) == 3, "d1 synthesize, d2 self-audit, d3 reconcile"
    assert "auditing" in calls[1].lower() and "reconciling" in calls[2].lower()
    assert store.layer_state(DIALECTIC, "agent:minir") is not None


def test_cold_start_uses_identity_files_without_messages(plugin_package, store, transcript):
    seen: list[str] = []

    def complete(task, system, prompt, **kwargs):
        seen.append(system)
        return ("## Adviser Review\n- no evidence yet\n\n## Constraints\n- unknown\n\n"
                "## Preferences\n- unknown\n\n## Second Brain\n- unknown")

    instance = plugin_package.worker.LayerWorker(
        store, transcript, identity="I am Masía.", preference="Be terse.",
        depth=1, complete=complete)
    store.record_job(DIALECTIC, SESSION, 0.0, 20.0, turns=0)
    assert instance.run_once() == 1
    assert "no prior dialectic" in seen[0].lower()
    assert store.layer_state(DIALECTIC, "agent:minir") is not None


def test_a_dialectic_audit_failure_keeps_the_initial_draft(plugin_package, store, transcript):
    replies = [
        "## Adviser Review\n- review\n\n## Constraints\n- c\n\n## Preferences\n- p\n\n## Second Brain\n- [tool] t",
        "not a valid artifact",
    ]

    def complete(task, system, prompt, **kwargs):
        return replies[min(len(replies) - 1, 0)] if len(replies) else ""

    instance = plugin_package.worker.LayerWorker(store, transcript, depth=2, complete=complete)
    transcript.add(SESSION, "user", "review this", created_at=10.0)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    assert instance.run_once() == 1
    assert "review" in store.layer_state(DIALECTIC, "agent:minir")["content"]


# -- canonical chunk chain ----------------------------------------------------

def test_base_refresh_appends_a_chunk_and_projects_it_into_the_artifact(store, transcript, worker):
    transcript.add(SESSION, "user", "first window", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert worker.run_once() == 1

    chunks = store.chunk_summaries(SESSION)
    assert len(chunks) == 1
    assert "Ships a Hermes memory plugin" in chunks[0]["content"]
    assert store.newest_conclusion("base.session_summary") is None, \
        "the rolling view must not be a second independent conclusion"

    artifact = store.layer_state(BASE, "user:default")["content"]
    assert "## Session Summary" in artifact
    assert "Ships a Hermes memory plugin" in artifact
    assert "Local chunk" in artifact, "the base section is the rolling chain projection"

    transcript.add(SESSION, "user", "second window", created_at=30.0)
    _dispatch(store, transcript, SESSION, BASE, 20.0, 40.0)
    assert worker.run_once() == 1
    assert len(store.chunk_summaries(SESSION)) == 2
    projection = store.rolling_session_summary(SESSION)
    assert projection.count("Local chunk") == 2


def test_final_summary_supersedes_the_chunk_chain(store, transcript, worker):
    transcript.add(SESSION, "user", "window", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    worker.run_once()
    assert len(store.chunk_summaries(SESSION)) == 1

    store.put_conclusion("session_summary", "summary:" + SESSION, "## Goal\n- done",
                         session_id=SESSION, subject="session", status="active")
    assert store.supersede_chunk_summaries(SESSION) == 1
    assert store.chunk_summaries(SESSION) == []
    projection = store.rolling_session_summary(SESSION)
    assert "Prior summarized context" in projection and "## Goal" in projection


def test_dialectic_prompt_consumes_the_chunk_chain(store, transcript, worker):
    store.add_chunk_summary(SESSION, 1.0, 2.0, "chunk one: tests pass")
    store.add_chunk_summary(SESSION, 2.0, 3.0, "chunk two: tests fail")
    transcript.add(SESSION, "user", "reconcile this", created_at=10.0)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    assert worker.run_once() == 1
    prompt = worker.calls[0]["prompt"]
    assert "LOCAL CHUNK SUMMARIES" in prompt
    assert "chunk one: tests pass" in prompt and "chunk two: tests fail" in prompt
    assert "Reconcile contradictions" in prompt


def test_base_prompt_does_not_feed_the_previous_projection_back(store, transcript, worker):
    store.put_layer_state(
        "base", "user:default",
        "## Session Summary\n- OLD PROJECTION\n\n## User Representation\n- [goal] old\n\n"
        "## User Peer Card\n- durable peer\n\n## AI Self Representation\n- self\n\n"
        "## AI Identity Card\n- identity")
    transcript.add(SESSION, "user", "new delta", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert worker.run_once() == 1
    prompt = worker.calls[0]["prompt"]
    assert "OLD PROJECTION" not in prompt, "the rolling projection must not be fed back"
    assert "durable peer" in prompt, "the other four sections still carry forward"


def test_a_dialectic_job_can_run_from_the_chunk_chain_alone(plugin_package, store, transcript):
    store.add_chunk_summary(SESSION, 1.0, 2.0, "chunk-only evidence")
    calls: list[str] = []

    def complete(task, system, prompt, **kwargs):
        calls.append(prompt)
        return ("## Adviser Review\n- reconciled\n\n## Constraints\n- c\n\n"
                "## Preferences\n- p\n\n## Second Brain\n- [tool] t")

    instance = plugin_package.worker.LayerWorker(store, transcript, depth=1, complete=complete)
    store.record_job(DIALECTIC, SESSION, 0.0, 20.0)
    assert instance.run_once() == 1, "chunks alone must drive the dialectic pass"
    assert "chunk-only evidence" in calls[0]
