"""Synthetic organization fixtures for demos and verification."""

from __future__ import annotations

import sys
from pathlib import Path

from docket.config import settings
from docket.domain.models import EntityType, ScopeKind
from docket.services.container import DocketApp, reset_app


ORG = "org_acme"


def seed_synthetic_org(brain: DocketApp, organization_id: str = ORG) -> dict:
    """Seed Acme Corp synthetic org used in the implementation spec examples."""
    store = brain.store

    store.put_organization(
        {"id": organization_id, "name": "Acme Corp", "region": "us-east-1"}
    )

    # Scopes
    scopes = [
        {
            "id": "scope_org_public",
            "organization_id": organization_id,
            "kind": ScopeKind.ORGANIZATION_PUBLIC.value,
            "label": "Organization Public",
            "is_public": True,
            "principal_ids": [],
        },
        {
            "id": "scope_channel_eng",
            "organization_id": organization_id,
            "kind": ScopeKind.PRIVATE_CHANNEL.value,
            "label": "Engineering (shared)",
            "channel_id": "channel_eng",
            "is_public": True,
            "principal_ids": ["person_ajay", "person_ravi", "person_meera"],
        },
        {
            "id": "scope_channel_leadership_private",
            "organization_id": organization_id,
            "kind": ScopeKind.PRIVATE_CHANNEL.value,
            "label": "Leadership Private",
            "channel_id": "channel_leadership",
            "is_public": False,
            "principal_ids": ["person_ajay"],
        },
        {
            "id": "scope_project_brain",
            "organization_id": organization_id,
            "kind": ScopeKind.PROJECT.value,
            "label": "Docket Project",
            "is_public": False,
            "principal_ids": ["person_ajay", "person_ravi"],
        },
    ]
    for s in scopes:
        store.upsert_scope(s)
        for pid in s.get("principal_ids") or []:
            store.add_membership(organization_id, pid, s["id"])
        # everyone gets public
        for pid in ["person_ajay", "person_ravi", "person_meera", "person_support_agent"]:
            store.add_membership(organization_id, pid, "scope_org_public")
            store.add_membership(organization_id, pid, "scope_channel_eng")

    # Entities
    entities = [
        ("person_ajay", EntityType.PERSON, "Ajay"),
        ("person_ravi", EntityType.PERSON, "Ravi"),
        ("person_meera", EntityType.PERSON, "Meera"),
        ("person_support_agent", EntityType.PERSON, "Support Agent"),
        ("team_data_platform", EntityType.TEAM, "Data Platform"),
        ("project_docket", EntityType.PROJECT, "Docket"),
        ("service_export_worker", EntityType.SERVICE, "export-worker"),
        ("customer_acme", EntityType.CUSTOMER, "Acme"),
        ("channel_eng", EntityType.CHANNEL, "#engineering"),
        ("channel_leadership", EntityType.CHANNEL, "#leadership-private"),
    ]
    for eid, etype, name in entities:
        brain.graph.upsert_entity(
            {
                "id": eid,
                "organization_id": organization_id,
                "entity_type": etype.value,
                "name": name,
                "aliases": [name.lower()],
            }
        )

    # Org edges
    edges = [
        ("MEMBER_OF", "person_ajay", "team_data_platform"),
        ("MEMBER_OF", "person_ravi", "team_data_platform"),
        ("MEMBER_OF", "person_meera", "team_data_platform"),
        ("WORKS_ON", "person_ajay", "project_docket"),
        ("WORKS_ON", "person_ravi", "project_docket"),
        ("OWNS", "person_ravi", "service_export_worker"),
        ("OWNS", "person_ajay", "project_docket"),
        ("ACCOUNT_OWNED_BY", "customer_acme", "person_ravi"),
        ("ON_CALL_FOR", "person_meera", "service_export_worker"),
        ("DEPENDS_ON", "project_docket", "service_export_worker"),
        ("ABOUT", "channel_eng", "project_docket"),
    ]
    for et, frm, to in edges:
        brain.graph.add_edge(organization_id, et, frm, to)

    # Public facts / decisions via ingestion
    public_docs = [
        {
            "external_id": "msg_launch_v1",
            "content": "The launch date is 1 September. Team selected SQLite as the definitive Docket V1 store.",
            "scope_id": "scope_org_public",
            "entity_ids": ["project_docket"],
            "author_entity_id": "person_ajay",
            "is_private_channel": False,
            "channel_id": "channel_eng",
        },
        {
            "external_id": "msg_launch_v2",
            "content": "Update: The launch date is 15 September. Decision confirmed in platform review.",
            "scope_id": "scope_org_public",
            "entity_ids": ["project_docket"],
            "author_entity_id": "person_ajay",
            "is_private_channel": False,
            "channel_id": "channel_eng",
        },
        {
            "external_id": "msg_export_issue",
            "content": "Customer Acme reports export failure. Exports have been stuck for 40 minutes after deployment 981.",
            "scope_id": "scope_org_public",
            "entity_ids": ["customer_acme", "service_export_worker"],
            "author_entity_id": "person_support_agent",
            "is_private_channel": False,
            "channel_id": "channel_eng",
        },
        {
            "external_id": "msg_commitment",
            "content": "Ajay committed to deliver the API schema by 2026-08-01 for Ravi.",
            "scope_id": "scope_org_public",
            "entity_ids": ["project_docket"],
            "author_entity_id": "person_ajay",
            "is_private_channel": False,
            "channel_id": "channel_eng",
        },
        {
            "external_id": "msg_decision_pg",
            "content": "We decided the platform team selected SQLite for Docket V1 storage, graph records, and vectors.",
            "scope_id": "scope_org_public",
            "entity_ids": ["project_docket"],
            "author_entity_id": "person_ravi",
            "is_private_channel": False,
            "channel_id": "channel_eng",
        },
    ]
    for doc in public_docs:
        brain.ingestion.ingest(
            organization_id=organization_id,
            source_system="slack",
            source_type="message",
            external_id=doc["external_id"],
            content=doc["content"],
            scope_id=doc["scope_id"],
            author_entity_id=doc["author_entity_id"],
            channel_id=doc.get("channel_id"),
            is_private_channel=doc.get("is_private_channel", False),
            entity_ids=doc["entity_ids"],
            learn=True,
        )

    # Private leadership memory (must not leak to public destinations)
    brain.ingestion.ingest(
        organization_id=organization_id,
        source_system="slack",
        source_type="message",
        external_id="msg_private_headcount",
        content="Private: leadership decided secret headcount freeze for Q4. Acme acquisition talks are confidential.",
        scope_id="scope_channel_leadership_private",
        author_entity_id="person_ajay",
        channel_id="channel_leadership",
        is_private_channel=True,
        entity_ids=["customer_acme"],
        learn=True,
    )

    # Human-authored skill
    skill = brain.skills.create(
        organization_id=organization_id,
        name="Handle Export Failure",
        description="Diagnose and resolve customer export processing failures.",
        goal="Restore export processing and confirm with customer.",
        owner_id="person_ravi",
        steps=[
            {"order": 1, "name": "Identify service", "description": "Map customer to export-worker service."},
            {"order": 2, "name": "Check deployment", "description": "Inspect recent deployments and metrics."},
            {"order": 3, "name": "Rollback if needed", "description": "Rollback bad deployment with approval."},
            {"order": 4, "name": "Verify", "description": "Confirm exports succeed and notify customer."},
        ],
        inputs=["customer_id", "symptoms"],
        preconditions=["on-call reachable"],
        warnings=["Do not restart all workers without owner approval."],
        scope_id="scope_org_public",
    )

    starter_skill_ids = [s["id"] for s in seed_starter_skills(brain, organization_id)]

    return {
        "organization_id": organization_id,
        "skill_id": skill["id"],
        "starter_skill_ids": starter_skill_ids,
        "scopes": [s["id"] for s in scopes],
        "entities": [e[0] for e in entities],
    }


def seed_starter_skills(brain: DocketApp, organization_id: str = ORG) -> list[dict]:
    """General-purpose starter procedures for any organization.

    Adapted from patterns in the hermes-agent reference harness
    (systematic-debugging, github-pr-workflow/github-issues, and the
    1-3-1 decision-communication rule), rewritten for Docket's actual
    tool surface: no shell/terminal/git, permission-scoped brain_* and
    composio_* tools, and human approval gating every write.
    """
    specs = [
        {
            "name": "Investigate Before Fixing",
            "description": "Root-cause an operational or customer-reported issue before proposing any fix.",
            "goal": "Understand why an issue is happening, backed by evidence, before recommending or taking corrective action.",
            "inputs": ["symptom description", "affected entity or customer"],
            "preconditions": ["issue is reproducible in evidence or facts, not just asserted"],
            "steps": [
                {
                    "order": 1,
                    "name": "Gather evidence",
                    "description": (
                        "brain_search the exact symptom and affected entity; brain_query_facts "
                        "for what was true and what was known at the relevant time; "
                        "brain_list_cases for related cross-source activity. Read messages and "
                        "facts rather than assuming the reported symptom is complete or accurate."
                    ),
                },
                {
                    "order": 2,
                    "name": "Check what changed",
                    "description": (
                        "Look for recent deployments, config changes, or connector events "
                        "around the time the symptom started. Prefer the earliest evidence, "
                        "not the most recent complaint."
                    ),
                },
                {
                    "order": 3,
                    "name": "Form ranked hypotheses",
                    "description": (
                        "State 2-3 plausible root causes ranked by likelihood, each with a "
                        "prediction that would confirm or rule it out using an available read tool."
                    ),
                },
                {
                    "order": 4,
                    "name": "Test the top hypothesis with a read-only check",
                    "description": (
                        "Use brain_query_facts, brain_list_cases, or a composio read tool to "
                        "confirm or rule out the leading hypothesis before proposing a fix. "
                        "If it's ruled out, re-rank and test the next one instead of guessing."
                    ),
                    "required": False,
                },
                {
                    "order": 5,
                    "name": "Propose or execute the fix",
                    "description": (
                        "State the root cause and the fix that addresses it, not just the "
                        "symptom. Any write action goes through the normal approval preview."
                    ),
                    "approvals": ["human approval required before any write tool executes"],
                },
            ],
            "warnings": [
                "Do not propose a fix before evidence supports a specific root cause.",
                "If the same class of fix has failed twice already, say so and ask a human "
                "instead of trying a third variation — that pattern usually means the "
                "diagnosis, not the fix, is wrong.",
            ],
        },
        {
            "name": "Triage a GitHub or Linear Issue",
            "description": "Search for an existing issue before filing a new one, then file or update it correctly.",
            "goal": "Avoid duplicate tickets and produce a well-formed issue or update with correct context.",
            "inputs": ["short description of the bug or request", "affected repo or project"],
            "preconditions": ["the relevant GitHub or Linear account is connected for this organization"],
            "steps": [
                {
                    "order": 1,
                    "name": "Discover the connected tool",
                    "description": (
                        "composio_list_integrations to confirm the account is connected; "
                        "composio_search for the exact action (e.g. search issues); never "
                        "invent an action slug."
                    ),
                },
                {
                    "order": 2,
                    "name": "Search for an existing issue first",
                    "description": (
                        "Use the read search action (linear_search_issues / "
                        "github_search_issues, or the composio-discovered equivalent) with the "
                        "core symptom terms before creating anything new."
                    ),
                },
                {
                    "order": 3,
                    "name": "Describe the write action's schema",
                    "description": (
                        "composio_describe the exact create/update/comment action before "
                        "calling it, so required fields and enums are correct on the first try."
                    ),
                },
                {
                    "order": 4,
                    "name": "Create or update with full context",
                    "description": (
                        "If no existing issue matches, create one with a clear title, the "
                        "evidence gathered, and links to related sources. If one matches, add a "
                        "comment instead of a duplicate. This is a write action and pauses for "
                        "approval."
                    ),
                    "approvals": ["human approval required before create/update/comment executes"],
                },
            ],
            "warnings": [
                "Never call composio_execute with a guessed action slug — always composio_search "
                "or composio_describe first.",
                "If more than one connected account could own this action, ask which account "
                "rather than guessing.",
            ],
        },
        {
            "name": "Structured Decision Recommendation (1-3-1)",
            "description": "Give a clear recommendation when someone asks what the team should do, not an open-ended list.",
            "goal": "Turn an ambiguous 'what should we do' question into one problem statement, a small set of real options, and one concrete recommendation.",
            "inputs": ["the decision or question being asked"],
            "preconditions": ["there are genuinely multiple viable approaches, not one obvious answer"],
            "steps": [
                {
                    "order": 1,
                    "name": "State the problem in one sentence",
                    "description": "Frame the actual decision or outcome needed, not the mechanism.",
                },
                {
                    "order": 2,
                    "name": "Present up to three distinct options",
                    "description": (
                        "Each option is a genuinely different approach, evidenced by what was "
                        "found via brain_search/brain_query_facts/brain_list_cases, with a "
                        "concrete tradeoff for each — not minor variations of the same idea."
                    ),
                },
                {
                    "order": 3,
                    "name": "Recommend one option directly",
                    "description": "State which option you recommend and the specific reason, based on the evidence gathered. Do not hedge across all three.",
                },
                {
                    "order": 4,
                    "name": "Give concrete next steps",
                    "description": "List the specific actions that would execute the recommendation, including which ones need human approval.",
                },
            ],
            "warnings": [
                "Do not use this format for a question with one obvious answer — it adds "
                "noise instead of clarity.",
            ],
        },
    ]
    created = []
    for spec in specs:
        existing = [
            s
            for s in brain.skills.list(organization_id)
            if s.get("name") == spec["name"]
        ]
        if existing:
            created.append(existing[0])
            continue
        created.append(
            brain.skills.create(
                organization_id=organization_id,
                name=spec["name"],
                description=spec["description"],
                goal=spec["goal"],
                steps=spec["steps"],
                inputs=spec.get("inputs", []),
                preconditions=spec.get("preconditions", []),
                warnings=spec.get("warnings", []),
                scope_id="scope_org_public",
            )
        )
    return created


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.db_path
    if db.exists():
        db.unlink()
    brain = reset_app(db)
    result = seed_synthetic_org(brain)
    print(f"Seeded synthetic org into {db}")
    print(result)
    brain.close()


if __name__ == "__main__":
    main()
