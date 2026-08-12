"""HarnessWorker splits jobs into a fast infrastructure lane and a slow
agent lane.

Regression coverage for a real, measured production problem: drain_once()
waits for its entire leased batch to finish, so a single long agent run
(session.dispatch driving a full reasoning loop) blocked every fast job
leased alongside it. Inbound Slack messages sat unrouted for up to 66
seconds behind one 57-second agent run -- which in turn widened the window
in which a Task could observe, and act on, a message that had not yet been
routed to it.
"""

from __future__ import annotations

import threading
import time


def test_every_job_kind_lands_in_exactly_one_lane(brain):
    """A kind in no lane would never be processed by the gateway at all
    (main.py drives exactly these three lanes and nothing else); a kind in
    more than one would be leased by two loops at once."""
    worker = brain.harness_worker
    agent = set(worker.agent_job_kinds())
    infra = set(worker.infrastructure_job_kinds())
    knowledge = set(worker.knowledge_job_kinds())
    every_kind = set(worker.handlers)

    assert agent & infra == set()
    assert agent & knowledge == set()
    assert infra & knowledge == set()
    assert agent | infra | knowledge == every_kind


def test_the_agent_lane_holds_exactly_the_full_reasoning_loop_kinds(brain):
    """These four are the only kinds that drive a whole agent loop and can
    legitimately run for minutes. Everything else finishes in well under a
    second and must not be stuck behind them."""
    worker = brain.harness_worker

    assert set(worker.agent_job_kinds()) == {
        "session.dispatch",
        "task.run_child",
        "task.pi_poll",
        "approval.resolve",
    }
    # The latency-sensitive path that actually regressed in production.
    assert "event.route" in worker.infrastructure_job_kinds()
    assert "delivery.send" in worker.infrastructure_job_kinds()


def test_the_knowledge_lane_holds_exactly_chunking_and_episode_work(brain):
    """Slow/remote like the agent lane, but not a conversational turn --
    isolated in its own pool so ingest volume can't starve control jobs
    (memory ingestion/retrieval plan §9)."""
    worker = brain.harness_worker

    assert set(worker.knowledge_job_kinds()) == {
        "knowledge.index_evidence",
        "knowledge.consolidate_episode",
        "memory.capture_task",
        "skill.curate_task",
    }


def test_knowledge_jobs_run_on_their_own_pool_not_the_main_one(brain):
    """A saturated main pool must not block a knowledge job, and vice
    versa -- that's the entire point of a third pool."""
    worker = brain.harness_worker
    worker.concurrency = 1
    release = threading.Event()
    worker.register("test.block_main", lambda _payload: release.wait(timeout=10) or {})
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="test.block_main",
        payload={},
        idempotency_key="lane-test:main-block",
    )
    assert worker.pump(kinds=["test.block_main"])["leased"] == 1
    # Main pool is now fully saturated (concurrency=1) -- a knowledge job
    # must still be able to start on its own pool.
    finished: list[str] = []
    worker.register("knowledge.index_evidence", lambda _payload: finished.append("k") or {})
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="knowledge.index_evidence",
        payload={"source_id": "does_not_matter"},
        idempotency_key="lane-test:knowledge",
    )
    assert worker.pump(kinds=worker.knowledge_job_kinds())["leased"] == 1
    for _ in range(100):
        if "k" in finished:
            break
        time.sleep(0.05)
    assert "k" in finished, "knowledge job was blocked behind a saturated main pool"
    release.set()


def test_pump_starts_new_work_while_a_slow_job_is_still_running(brain):
    """The point of pump(): a lane must keep leasing and starting jobs
    while an earlier slow job is still in flight. drain_once() cannot do
    this -- it waits for its whole batch, which is precisely how one
    57-second agent run left inbound Slack messages unrouted for over a
    minute."""
    import threading

    release = threading.Event()
    started = threading.Event()
    finished: list[str] = []

    def slow(_payload):
        started.set()
        release.wait(timeout=10)
        finished.append("slow")
        return {}

    brain.harness_worker.register("test.slow", slow)
    brain.harness_worker.register(
        "test.fast", lambda _payload: finished.append("fast") or {}
    )
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="test.slow",
        payload={},
        idempotency_key="pump:slow",
    )

    assert brain.harness_worker.pump(kinds=["test.slow"])["leased"] == 1
    assert started.wait(timeout=5)  # running, and pump already returned

    # A job enqueued while the slow one is still blocked must still start.
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="test.fast",
        payload={},
        idempotency_key="pump:fast",
    )
    assert brain.harness_worker.pump(kinds=["test.fast"])["leased"] == 1

    for _ in range(100):
        if "fast" in finished:
            break
        time.sleep(0.05)
    assert "fast" in finished, "fast job was blocked behind the slow one"
    assert "slow" not in finished, "slow job should still be in flight"

    release.set()


def test_pump_never_leases_more_than_it_can_start(brain):
    """Leasing is capped at free capacity so a job's lease clock never
    starts while it waits for a thread -- otherwise a lease could expire
    before the job ever ran and a second worker would double-execute it."""
    worker = brain.harness_worker
    worker.concurrency = 2
    release = threading.Event()

    worker.register("test.block", lambda _payload: release.wait(timeout=10) or {})
    for index in range(5):
        brain.harness_jobs.enqueue(
            organization_id="org_acme",
            kind="test.block",
            payload={},
            idempotency_key=f"pump:capacity:{index}",
        )

    assert worker.pump(kinds=["test.block"])["leased"] == 2
    assert worker.pump(kinds=["test.block"]) == {"leased": 0, "saturated": True}

    release.set()


def test_draining_one_lane_never_leases_the_other_lanes_jobs(brain):
    """The lanes must genuinely partition the queue at lease time -- if the
    infra drain could pick up a session.dispatch job, the head-of-line
    blocking this split exists to prevent would come straight back."""
    worker = brain.harness_worker
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="session.dispatch",
        payload={"session_id": "session_does_not_exist"},
        idempotency_key="lane-test:agent",
    )

    result = worker.drain_once(kinds=worker.infrastructure_job_kinds())

    assert result["leased"] == 0
