#!/usr/bin/env python3
"""Exercise the complete Slack-native company agent harness locally.

The simulation sends real Slack Events API-shaped envelopes through the gateway,
drains the durable SQLite worker, runs reactive and proactive agent invocations,
captures outcomes into memory, and checks permission isolation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


ORG = "org_simulated"
WORKSPACE = "T_DEMO"
BOT = "U_COMPANY_BRAIN"


def slack_event(
    event_id: str,
    *,
    user: str,
    channel: str,
    text: str,
    ts: str,
    mention: bool = False,
    channel_type: str = "channel",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": WORKSPACE,
        "event_id": event_id,
        "event_context": f"EC_{event_id}",
        "event": {
            "type": "app_mention" if mention else "message",
            "user": user,
            "channel": channel,
            "channel_type": channel_type,
            "ts": ts,
            "thread_ts": thread_ts,
            "text": f"<@{BOT}> {text}" if mention else text,
        },
    }


def seed(brain: Any) -> None:
    brain.store.put_organization(
        {"id": ORG, "name": "Northstar Labs", "region": "local"}
    )
    for scope in [
        {
            "id": "scope_sim_public",
            "organization_id": ORG,
            "kind": "organization_public",
            "label": "Company",
            "is_public": True,
        },
        {
            "id": "scope_sim_eng",
            "organization_id": ORG,
            "kind": "team",
            "label": "Engineering",
            "is_public": False,
        },
        {
            "id": "scope_sim_leadership",
            "organization_id": ORG,
            "kind": "private_channel",
            "label": "Leadership",
            "is_public": False,
        },
    ]:
        brain.store.upsert_scope(scope)
    memberships = [
        ("person_alice", "scope_sim_public"),
        ("person_bob", "scope_sim_public"),
        ("person_bob", "scope_sim_eng"),
        ("person_carol", "scope_sim_public"),
        ("person_carol", "scope_sim_leadership"),
    ]
    for person, scope in memberships:
        brain.store.add_membership(ORG, person, scope)
    for ident, kind, name in [
        ("person_alice", "person", "Alice Chen"),
        ("person_bob", "person", "Bob Singh"),
        ("person_carol", "person", "Carol Diaz"),
        ("team_platform", "team", "Platform"),
        ("service_api", "service", "Public API"),
        ("service_exports", "service", "Export Worker"),
        ("project_atlas", "project", "Atlas launch"),
        ("customer_northstar", "customer", "Northstar Health"),
    ]:
        brain.graph.upsert_entity(
            {
                "id": ident,
                "organization_id": ORG,
                "entity_type": kind,
                "name": name,
            }
        )
    for kind, start, end in [
        ("MEMBER_OF", "person_bob", "team_platform"),
        ("OWNS", "person_bob", "service_api"),
        ("OWNS", "person_bob", "service_exports"),
        ("ON_CALL_FOR", "person_bob", "service_api"),
        ("WORKS_ON", "person_alice", "project_atlas"),
        ("DEPENDS_ON", "project_atlas", "service_api"),
        ("ACCOUNT_OWNED_BY", "customer_northstar", "person_carol"),
    ]:
        brain.graph.add_edge(ORG, kind, start, end)
    brain.store.upsert_slack_installation(
        {
            "id": "slack_install_demo",
            "organization_id": ORG,
            "workspace_id": WORKSPACE,
            "workspace_name": "Northstar Labs",
            "bot_user_id": BOT,
        }
    )
    for source_user_id, principal_id in [
        ("U_ALICE", "person_alice"),
        ("U_BOB", "person_bob"),
        ("U_CAROL", "person_carol"),
    ]:
        brain.store.upsert_simple_harness_record(
            "identity_mappings",
            {
                "id": f"identity_{source_user_id}",
                "organization_id": ORG,
                "source": "slack",
                "source_user_id": source_user_id,
                "principal_id": principal_id,
            },
        )
    channel_policies = [
        ("C_GENERAL", "scope_sim_public", False, False),
        ("C_SUPPORT", "scope_sim_public", True, False),
        ("C_ENG", "scope_sim_eng", True, True),
        ("C_LEADERSHIP", "scope_sim_leadership", False, True),
    ]
    for channel, scope, proactive, private in channel_policies:
        brain.store.upsert_simple_harness_record(
            "channel_policies",
            {
                "id": f"policy_{channel}",
                "organization_id": ORG,
                "source": "slack",
                "channel_id": channel,
                "enabled": True,
                "observe": True,
                "proactive": proactive,
                "free_response": False,
                "thread_require_mention": False,
                "is_private": private,
                "scope_id": scope,
            },
        )
    # Seed evidence used by both reactive and proactive retrieval.
    for external_id, content, scope_id, entities, author in [
        (
            "seed_launch",
            "The Atlas launch date is September 15.",
            "scope_sim_public",
            ["project_atlas"],
            "person_alice",
        ),
        (
            "seed_export",
            "Northstar exports previously failed after queue deployment 981. "
            "Bob owns the Export Worker.",
            "scope_sim_public",
            ["customer_northstar", "service_exports", "person_bob"],
            "person_bob",
        ),
        (
            "seed_private",
            "Leadership approved a confidential hiring freeze through Q4.",
            "scope_sim_leadership",
            [],
            "person_carol",
        ),
    ]:
        brain.ingestion.ingest(
            organization_id=ORG,
            source_system="simulation_seed",
            source_type="document",
            external_id=external_id,
            content=content,
            scope_id=scope_id,
            author_entity_id=author,
            entity_ids=entities,
        )


def run_simulation(
    db_path: Path,
    *,
    live_embeddings: bool = False,
    live_models: bool = False,
) -> dict[str, Any]:
    if not live_embeddings:
        os.environ["COMPANY_BRAIN_EMBEDDING_PROVIDER"] = "deterministic"
    if not live_models:
        os.environ["COMPANY_BRAIN_MODEL_PROVIDER"] = "deterministic"
    from company_brain.services.container import BrainApp

    brain = BrainApp(db_path)
    try:
        seed(brain)
        events = [
            slack_event(
                "Ev_launch",
                user="U_ALICE",
                channel="C_GENERAL",
                text="Atlas launch date is September 15.",
                ts="1760000000.001",
            ),
            slack_event(
                "Ev_incident",
                user="U_BOB",
                channel="C_SUPPORT",
                text=(
                    "Northstar exports are failing again after deployment 981 "
                    "and the customer is blocked."
                ),
                ts="1760000001.001",
            ),
            slack_event(
                "Ev_private",
                user="U_CAROL",
                channel="C_LEADERSHIP",
                text="The confidential Q4 hiring freeze remains in place.",
                ts="1760000002.001",
            ),
            slack_event(
                "Ev_mention",
                user="U_ALICE",
                channel="C_GENERAL",
                text="Who owns the Export Worker and what happened before?",
                ts="1760000003.001",
                mention=True,
            ),
            slack_event(
                "Ev_dm",
                user="U_ALICE",
                channel="D_ALICE",
                channel_type="im",
                text="When does Atlas launch?",
                ts="1760000004.001",
            ),
        ]
        accepted = [
            brain.slack_gateway.accept(
                body=json.dumps(event).encode(),
                headers={},
                allow_unsigned=True,
            )
            for event in events
        ]
        duplicate = brain.slack_gateway.accept(
            body=json.dumps(events[-1]).encode(),
            headers={},
            allow_unsigned=True,
        )
        worker = brain.harness_worker.drain_until_idle(max_cycles=100)

        runs = brain.store.list_harness_records("agent_runs", ORG)
        evaluations = brain.store.list_harness_records(
            "attention_evaluations", ORG
        )
        interventions = brain.store.list_harness_records(
            "agent_interventions", ORG
        )
        deliveries = brain.store.list_harness_records("deliveries", ORG)
        public_private_search = brain.retrieval.search(
            organization_id=ORG,
            query="confidential hiring freeze Q4",
            acting_principal_id="person_alice",
            destination={
                "type": "slack_channel",
                "id": "C_GENERAL",
                "is_private": False,
            },
        )
        private_text = " ".join(
            item["memory"]["content"]
            for item in public_private_search["results"]
        ).lower()
        incident_eval = next(
            item for item in evaluations if item["event_id"] == "Ev_incident"
        )
        private_eval = next(
            item for item in evaluations if item["event_id"] == "Ev_private"
        )
        mention_run = next(
            item for item in runs if item.get("trigger_event_id") == "Ev_mention"
        )
        dm_run = next(
            item for item in runs if item.get("trigger_event_id") == "Ev_dm"
        )
        proactive_run = next(
            item for item in runs if item.get("trigger_event_id") == "Ev_incident"
        )
        captured_run_ids = {
            source.get("external_id")
            for source in brain.store.list_sources(ORG)
            if source.get("source_type") == "agent_run"
        }
        checks = [
            (
                "Slack events accepted",
                all(item.get("accepted") for item in accepted),
                f"{sum(bool(item.get('accepted')) for item in accepted)}/{len(events)}",
            ),
            (
                "Slack event deduplication",
                duplicate.get("duplicate") is True,
                str(duplicate),
            ),
            (
                "Mention bypasses attention and completes",
                mention_run["trigger_kind"] == "reactive"
                and mention_run["state"] == "completed",
                mention_run["id"],
            ),
            (
                "DM bypasses attention and completes",
                dm_run["trigger_kind"] == "reactive"
                and dm_run["state"] == "completed",
                dm_run["id"],
            ),
            (
                "Contextual attention wakes main agent",
                incident_eval["decision"] == "WAKE_MAIN_AGENT"
                and proactive_run["state"] == "completed",
                incident_eval["reason"],
            ),
            (
                "Non-opted private channel does not wake",
                private_eval["decision"] == "ADD_TO_DIGEST",
                private_eval["reason"],
            ),
            (
                "Permission-safe public retrieval",
                "hiring freeze" not in private_text,
                f"{public_private_search['count']} public result(s)",
            ),
            (
                "Proactive intervention delivered",
                any(
                    item["run_id"] == proactive_run["id"]
                    for item in interventions
                ),
                f"{len(interventions)} intervention(s)",
            ),
            (
                "All completed runs captured into memory",
                all(
                    run["id"] in captured_run_ids
                    for run in runs
                    if run["state"] == "completed"
                ),
                f"{len(captured_run_ids)} captured run(s)",
            ),
            (
                "Durable worker has no failures",
                worker["failed"] == 0,
                f"{worker['completed']} jobs completed",
            ),
        ]
        return {
            "database": str(db_path),
            "embedding_provider": type(brain.embeddings).__name__,
            "model_provider": getattr(brain.main_model, "model", "unknown"),
            "events": len(events),
            "runs": len(runs),
            "attention_evaluations": len(evaluations),
            "interventions": len(interventions),
            "deliveries": len(deliveries),
            "sources": len(brain.store.list_sources(ORG)),
            "memories": len(
                brain.store.list_memories(ORG, latest_only=False)
            ),
            "checks": [
                {"name": name, "passed": passed, "detail": detail}
                for name, passed, detail in checks
            ],
            "passed": all(passed for _, passed, _ in checks),
        }
    finally:
        brain.close()


def markdown(report: dict[str, Any]) -> str:
    output = [
        "# Company Brain full-harness simulation",
        "",
        f"- Database: {report['database']}",
        f"- Embeddings: {report['embedding_provider']}",
        f"- Agent model: {report['model_provider']}",
        f"- Slack events: {report['events']}",
        f"- Agent runs: {report['runs']}",
        f"- Attention evaluations: {report['attention_evaluations']}",
        f"- Proactive interventions: {report['interventions']}",
        f"- Deliveries: {report['deliveries']}",
        f"- Sources / memories: {report['sources']} / {report['memories']}",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    output += [
        f"| {check['name']} | {'PASS' if check['passed'] else 'FAIL'} | "
        f"{check['detail']} |"
        for check in report["checks"]
    ]
    return "\n".join(output) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--live-embeddings", action="store_true")
    parser.add_argument("--live-models", action="store_true")
    args = parser.parse_args()
    if args.db:
        report = run_simulation(
            args.db,
            live_embeddings=args.live_embeddings,
            live_models=args.live_models,
        )
    else:
        with tempfile.TemporaryDirectory() as directory:
            report = run_simulation(
                Path(directory) / "simulation.db",
                live_embeddings=args.live_embeddings,
                live_models=args.live_models,
            )
    text = markdown(report)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
