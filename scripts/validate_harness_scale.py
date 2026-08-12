"""Repeatable local smoke benchmark for the 1,000-person harness target."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from toda.domain.models import utcnow
from toda.services.container import TodaApp


def _milliseconds(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def run(*, people: int = 1_000, tasks: int = 1_000) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="toda-scale-") as directory:
        app = TodaApp(db_path=Path(directory) / "scale.db")
        app.store.put_organization({"id": "org_scale", "name": "ScaleCo"})

        started = time.perf_counter()
        for index in range(people):
            app.graph.upsert_entity(
                {
                    "id": f"person_{index}",
                    "organization_id": "org_scale",
                    "entity_type": "person",
                    "name": f"Person {index}",
                    "aliases": [f"employee-{index}"],
                    "attributes": {"team": f"team-{index % 50}"},
                }
            )
        people_ms = _milliseconds(started)

        started = time.perf_counter()
        now = utcnow().isoformat()
        for index in range(tasks):
            task_id = f"task_scale_{index}"
            task = {
                "id": task_id,
                "organization_id": "org_scale",
                "session_id": f"session_{index % 100}",
                "parent_task_id": None,
                "root_task_id": task_id,
                "state": "completed",
                "worker_type": "session_manager",
                "goal": f"Migrate customer batch {index} export data",
                "completion_summary": f"Customer batch {index} migrated successfully",
                "owner_id": f"person_{index % people}",
                "origin_scope_id": "scope_org_public",
                "created_at": now,
                "updated_at": now,
                "completed_at": now,
            }
            app.store.create_task(task)
            app.store.index_root_task(task)
        tasks_ms = _milliseconds(started)

        started = time.perf_counter()
        found = app.store.search_root_tasks(
            "org_scale", query="customer batch 777", limit=10
        )
        search_ms = _milliseconds(started)

        started = time.perf_counter()
        candidates = app.store.find_entity_candidates(
            "org_scale", "employee-777", limit=20
        )
        entity_lookup_ms = _milliseconds(started)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=16) as pool:
            rows = list(
                pool.map(
                    lambda index: app.store.search_root_tasks(
                        "org_scale", query=f"batch {index}", limit=3
                    ),
                    range(100),
                )
            )
        concurrent_reads_ms = _milliseconds(started)
        app.close()

    return {
        "people": people,
        "root_tasks": tasks,
        "people_index_ms": people_ms,
        "task_index_ms": tasks_ms,
        "task_search_ms": search_ms,
        "task_search_hits": len(found),
        "entity_lookup_ms": entity_lookup_ms,
        "entity_lookup_hits": len(candidates),
        "concurrent_100_searches_ms": concurrent_reads_ms,
        "concurrent_search_result_sets": len(rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--people", type=int, default=1_000)
    parser.add_argument("--tasks", type=int, default=1_000)
    arguments = parser.parse_args()
    print(json.dumps(run(people=arguments.people, tasks=arguments.tasks), indent=2))
