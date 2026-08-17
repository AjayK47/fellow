"""SQLite remains local, but readers and durable job claims can be concurrent."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from toda.harness.jobs import JobQueue
from toda.storage.db import BrainStore


def test_one_store_does_not_lock_all_operations_behind_an_active_reader(tmp_path):
    store = BrainStore(tmp_path / "concurrent.db")
    try:
        store.put_organization({"id": "org_one", "name": "One"})
        with store.cursor() as reader:
            reader.execute("SELECT payload FROM organizations")
            assert reader.fetchone() is not None
            with ThreadPoolExecutor(max_workers=1) as pool:
                write = pool.submit(
                    store.put_organization,
                    {"id": "org_two", "name": "Two"},
                )
                assert write.result(timeout=2) is None
                assert store.get_organization("org_two")["id"] == "org_two"
    finally:
        store.close()


def test_two_process_style_workers_cannot_lease_the_same_job(tmp_path):
    path = tmp_path / "leases.db"
    first_store = BrainStore(path)
    second_store = BrainStore(path)
    first = JobQueue(first_store)
    second = JobQueue(second_store)
    try:
        job = first.enqueue(
            organization_id="org_acme",
            kind="test.concurrent",
            payload={"value": 1},
            idempotency_key="only-once",
        )
        barrier = Barrier(2)

        def claim(queue: JobQueue, worker_id: str):
            barrier.wait()
            return queue.lease(worker_id=worker_id, limit=1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [
                pool.submit(claim, first, "worker_one"),
                pool.submit(claim, second, "worker_two"),
            ]
            batches = [future.result(timeout=5) for future in claims]

        leased = [item for batch in batches for item in batch]
        assert [item["id"] for item in leased] == [job["id"]]
        owner = leased[0]["lease_owner"]
        wrong_owner = "worker_two" if owner == "worker_one" else "worker_one"
        assert first.complete(job["id"], worker_id=wrong_owner) is None
        assert first_store.get_harness_job(job["id"])["status"] == "leased"
        assert first.complete(job["id"], worker_id=owner)["status"] == "completed"
    finally:
        first_store.close()
        second_store.close()
