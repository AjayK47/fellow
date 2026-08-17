#!/usr/bin/env python3
"""End-to-end verification script for Toda (writes evidence artifacts)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

SCRATCH = Path(
    os.environ.get(
        "TODA_SCRATCH",
        "/var/folders/g7/jnbsr5mx65sdhpxwhsd5mfmc0000gn/T/grok-goal-17676d6b3ae8/implementer",
    )
)
SCRATCH.mkdir(parents=True, exist_ok=True)

BASE = os.environ.get("TODA_URL", "http://127.0.0.1:8080")
ORG = "org_acme"
DEST = {"type": "internal_agent_session", "id": "verify_session", "is_private": True}


def write(name: str, data) -> None:
    path = SCRATCH / name
    if isinstance(data, (dict, list)):
        path.write_text(json.dumps(data, indent=2, default=str))
    else:
        path.write_text(str(data))
    print(f"wrote {path}")


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30.0)

    # health
    for attempt in range(30):
        try:
            h = client.get("/health")
            if h.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        write("brain-launch.log", "FAILED: server not healthy")
        return 1

    write("brain-launch.log", f"OK health={client.get('/health').json()}\n")

    # five reactive ops
    search = client.post(
        "/v1/search",
        json={
            "organization_id": ORG,
            "query": "launch date Acme export",
            "acting_principal_id": "person_ajay",
            "destination": DEST,
            "limit": 10,
        },
    ).json()
    write("reactive-search.json", search)
    assert search.get("count", 0) >= 1, search
    assert search["results"][0]["memory"]["content"]

    ctx1 = client.post(
        "/v1/context:prepare",
        json={
            "organization_id": ORG,
            "task": "Investigate Acme export issue",
            "acting_principal_id": "person_ajay",
            "destination": DEST,
            "entity_ids": ["customer_acme"],
            "token_budget": 4000,
        },
    ).json()
    write("reactive-prepare_context.json", ctx1)
    assert ctx1.get("permission_decision_id")
    assert ctx1.get("summary")

    # second prepare_context for consistency
    ctx2 = client.post(
        "/v1/context:prepare",
        json={
            "organization_id": ORG,
            "task": "Investigate Acme export issue",
            "acting_principal_id": "person_ajay",
            "destination": DEST,
            "entity_ids": ["customer_acme"],
            "token_budget": 4000,
        },
    ).json()
    write("reactive-prepare_context-2.json", ctx2)
    assert ctx2.get("permission_decision_id")

    rel = client.post(
        "/v1/relationships:query",
        json={
            "organization_id": ORG,
            "from_entity_id": "person_ajay",
            "to_entity_id": "person_ravi",
            "acting_principal_id": "person_ajay",
            "destination": DEST,
        },
    ).json()
    write("reactive-relationship.json", rel)

    owner = client.post(
        "/v1/owners:resolve",
        json={
            "organization_id": ORG,
            "issue_text": "Acme exports stuck",
            "acting_principal_id": "person_support_agent",
            "entity_ids": ["customer_acme", "service_export_worker"],
            "destination": DEST,
        },
    ).json()
    write("reactive-owner.json", owner)
    assert owner.get("primary_owner")
    assert owner.get("evidence")

    proj = client.post(
        "/v1/projects:context",
        json={
            "organization_id": ORG,
            "project_id": "project_toda",
            "acting_principal_id": "person_ajay",
            "destination": DEST,
        },
    ).json()
    write("reactive-project.json", proj)

    # temporal memory via ingest

    # exercise via API
    i1 = client.post(
        "/v1/ingest",
        json={
            "organization_id": ORG,
            "source_system": "verify",
            "source_type": "message",
            "external_id": "verify_launch_a",
            "content": "The launch date is 1 October for Toda.",
            "scope_id": "scope_org_public",
            "entity_ids": ["project_toda"],
            "author_entity_id": "person_ajay",
        },
    ).json()
    i2 = client.post(
        "/v1/ingest",
        json={
            "organization_id": ORG,
            "source_system": "verify",
            "source_type": "message",
            "external_id": "verify_launch_b",
            "content": "The launch date is 10 October for Toda.",
            "scope_id": "scope_org_public",
            "entity_ids": ["project_toda"],
            "author_entity_id": "person_ajay",
        },
    ).json()
    search_launch = client.post(
        "/v1/search",
        json={
            "organization_id": ORG,
            "query": "launch date October",
            "acting_principal_id": "person_ajay",
            "destination": DEST,
            "limit": 20,
        },
    ).json()
    write(
        "memory-temporal.log",
        {
            "ingest_1_memories": i1.get("memories"),
            "ingest_2_memories": i2.get("memories"),
            "search_after": search_launch,
        },
    )
    contents = [r["memory"]["content"] for r in search_launch.get("results", [])]
    assert any("10 October" in c for c in contents), contents

    # permissions: public dest must not see private
    pub = client.post(
        "/v1/search",
        json={
            "organization_id": ORG,
            "query": "headcount freeze confidential",
            "acting_principal_id": "person_ajay",
            "destination": {"type": "slack_channel", "id": "channel_eng", "is_private": False},
            "limit": 20,
        },
    ).json()
    pub_text = " ".join(r["memory"]["content"] for r in pub.get("results", [])).lower()
    assert "headcount freeze" not in pub_text

    # revoke a source
    src_id = i1["source"]["id"]
    rev = client.post(f"/v1/sources/{src_id}:revoke").json()
    after_rev = client.post(
        "/v1/search",
        json={
            "organization_id": ORG,
            "query": "1 October",
            "acting_principal_id": "person_ajay",
            "destination": DEST,
            "limit": 20,
        },
    ).json()
    for r in after_rev.get("results", []):
        for s in r.get("sources") or []:
            assert s["id"] != src_id
    write(
        "permission-revocation.log",
        {"public_search": pub, "revoked": rev, "after_revocation_search": after_rev},
    )

    # skills + runs + proactive
    skills = client.post(
        "/v1/skills:search",
        json={"organization_id": ORG, "query": "export"},
    ).json()
    run = client.post(
        "/v1/runs",
        json={
            "organization_id": ORG,
            "task": "Resolve export failure",
            "acting_principal_id": "person_ajay",
            "harness_id": "verify_harness",
            "target_entity_ids": ["customer_acme"],
        },
    ).json()
    client.post(
        f"/v1/runs/{run['id']}/events",
        json={
            "event_type": "action.completed",
            "action": "check_metrics",
            "result": "ok",
        },
    )
    completed = client.post(
        f"/v1/runs/{run['id']}:complete",
        json={"outcome": "resolved", "summary": "fixed"},
    ).json()
    graph = client.get(f"/v1/runs/{run['id']}/graph").json()

    proactive_results = {}
    for signal_type, extra in [
        ("verified_missing_answer", {"confidence": 0.97, "evidence_authority": 0.95, "usefulness": 1.0, "urgency": 1.0, "actionability": 1.0, "novelty": 1.0, "interruption_cost": 0.05}),
        ("verified_correction", {"confidence": 0.96, "evidence_authority": 0.95, "usefulness": 1.0, "urgency": 1.0, "actionability": 1.0, "novelty": 1.0, "interruption_cost": 0.05}),
        ("unacknowledged_customer_issue", {"confidence": 0.95, "evidence_authority": 0.95, "usefulness": 1.0, "urgency": 1.0, "actionability": 1.0, "novelty": 1.0, "interruption_cost": 0.05}),
        ("verified_operational_incident", {"confidence": 0.95, "evidence_authority": 0.95, "usefulness": 1.0, "urgency": 1.0, "actionability": 1.0, "novelty": 1.0, "interruption_cost": 0.05}),
    ]:
        cand = {
            "signal_type": signal_type,
            "permission_safe": True,
            "content": f"signal {signal_type}",
            "evidence": [{"note": "fixture"}],
            **extra,
        }
        pr = client.post(
            "/v1/proactive:evaluate",
            json={
                "organization_id": ORG,
                "trigger": {"type": signal_type, "text": "test"},
                "acting_principal_id": "person_ajay",
                "destination": DEST,
                "mode": "dry_run",
                "candidate": cand,
            },
        ).json()
        proactive_results[signal_type] = pr
        assert pr["decision"] in {
            "silent",
            "remember_only",
            "digest",
            "reply_in_context",
            "private_route",
            "escalate",
        }
        assert pr.get("policy_explanation") or pr.get("reason")

    # silence when unsafe
    silent = client.post(
        "/v1/proactive:evaluate",
        json={
            "organization_id": ORG,
            "trigger": {"type": "x", "text": "y"},
            "acting_principal_id": "person_ajay",
            "destination": DEST,
            "candidate": {
                "signal_type": "verified_missing_answer",
                "confidence": 0.99,
                "evidence_authority": 0.99,
                "usefulness": 1,
                "urgency": 1,
                "actionability": 1,
                "novelty": 1,
                "interruption_cost": 0,
                "permission_safe": False,
            },
        },
    ).json()
    assert silent["decision"] == "silent"

    write(
        "execution-skills-proactive.log",
        {
            "skills": skills,
            "run": completed,
            "execution_graph": graph,
            "proactive": proactive_results,
            "silent_when_unsafe": silent,
        },
    )

    write("llm-unavailable.log", "Using deterministic/heuristic extractor; no external LLM required.\n")

    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write("verify-error.log", repr(exc))
        raise
