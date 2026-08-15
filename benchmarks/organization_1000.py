#!/usr/bin/env python3
"""Reproducible 1,000-member Toda harness benchmark.

This deliberately exercises the local storage/runtime boundary without model or
network calls. It reports timings, but fails only on correctness and isolation:
performance thresholds belong in a history of comparable CI runners, not in a
single invented number.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Providers are pinned by passing Settings into TodaApp below, not by the
# environment. These used to be TODA_MODEL_PROVIDER / TODA_EMBEDDING_PROVIDER
# environment variables, which nothing has read since config.json became the
# single source of configuration -- so this benchmark's "no model or network
# calls" promise was false, and a developer with a configured embedding model
# made 1,000 real billable calls by running it.

ORG = "org_benchmark"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def measure(operation: Callable[[], Any], repeats: int = 1) -> tuple[Any, dict[str, float]]:
    samples: list[float] = []
    result: Any = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = operation()
        samples.append((time.perf_counter() - started) * 1_000)
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return result, {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "samples": len(samples),
    }


def seed(app: Any, *, people: int, tasks: int) -> dict[str, float]:
    started = time.perf_counter()
    app.store.put_organization({"id": ORG, "name": "Benchmark Company"})
    app.store.upsert_scope(
        {"id": "scope_public", "organization_id": ORG, "kind": "organization_public", "is_public": True}
    )
    for team in range(100):
        app.store.upsert_scope(
            {"id": f"scope_team_{team}", "organization_id": ORG, "kind": "team", "is_public": False}
        )
        app.graph.upsert_entity(
            {"id": f"team_{team}", "organization_id": ORG, "entity_type": "team", "name": f"Team {team}"}
        )

    for index in range(people):
        person_id = f"person_{index}"
        team = index % 100
        app.graph.upsert_entity(
            {
                "id": person_id,
                "organization_id": ORG,
                "entity_type": "person",
                "name": f"Person {index}",
                "aliases": [f"employee-{index}", f"person{index}@example.test"],
            }
        )
        app.store.add_membership(ORG, person_id, "scope_public")
        app.store.add_membership(ORG, person_id, f"scope_team_{team}")
        app.graph.add_edge(ORG, "MEMBER_OF", person_id, f"team_{team}")
        memory_id = f"memory_{index}"
        app.store.upsert_memory(
            {
                "id": memory_id,
                "organization_id": ORG,
                "memory_type": "fact",
                "status": "active",
                "is_latest": True,
                "scope_id": f"scope_team_{team}",
                "is_private": True,
                "content": f"Person {index} owns benchmark account {index}.",
                "valid_from": now(),
            }
        )
        app.graph.add_edge(ORG, "ABOUT", memory_id, person_id)

    timestamp = now()
    for index in range(tasks):
        task_id = f"task_{index}"
        task = {
            "id": task_id,
            "organization_id": ORG,
            "session_id": f"session_{index % 250}",
            "parent_task_id": None,
            "root_task_id": task_id,
            "state": "completed",
            "worker_type": "session_manager",
            "goal": f"Prepare migration batch {index} for customer benchmark-{index}",
            "completion_summary": f"Migration batch {index} completed",
            "owner_id": f"person_{index % people}",
            "origin_scope_id": f"scope_team_{index % 100}",
            "created_at": timestamp,
            "updated_at": timestamp,
            "completed_at": timestamp,
        }
        app.store.create_task(task)
        app.store.index_root_task(task)

    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for index in range(250):
        app.store.enqueue_harness_job(
            {
                "id": f"job_{index}",
                "organization_id": ORG,
                "kind": "benchmark.noop",
                "status": "queued",
                "available_at": expired,
                "attempts": 0,
                "max_attempts": 3,
                "idempotency_key": f"benchmark:{index}",
                "payload": {"index": index},
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
    return {"seed_ms": round((time.perf_counter() - started) * 1_000, 3)}


def run(*, people: int = 1_000, tasks: int = 2_000) -> dict[str, Any]:
    from toda.services.container import TodaApp

    with tempfile.TemporaryDirectory(prefix="toda-benchmark-") as directory:
        from toda.config import ModelsConfig, settings as loaded

        app = TodaApp(
            db_path=Path(directory) / "organization.db",
            settings=loaded.model_copy(update={"models": ModelsConfig()}),
        )
        metrics: dict[str, Any] = seed(app, people=people, tasks=tasks)

        candidates, metrics["entity_alias_lookup"] = measure(
            lambda: app.store.find_entity_candidates(ORG, "employee-777", limit=10), 50
        )
        neighbors, metrics["graph_neighbor_lookup"] = measure(
            lambda: app.graph.neighbors(ORG, "person_777"), 50
        )
        memories, metrics["authorized_memory_lookup"] = measure(
            lambda: app.graph.current_memories(
                ORG, entity_ids=["person_777"], scope_ids=["scope_public", "scope_team_77"]
            ),
            50,
        )
        hidden, metrics["unauthorized_memory_lookup"] = measure(
            lambda: app.graph.current_memories(
                ORG, entity_ids=["person_777"], scope_ids=["scope_public"]
            ),
            50,
        )
        found, metrics["task_history_search"] = measure(
            lambda: app.store.search_root_tasks(ORG, query="migration batch 1777", limit=10), 50
        )

        queries = [f"migration batch {index}" for index in range(100, 300)]
        _, metrics["concurrent_200_task_searches"] = measure(
            lambda: list(
                ThreadPoolExecutor(max_workers=16).map(
                    lambda query: app.store.search_root_tasks(ORG, query=query, limit=3), queries
                )
            )
        )
        leased, metrics["lease_100_jobs"] = measure(
            lambda: app.store.lease_harness_jobs(
                worker_id="benchmark-worker", limit=100, lease_seconds=60, kinds=["benchmark.noop"]
            )
        )

        assertions = {
            "exact_alias_resolves": bool(candidates and candidates[0]["id"] == "person_777"),
            "relationship_resolves": bool(neighbors),
            "authorized_memory_visible": bool(memories and memories[0]["id"] == "memory_777"),
            "unauthorized_memory_hidden": hidden == [],
            "task_history_resolves": bool(found and found[0]["id"] == "task_1777"),
            "job_leases_are_unique": len(leased) == len({job["id"] for job in leased}) == 100,
        }
        app.close()

    result = {
        "schema": 1,
        "created_at": now(),
        "dataset": {
            "organizations": 1,
            "people": people,
            "teams": 100,
            "private_memories": people,
            "root_tasks": tasks,
            "queued_jobs": 250,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "sqlite": __import__("sqlite3").sqlite_version,
        },
        "metrics": metrics,
        "assertions": assertions,
        "passed": all(assertions.values()),
    }
    if not result["passed"]:
        raise AssertionError(json.dumps(assertions, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--people", type=int, default=1_000)
    parser.add_argument("--tasks", type=int, default=2_000)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run(people=arguments.people, tasks=arguments.tasks)
    encoded = json.dumps(report, indent=2) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
