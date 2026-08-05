"""Storage-layer concurrency tests for the Session/Task architecture:
atomic Session Manager claims and atomic Task claims must behave as a real
compare-and-swap under genuine concurrent access -- two executors racing for
the same lock must never both win."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from docket.harness.task_run_store import TaskRunStore
from docket.storage.db import BrainStore


def _make_store() -> BrainStore:
    return BrainStore(Path(tempfile.mktemp(suffix=".db")))


def _now() -> str:
    from docket.domain.models import utcnow

    return utcnow().isoformat()


def test_two_executors_cannot_claim_the_same_session_manager():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_race",
            "organization_id": "org_acme",
            "conversation_key": "slack:race:1",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    store.bump_session_revision("sess_race")

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt(owner: str) -> None:
        barrier.wait()
        results.append(
            store.claim_session_manager("sess_race", owner=owner, lease_seconds=60)
        )

    threads = [
        threading.Thread(target=attempt, args=(f"executor-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]
    session = store.get_session("sess_race")
    assert session["manager_state"] == "running"
    assert session["manager_owner"] in {"executor-0", "executor-1"}


def test_two_executors_cannot_claim_the_same_task():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_task_race",
            "organization_id": "org_acme",
            "conversation_key": "slack:race:2",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    store.create_task(
        {
            "id": "task_race",
            "organization_id": "org_acme",
            "session_id": "sess_task_race",
            "root_task_id": "task_race",
            "state": "queued",
            "worker_type": "general",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt(owner: str) -> None:
        barrier.wait()
        results.append(store.claim_task("task_race", owner=owner, lease_seconds=60))

    threads = [
        threading.Thread(target=attempt, args=(f"executor-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]
    task = store.get_task("task_race")
    assert task["state"] == "running"


def test_different_sessions_claim_concurrently_without_blocking_each_other():
    store = _make_store()
    for i in range(2):
        store.create_session(
            {
                "id": f"sess_{i}",
                "organization_id": "org_acme",
                "conversation_key": f"slack:distinct:{i}",
                "created_at": _now(),
                "updated_at": _now(),
            }
        )
        store.bump_session_revision(f"sess_{i}")

    results: dict[str, bool] = {}

    def attempt(session_id: str) -> None:
        results[session_id] = store.claim_session_manager(
            session_id, owner="executor-shared", lease_seconds=60
        )

    threads = [
        threading.Thread(target=attempt, args=(f"sess_{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {"sess_0": True, "sess_1": True}


def test_sibling_tasks_claim_concurrently():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_siblings",
            "organization_id": "org_acme",
            "conversation_key": "slack:siblings:1",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    for i in range(3):
        store.create_task(
            {
                "id": f"child_{i}",
                "organization_id": "org_acme",
                "session_id": "sess_siblings",
                "parent_task_id": "root_task",
                "root_task_id": "root_task",
                "state": "queued",
                "worker_type": "general",
                "created_at": _now(),
                "updated_at": _now(),
            }
        )

    results: dict[str, bool] = {}

    def attempt(task_id: str) -> None:
        results[task_id] = store.claim_task(
            task_id, owner="executor-shared", lease_seconds=60
        )

    threads = [
        threading.Thread(target=attempt, args=(f"child_{i}",)) for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results.values())


def test_expired_session_manager_lease_can_be_reclaimed():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_stale",
            "organization_id": "org_acme",
            "conversation_key": "slack:stale:1",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    store.bump_session_revision("sess_stale")
    assert store.claim_session_manager("sess_stale", owner="crashed-executor", lease_seconds=-1)
    # Lease is already expired (negative TTL) -- a fresh executor can reclaim
    # a `running` session without anyone explicitly releasing it first.
    assert store.claim_session_manager("sess_stale", owner="recovering-executor", lease_seconds=60)
    session = store.get_session("sess_stale")
    assert session["manager_owner"] == "recovering-executor"


def test_expired_task_lease_can_be_reclaimed():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_stale_task",
            "organization_id": "org_acme",
            "conversation_key": "slack:stale:2",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    store.create_task(
        {
            "id": "task_stale",
            "organization_id": "org_acme",
            "session_id": "sess_stale_task",
            "root_task_id": "task_stale",
            "state": "queued",
            "worker_type": "general",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    assert store.claim_task("task_stale", owner="crashed-executor", lease_seconds=-1)
    assert store.claim_task("task_stale", owner="recovering-executor", lease_seconds=60)
    assert store.get_task("task_stale")["execution_owner"] == "recovering-executor"


def test_old_task_owner_cannot_update_or_complete_after_reclaim():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_task_lost",
            "organization_id": "org_acme",
            "conversation_key": "slack:lost:task",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    store.create_task(
        {
            "id": "task_lost",
            "organization_id": "org_acme",
            "session_id": "sess_task_lost",
            "root_task_id": "task_lost",
            "state": "queued",
            "worker_type": "general",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    assert store.claim_task("task_lost", owner="old-executor", lease_seconds=-1)
    assert store.claim_task("task_lost", owner="new-executor", lease_seconds=60)

    stale = dict(store.get_task("task_lost"))
    stale["result"] = "stale result"
    stale["updated_at"] = _now()
    assert not store.update_task_owned(stale, owner="old-executor")
    assert not store.release_task(
        "task_lost", owner="old-executor", next_state="completed", task=stale
    )

    old_runs = TaskRunStore(store, owner="old-executor", lease_seconds=60)
    with pytest.raises(RuntimeError, match="lease lost"):
        old_runs.complete("task_lost", "stale final answer")

    current = store.get_task("task_lost")
    assert current["state"] == "running"
    assert current["execution_owner"] == "new-executor"
    assert current.get("final_answer") is None


def test_old_owner_cannot_release_after_losing_the_lease():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_lost",
            "organization_id": "org_acme",
            "conversation_key": "slack:lost:1",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    store.bump_session_revision("sess_lost")
    store.claim_session_manager("sess_lost", owner="crashed-executor", lease_seconds=-1)
    store.claim_session_manager("sess_lost", owner="new-executor", lease_seconds=60)
    # The old (crashed) owner must not be able to release a session it no
    # longer owns -- that would corrupt the new owner's active claim.
    assert not store.release_session_manager("sess_lost", owner="crashed-executor", idle=True)
    assert store.get_session("sess_lost")["manager_owner"] == "new-executor"


def test_revision_increments_are_never_lost_under_concurrent_bumps():
    store = _make_store()
    store.create_session(
        {
            "id": "sess_revisions",
            "organization_id": "org_acme",
            "conversation_key": "slack:revisions:1",
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    barrier = threading.Barrier(10)

    def bump() -> None:
        barrier.wait()
        store.bump_session_revision("sess_revisions")

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert store.get_session("sess_revisions")["manager_revision"] == 10
