"""Layer worker — job lifecycle, window coverage, merge contract, and publication.

The worker reads its evidence from the in-memory transcript, never from the store: the provider
persists no conversation content.
"""

from __future__ import annotations

import json
import time

import pytest

BASE = "base"
DIALECTIC = "dialectic"
SESSION = "session-1"


def _base_json(*, summary=None, user=None, peer=None, ai_self=None, identity=None,
               markers=None):
    """A full six-key base reply; each staged pass reads only its own keys."""
    return json.dumps({
        "session_chunk_summary": summary or ["Ships a Hermes memory plugin from Windows."],
        "user_representation": user or ["[goal] ship the plugin"],
        "user_peer_card": peer or ["Wants terse answers."],
        "ai_self_representation": ai_self or ["direct"],
        "ai_identity_card": identity or ["careful with user files"],
        "value_markers": markers or {
            "entities": ["Hermes memory plugin"],
            "trajectories": [],
            "experiences": [],
            "tools": ["pytest"],
            "projects": ["minir-memory"],
        },
    })


def _dialectic_json(*, user=None, peer=None, ai_self=None, identity=None, summary=None,
                    advices=None, markers=None, changed=False, fields=None):
    """A full eight-key dialectic reply; the default carries one actionable concern."""
    return json.dumps({
        "user_representation": user or ["[goal] ship the plugin"],
        "user_peer_card": peer or ["Wants terse answers."],
        "ai_self_representation": ai_self or ["[done] ran the smoke test"],
        "ai_identity_card": identity or ["careful with user files"],
        "session_chunk_summary": summary or ["[done] ships a Hermes memory plugin"],
        "advices": advices if advices is not None else [{
            "severity": "concern", "target": "user_peer_card",
            "text": "run a smoke test before shipping",
            "decision": "adopt the smoke test as a standing check",
            "evidence": ["msg-1"],
        }],
        "value_markers": markers or {
            "entities": ["Hermes memory plugin"], "trajectories": [],
            "experiences": [], "tools": ["pytest"], "projects": ["minir-memory"],
        },
        "base_changed": {"changed": bool(changed), "fields": list(fields or [])},
    })


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
            return _dialectic_json()
        return _base_json()

    instance = plugin_package.worker.LayerWorker(
        store, transcript, complete=complete, enqueue_dialectic=False)
    instance.calls = calls
    return instance


def _dispatch(store, transcript, session, layer, from_ts, to_ts, turns=0):
    """Queue a job exactly the way the provider does: the edge is registered with the transcript
    first, so a message that shares the stamp is pushed strictly past it."""
    transcript.mark_edge(session, to_ts)
    return store.record_job(layer, session, from_ts, to_ts, turns=turns)


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
    row = store.latest_base_refresh(SESSION, "user:default")
    assert (row["window_from_turn"], row["window_to_turn"]) == (1, 1), \
        "the turn range follows the content the job actually drained, not the dispatch turn"


def test_content_is_never_reasoned_over_twice(store, transcript, worker):
    transcript.add(SESSION, "user", "first window", created_at=50.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 100.0)
    transcript.add(SESSION, "user", "second window", created_at=150.0)
    _dispatch(store, transcript, SESSION, BASE, 100.0, 140.0)
    assert worker.run_once() == 2
    assert len(worker.calls) == 6, "three staged passes per base refresh"
    first = worker.calls[0]["prompt"]
    second = worker.calls[3]["prompt"]
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
    first = _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    worker.run_once()
    assert len(worker.calls) == 3, "one staged refresh = three passes"
    assert _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0) == first, \
        "one base window has one job"
    assert worker.run_once() == 0
    assert len(worker.calls) == 3, "no second model call for content already reasoned over"
    assert store.last_processed_ts(BASE, SESSION) >= 10.0


def test_one_base_job_per_window(store):
    first = store.record_job("base", SESSION, 0.0, 10.0)
    assert store.record_job("base", SESSION, 0.0, 10.0) == first
    assert len(store.pending_jobs()) == 1

    job = store.claim_jobs(limit=1)[0]
    store.complete_job(job["id"], 10.0)
    assert store.record_job("base", SESSION, 0.0, 10.0) == first
    assert store.pending_jobs() == []
    assert store.record_job("base", SESSION, 10.0, 20.0) != first
    assert len(store.pending_jobs()) == 1


def test_oldest_job_first_and_one_claim_each(store):
    for index in range(3):
        store.record_job(BASE, SESSION, from_ts=float(index), to_ts=float(index))
    assert [job["id"] for job in store.claim_jobs(limit=2)] == [1, 2]
    assert [job["id"] for job in store.claim_jobs(limit=2)] == [3]


# -- publication --------------------------------------------------------------

def test_base_layer_publishes_one_artifact_for_the_profile_user(store, transcript, worker):
    transcript.add(SESSION, "user", "I prefer terse answers", author="Ada", author_id="u-1",
                   created_at=10.0)
    transcript.add(SESSION, "user", "ship it", author="Bo", author_id="u-2", created_at=11.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert worker.run_once() == 1
    states = store.all_layer_states()
    assert [state["subject"] for state in states] == ["user:default"]
    state = store.layer_state(BASE, "user:default")
    assert state["version"] == 1 and state["content"].startswith("## Session Summary")
    assert "user:default" in worker.calls[0]["prompt"]
    assert "user (Ada)" in worker.calls[0]["prompt"]
    assert "user (Bo)" in worker.calls[0]["prompt"]


def test_an_anonymous_turn_falls_back_to_the_profile_user(plugin_package, store, transcript):
    def complete(task, system, prompt, **kwargs):
        return _base_json(summary=["anonymous"], user=["[fact] anonymous"], peer=["none"],
                          ai_self=["none"], identity=["none"],
                          markers={"entities": [], "trajectories": [], "experiences": [],
                                   "tools": [], "projects": []})

    transcript.add(SESSION, "user", "no author fields", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete,
                                                user_id="profile-user",
                                                enqueue_dialectic=False)
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
        "the latest-chunk projection must be recomputed, not fed back"


def test_dialectic_layer_publishes_advices_without_base_cards(plugin_package, store, transcript):
    def complete(task, system, prompt, **kwargs):
        return _dialectic_json()

    transcript.add(SESSION, "user", "review this", created_at=10.0)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete,
                                                agent_identity="Masía")
    assert instance.run_once() == 1
    state = store.layer_state(DIALECTIC, "agent:Masía")
    assert state["version"] == 1
    assert state["content"].startswith("## Adviser Review")
    assert "run a smoke test" in state["content"]
    assert "## User Peer Card" not in state["content"]
    assert "## Constraints" not in state["content"]
    pending = store.pending_advisories(SESSION)
    assert [row["severity"] for row in pending] == ["concern"]


def test_an_unusable_artifact_fails_the_job_and_keeps_the_previous_version(plugin_package, store,
                                                                           transcript):
    good_reply = _base_json(summary=["keeps this"], user=["[fact] this"], peer=["terse"],
                            ai_self=["direct"], identity=["careful"])
    seen: list[str] = []

    def complete(task, system, prompt, **kwargs):
        seen.append(prompt)
        return "no headings at all" if "second" in prompt else good_reply

    instance = plugin_package.worker.LayerWorker(
        store, transcript, complete=complete, enqueue_dialectic=False)
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
        return "" if "unusable" in prompt else _base_json(
            summary=["ok"], user=["[fact] ok"], peer=["ok"], ai_self=["ok"], identity=["ok"],
            markers={"entities": [], "trajectories": [], "experiences": [],
                     "tools": [], "projects": []})

    instance = plugin_package.worker.LayerWorker(
        store, transcript, complete=complete, enqueue_dialectic=False)
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
            "## AI Identity Card\n- a\n\n## Value Markers\n- [entity] a")
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

def test_dialectic_depth_adapts_and_runs_three_passes_for_long_windows(
        plugin_package, store, transcript):
    calls: list[str] = []

    def complete(task, system, prompt, **kwargs):
        calls.append(system)
        return _dialectic_json()

    store.put_layer_state("dialectic", "agent:minir", "## Adviser Review\n- prior")
    for index in range(4):
        transcript.add(SESSION, "user", f"long evidence {index} " + "z" * 3500,
                       created_at=10.0 + index)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, depth=3, complete=complete)
    assert instance.run_once() == 1
    assert len(calls) == 3, "a long window earns d1 synthesize, d2 self-audit, d3 reconcile"
    assert "auditing" in calls[1].lower() and "reconciling" in calls[2].lower()
    assert store.layer_state(DIALECTIC, "agent:minir") is not None


def test_cold_start_uses_identity_files_without_messages(plugin_package, store, transcript):
    seen: list[str] = []

    def complete(task, system, prompt, **kwargs):
        seen.append(system)
        return _dialectic_json(advices=[])

    instance = plugin_package.worker.LayerWorker(
        store, transcript, identity="I am Masía.", preference="Be terse.",
        depth=1, complete=complete)
    store.record_job(DIALECTIC, SESSION, 0.0, 20.0, turns=0)
    assert instance.run_once() == 1
    assert "no previous dialectic" in seen[0].lower()
    assert store.layer_state(DIALECTIC, "agent:minir") is not None


def test_a_dialectic_audit_failure_keeps_the_initial_draft(plugin_package, store, transcript):
    replies = [_dialectic_json(), "not a valid artifact"]
    seen: list[int] = []

    def complete(task, system, prompt, **kwargs):
        index = min(len(seen), len(replies) - 1)
        seen.append(index)
        return replies[index]

    instance = plugin_package.worker.LayerWorker(store, transcript, depth=2, complete=complete)
    transcript.add(SESSION, "user", "review this " + "z" * 5000, created_at=10.0)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    assert instance.run_once() == 1
    assert "run a smoke test" in store.layer_state(DIALECTIC, "agent:minir")["content"]


# -- canonical chunk chain ----------------------------------------------------

def test_base_refresh_appends_a_chunk_and_projects_only_the_newest(store, transcript, worker):
    transcript.add(SESSION, "user", "first window", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0, turns=1)
    assert worker.run_once() == 1

    chunks = store.chunk_summaries(SESSION)
    assert len(chunks) == 1
    assert "Ships a Hermes memory plugin" in chunks[0]["content"]
    assert store.newest_conclusion("base.session_summary") is None, \
        "the projection must not be a second independent conclusion"

    artifact = store.layer_state(BASE, "user:default")["content"]
    assert "## Session Summary" in artifact
    assert "Ships a Hermes memory plugin" in artifact
    assert "Local chunk" in artifact, "the base section projects the newest chunk"

    transcript.add(SESSION, "user", "second window", created_at=30.0)
    _dispatch(store, transcript, SESSION, BASE, 20.0, 40.0, turns=2)
    assert worker.run_once() == 1
    assert len(store.chunk_summaries(SESSION)) == 2
    projection = store.latest_chunk_summary(SESSION)
    assert projection.count("Local chunk") == 1, "only the newest chunk is projected"
    artifact = store.layer_state(BASE, "user:default")["content"]
    assert artifact.count("Local chunk") == 1


def test_each_chunk_records_the_turn_range_it_actually_covers(store, transcript, worker):
    # Three host turns were counted, but this window only stored the last one's message.
    transcript.add(SESSION, "user", "first window", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0, turns=3)
    assert worker.run_once() == 1
    chunk = store.chunk_summaries(SESSION)[0]
    assert (chunk["from_turn"], chunk["to_turn"]) == (3, 3)

    transcript.add(SESSION, "user", "second window", created_at=30.0)
    transcript.add(SESSION, "user", "third window", created_at=31.0)
    _dispatch(store, transcript, SESSION, BASE, 20.0, 40.0, turns=5)
    assert worker.run_once() == 1
    chunk = store.chunk_summaries(SESSION)[-1]
    assert (chunk["from_turn"], chunk["to_turn"]) == (4, 5)
    assert "turns 4-5" in store.latest_chunk_summary(SESSION)


def test_final_summary_supersedes_the_chunk_chain(store, transcript, worker):
    transcript.add(SESSION, "user", "window", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    worker.run_once()
    assert len(store.chunk_summaries(SESSION)) == 1

    store.put_conclusion("session_summary", "summary:" + SESSION, "## Goal\n- done",
                         session_id=SESSION, subject="session", status="active")
    assert store.supersede_chunk_summaries(SESSION) == 1
    assert store.chunk_summaries(SESSION) == []
    assert store.latest_chunk_summary(SESSION) == "", \
        "a superseded chunk is history, not the live window record"
    hits = store.search("Hermes memory", session_id=SESSION)
    assert any(row["kind"] == "session_chunk_summary" for row in hits), \
        "historical chunks stay searchable inside their session"
    assert not any(row["kind"] == "session_chunk_summary"
                   for row in store.search("Hermes memory", session_id="other-session"))


def test_dialectic_prompt_consumes_the_chunk_chain(store, transcript, worker):
    store.add_chunk_summary(SESSION, 1.0, 2.0, "chunk one: tests pass")
    store.add_chunk_summary(SESSION, 2.0, 3.0, "chunk two: tests fail")
    transcript.add(SESSION, "user", "reconcile this", created_at=10.0)
    _dispatch(store, transcript, SESSION, DIALECTIC, 0.0, 20.0)
    assert worker.run_once() == 1
    prompt = worker.calls[0]["prompt"]
    assert "LOCAL CHUNK SUMMARIES" in prompt
    assert "chunk one: tests pass" in prompt and "chunk two: tests fail" in prompt


def test_base_prompt_does_not_feed_the_previous_projection_back(store, transcript, worker):
    store.put_layer_state(
        "base", "user:default",
        "## Session Summary\n- OLD PROJECTION\n\n## User Representation\n- [goal] old\n\n"
        "## User Peer Card\n- durable peer\n\n## AI Self Representation\n- self\n\n"
        "## AI Identity Card\n- identity\n\n## Value Markers\n- [tool] old")
    transcript.add(SESSION, "user", "new delta", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert worker.run_once() == 1
    prompt = worker.calls[0]["prompt"]
    assert "OLD PROJECTION" not in prompt, "the latest-chunk projection must not be fed back"
    assert "durable peer" in prompt, "the other five sections still carry forward"


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


# -- the structured base refresh record ---------------------------------------

def test_base_refresh_persists_the_six_fields_and_window_metadata(store, transcript, worker):
    transcript.add(SESSION, "user", "I need a plugin", created_at=10.0)
    transcript.add(SESSION, "assistant", "starting", created_at=11.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0, turns=2)
    assert worker.run_once() == 1

    row = store.latest_base_refresh(SESSION, "user:default")
    assert row is not None and row["is_latest"] == 1
    assert json.loads(row["user_representation"]) == ["[goal] ship the plugin"]
    assert json.loads(row["user_peer_card"]) == ["Wants terse answers."]
    assert json.loads(row["ai_self_representation"]) == ["direct"]
    assert json.loads(row["ai_identity_card"]) == ["careful with user files"]
    assert json.loads(row["session_chunk_summary"]) == [
        "Ships a Hermes memory plugin from Windows."]
    assert json.loads(row["value_markers"]) == {
        "entities": ["Hermes memory plugin"],
        "trajectories": [],
        "experiences": [],
        "tools": ["pytest"],
        "projects": ["minir-memory"],
    }
    hits = store.search_base_refreshes("pytest")
    assert hits and json.loads(hits[0]["value_markers"])["tools"] == ["pytest"]
    assert row["window_from_ts"] == 0.0
    assert row["window_to_ts"] == store.last_processed_ts(BASE, SESSION)
    assert row["window_to_ts"] >= 20.0, "the newest job drains to the newest message"
    assert (row["window_from_turn"], row["window_to_turn"]) == (2, 2)

    transcript.add(SESSION, "user", "next window", created_at=30.0)
    _dispatch(store, transcript, SESSION, BASE, 20.0, 40.0, turns=4)
    assert worker.run_once() == 1
    history = store.base_refreshes(SESSION, "user:default")
    assert len(history) == 2 and [item["is_latest"] for item in history] == [1, 0]
    assert history[0]["window_from_ts"] == history[1]["window_to_ts"], \
        "windows are edge-to-edge in the stored history"
    assert history[0]["window_from_ts"] >= 20.0
    assert history[0]["window_to_ts"] == store.last_processed_ts(BASE, SESSION)
    assert (history[1]["window_from_turn"], history[1]["window_to_turn"]) == (2, 2)
    assert (history[0]["window_from_turn"], history[0]["window_to_turn"]) == (4, 4)


def test_first_refresh_bootstraps_and_later_refreshes_merge(plugin_package, store, transcript,
                                                            monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(plugin_package.session_search, "query", lambda args, **kwargs: {
        "messages": [{"content": "anchor: build the memory plugin"}]})

    def complete(task, system, prompt, **kwargs):
        calls.append(prompt)
        return _base_json(summary=["[goal] build"], user=["[goal] build"], peer=["terse"],
                          ai_self=["direct"], identity=["careful"])

    instance = plugin_package.worker.LayerWorker(
        store, transcript, complete=complete, state_db="test-state.db",
        enqueue_dialectic=False)
    transcript.add(SESSION, "user", "first turn", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert instance.run_once() == 1
    assert "first base refresh (bootstrap)" in calls[0]
    assert "Previous user cards: none" in calls[0]
    assert "Session: session-1" in calls[0]
    assert "Window: (0.0, " in calls[0]
    assert "anchor: build the memory plugin" in calls[0]
    assert "first turn" in calls[0]

    transcript.add(SESSION, "user", "second turn", created_at=30.0)
    _dispatch(store, transcript, SESSION, BASE, 20.0, 40.0)
    assert instance.run_once() == 1
    second = calls[3]
    assert "subsequent base refresh (merge previous cards)" in second
    assert "Previous user cards:" in second and "terse" in second
    assert "first turn" not in second, "the previous window is not re-fed as evidence"
    assert "second turn" in second
    assert "Session: session-1" in second
    assert "anchor: build the memory plugin" not in second, \
        "the first-message anchor belongs to the first refresh only"
    assert "Previous value markers:" in calls[5]
    assert "- [entity] Hermes memory plugin" in calls[5], \
        "value markers merge the previous window's inventory"


def test_base_refresh_runs_on_the_adviser_route_with_the_adviser_toolset(
        plugin_package, store, transcript, monkeypatch):
    reply = _base_json(summary=["[goal] build"], user=["[goal] build"], peer=["terse"],
                       ai_self=["direct"], identity=["careful"])
    requests: list[dict] = []

    monkeypatch.setattr(plugin_package.adviser, "load_config",
                        lambda: {"provider": "deepseek", "model": "deepseek-v4-flash",
                                 "toolsets": ["file"]})

    def run(message, **kwargs):
        requests.append({"message": message, **kwargs})
        return type("Run", (), {"answer": reply})()

    monkeypatch.setattr(plugin_package.agent_loop, "run", run)
    instance = plugin_package.worker.LayerWorker(
        store, transcript, enqueue_dialectic=False)  # no injection -> adviser route
    transcript.add(SESSION, "user", "grounded by session search", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert instance.run_once() == 1
    assert len(requests) == 3, "user cards, AI cards, then the window summary"
    for request in requests:
        assert request["provider"] == "deepseek"
        assert request["model"] == "deepseek-v4-flash"
        assert "file" in request["toolsets"] and "adviser" in request["toolsets"]
    assert "You maintain the user model" in requests[0]["system_message"]
    assert "You maintain the AI half" in requests[1]["system_message"]
    assert "local compression record" in requests[2]["system_message"]
    for request in requests:
        assert "adviser_session_search" in request["system_message"]
        assert "Session: session-1" in request["message"]
        assert "JSON object" in request["system_message"]
    assert '"user_representation"' in requests[0]["system_message"]
    assert '"value_markers"' in requests[2]["system_message"]
    assert store.latest_base_refresh(SESSION, "user:default") is not None


def test_one_shot_fallback_prompt_does_not_mention_unavailable_tools(store, transcript, worker):
    transcript.add(SESSION, "user", "fallback path", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    assert worker.run_once() == 1
    assert worker.calls
    for call in worker.calls:
        assert "adviser_session_search" not in call["system"], \
            "a one-shot fallback has no adviser tool and must not be told to call it"
        assert "JSON object" in call["system"]


def test_retry_reuses_the_chunk_turn_range(store, transcript, worker):
    transcript.add(SESSION, "user", "retry this window", created_at=10.0)
    store.add_chunk_summary(SESSION, 0.0, 20.0, "- previous attempt", from_turn=1, to_turn=5)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0, turns=5)
    assert worker.run_once() == 1
    row = store.latest_base_refresh(SESSION, "user:default")
    assert (row["window_from_turn"], row["window_to_turn"]) == (1, 5), \
        "a retry must reuse the chunk range, not treat its own chunk as the previous window"


def test_json_items_are_bounded(plugin_package):
    items = plugin_package.worker._json_items(
        ["x" * 2_000, 5, "", "durable preference"], max_items=2, max_chars=10)
    assert items == ["x" * 10, "durable pr"], \
        "non-strings/empties are dropped; each item and the count are bounded"


# -- dependent dialectic + base revision --------------------------------------

def test_a_base_job_queues_the_dependent_dialectic_window(plugin_package, store, transcript):
    calls: list[str] = []

    def complete(task, system, prompt, **kwargs):
        calls.append(task)
        return (_dialectic_json() if task == plugin_package.worker.DIALECTIC_LAYER_TASK
                else _base_json())

    transcript.add(SESSION, "user", "ship it", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete)

    assert instance.run_once() == 2, "the base job and the dialectic job it queues"
    assert calls == [plugin_package.worker.BASE_LAYER_TASK] * 3 + [
        plugin_package.worker.DIALECTIC_LAYER_TASK]
    assert store.pending_jobs() == []
    assert store.layer_state(BASE, "user:default") is not None
    assert store.layer_state(DIALECTIC, "agent:minir") is not None
    assert store.pending_advisories(SESSION)


def test_dialectic_revision_snapshots_the_old_base_and_applies_the_change(
        plugin_package, store, transcript):
    def complete(task, system, prompt, **kwargs):
        if task == plugin_package.worker.DIALECTIC_LAYER_TASK:
            return _dialectic_json(peer=["Wants terse answers.", "No emoji."],
                                   changed=True, fields=["user_peer_card"])
        return _base_json()

    transcript.add(SESSION, "user", "ship it", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete)
    assert instance.run_once() == 2

    latest = store.latest_base_refresh(SESSION, "user:default")
    assert json.loads(latest["user_peer_card"]) == ["Wants terse answers.", "No emoji."]
    revisions = store.base_revisions(SESSION, "user:default")
    assert len(revisions) == 1
    revision = revisions[0]
    assert revision["base_refresh_id"] == latest["id"]
    assert json.loads(revision["user_peer_card"]) == ["Wants terse answers."]
    assert json.loads(revision["changed_fields"]) == ["user_peer_card"]
    assert "smoke test" in revision["advices"]
    assert "No emoji." in store.layer_state(BASE, "user:default")["content"]
    assert store.search_base_revisions("smoke test", session_id=SESSION)
    assert store.stats()["base_revisions"] == 1


def test_a_dialectic_job_without_its_base_window_does_not_correct_another_window(
        plugin_package, store, transcript):
    def complete(task, system, prompt, **kwargs):
        if task == plugin_package.worker.DIALECTIC_LAYER_TASK:
            return _dialectic_json(peer=["mutated"], changed=True, fields=["user_peer_card"])
        return _base_json()

    transcript.add(SESSION, "user", "ship it", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    base_worker = plugin_package.worker.LayerWorker(
        store, transcript, complete=complete, enqueue_dialectic=False)
    base_worker.run_once()
    before = store.latest_base_refresh(SESSION, "user:default")

    # A job that did not follow this base window (the context engine can queue one) may still
    # emit advice, but must not overwrite an unrelated base row.
    store.record_job(DIALECTIC, SESSION, -1.0, 100.0)
    dialectic_worker = plugin_package.worker.LayerWorker(store, transcript, complete=complete)
    assert dialectic_worker.run_once() == 1

    after = store.latest_base_refresh(SESSION, "user:default")
    assert after["user_peer_card"] == before["user_peer_card"]
    assert store.base_revisions(SESSION) == []
    assert store.pending_advisories(SESSION)


def test_dialectic_reviews_the_base_row_for_its_own_window(
        plugin_package, store, transcript):
    common = dict(
        window_from_turn=None, window_to_turn=None,
        user_representation=json.dumps(["[goal] window"]),
        user_peer_card=json.dumps(["window-1 peer"]),
        ai_self_representation=json.dumps(["[done] window"]),
        ai_identity_card=json.dumps(["careful"]),
        session_chunk_summary=json.dumps(["[done] window"]),
        value_markers=json.dumps({"entities": [], "trajectories": [], "experiences": [],
                                  "tools": [], "projects": []}))
    store.add_base_refresh(SESSION, "user:default", window_from_ts=0.0, window_to_ts=10.0,
                           **common)
    store.add_base_refresh(SESSION, "user:default", window_from_ts=10.0, window_to_ts=20.0,
                           **{**common, "user_peer_card": json.dumps(["window-2 peer"])})
    prompts: list[str] = []

    def complete(task, system, prompt, **kwargs):
        prompts.append(prompt)
        return _dialectic_json(changed=False)

    instance = plugin_package.worker.LayerWorker(store, transcript, complete=complete)
    rows = [{"role": "user", "content": "window one", "created_at": 5.0,
             "message_uid": "m1"}]
    instance._merge_dialectic(rows, session=SESSION, from_ts=0.0, to_ts=10.0)

    assert "window-1 peer" in prompts[0]
    assert "window-2 peer" not in prompts[0], \
        "a catch-up review must read its own base window, not the live one"


def test_dialectic_route_appends_the_adviser_toolset_explicitly(
        plugin_package, store, transcript, monkeypatch):
    requests: list[dict] = []

    def run(message, **kwargs):
        requests.append({"message": message, **kwargs})
        reply = (_dialectic_json() if "dialectic reviewer" in kwargs.get("system_message", "")
                 else _base_json())
        return type("Run", (), {"answer": reply})()

    monkeypatch.setattr(plugin_package.adviser, "load_config",
                        lambda: {"provider": "deepseek", "model": "m", "toolsets": []})
    monkeypatch.setattr(plugin_package.agent_loop, "run", run)
    transcript.add(SESSION, "user", "grounded by history", created_at=10.0)
    _dispatch(store, transcript, SESSION, BASE, 0.0, 20.0)
    instance = plugin_package.worker.LayerWorker(store, transcript)
    assert instance.run_once() == 2

    assert len(requests) == 4, "three base passes then one dialectic pass"
    dialectic = requests[-1]
    assert "adviser" in dialectic["toolsets"]
    assert "dialectic reviewer" in dialectic["system_message"]
    assert "Base-layer artifact for this window" in dialectic["message"]
