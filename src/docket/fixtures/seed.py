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

    return {
        "organization_id": organization_id,
        "skill_id": skill["id"],
        "scopes": [s["id"] for s in scopes],
        "entities": [e[0] for e in entities],
    }


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
