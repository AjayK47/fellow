"""End-to-end requirements for the company reality engine and child Tasks."""

from __future__ import annotations

from tests.conftest import sign_in
from toda.harness.models import (
    DestinationEnvelope,
    InvocationEnvelope,
    ModelToolCall,
)
from toda.services.policy import PolicyEngine


def _account(brain):
    account = {
        "id": "connector_test_linear",
        "organization_id": "org_acme",
        "toolkit": "linear",
        "connected_account_id": "ca_test_linear",
        "status": "active",
        "composio_user_id": "toda:org_acme",
        "principal_id": "connector:ca_test_linear",
        "scope_id": "scope_org_public",
        "learn": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    brain.store.upsert_connector_account(account)
    return account


def test_ingestion_materializes_object_identity_fact_and_case(brain):
    _account(brain)
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="linear",
        source_type="issue.updated",
        external_id="LIN-501",
        content="Issue: Toda status is investigating. Ajay owns the incident.",
        scope_id="scope_org_public",
        author_entity_id="person_ajay",
        entity_ids=["project_toda"],
        metadata={
            "object_type": "issue",
            "object_id": "LIN-501",
            "connected_account_id": "ca_test_linear",
            "raw_data": {
                "id": "LIN-501",
                "title": "Toda incident",
                "status": "investigating",
                "owner_id": "ajay-linear",
                "owner_email": "ajay@example.com",
                "owner_name": "Ajay",
            },
        },
        occurred_at="2026-03-04T05:06:07+00:00",
    )
    reality = result["reality"]
    assert reality["connector_object"]["external_id"] == "LIN-501"
    assert reality["connector_object"]["version"] == 1
    assert "person_ajay" in reality["entity_ids"]
    assert reality["facts"]
    assert result["source"]["occurred_at"] == "2026-03-04T05:06:07Z"
    assert all(
        fact["valid_from"] in {
            "2026-03-04T05:06:07Z",
            "2026-03-04T05:06:07+00:00",
        }
        for fact in reality["facts"]
    )
    assert reality["case"]["status"] == "open"

    identifier = brain.reality.lookup_identifier(
        "org_acme", "email", "AJAY@example.com"
    )
    assert identifier and identifier["entity_id"] == "person_ajay"


def test_connector_backfill_is_resumable_idempotent_and_learned(brain):
    account = _account(brain)
    run = brain.reality.begin_sync(
        organization_id="org_acme",
        connector_account_id=account["id"],
        object_type="issue",
    )
    result = brain._process_connector_backfill(
        {
            "sync_run_id": run["id"],
            "provider": "linear",
            "records": [
                {
                    "id": "LIN-900",
                    "title": "Export worker is failing",
                    "content": "Issue: export-worker is failing for Acme.",
                    "status": "open",
                }
            ],
            "next_cursor": "cursor-2",
        }
    )
    assert result["sync_run"]["status"] == "completed"
    assert result["sync_run"]["processed"] == 1
    cursor = brain.reality.get_sync_cursor(
        "org_acme", account["id"], "issue"
    )
    assert cursor["cursor"] == "cursor-2"

    # A replay produces a source version but not a duplicate canonical object.
    before = _connector_objects(brain, "LIN-900")
    run2 = brain.reality.begin_sync(
        organization_id="org_acme",
        connector_account_id=account["id"],
        object_type="issue",
    )
    brain._process_connector_backfill(
        {
            "sync_run_id": run2["id"],
            "provider": "linear",
            "records": [
                {
                    "id": "LIN-900",
                    "title": "Export worker is failing",
                    "content": "Issue: export-worker is failing for Acme.",
                    "status": "open",
                }
            ],
            "next_cursor": "cursor-2",
        }
    )
    assert len(_connector_objects(brain, "LIN-900")) == len(before)


def test_connector_backfill_paginates_remote_action_and_persists_cursor(brain):
    account = _account(brain)
    calls = []
    pages = [
        {
            "data": {
                "items": [{"id": "LIN-901", "title": "First historical issue"}],
                "next_cursor": "page-2",
            }
        },
        {
            "data": {
                "items": [{"id": "LIN-902", "title": "Second historical issue"}],
                "next_cursor": None,
            }
        },
    ]

    def execute(action_slug, arguments, *, connected_account_id, user_id=None):
        calls.append((action_slug, arguments, connected_account_id))
        return pages.pop(0)

    brain.composio.execute = execute
    run = brain.reality.begin_sync(
        organization_id="org_acme",
        connector_account_id=account["id"],
        object_type="issue",
    )
    result = brain._process_connector_backfill(
        {
            "sync_run_id": run["id"],
            "provider": "linear",
            "action_slug": "LINEAR_LIST_ISSUES",
            "action_arguments": {"team_id": "team-1"},
            "items_path": "data.items",
            "next_cursor_path": "data.next_cursor",
            "cursor_argument": "after",
            "max_pages": 10,
        }
    )
    assert result["pages"] == 2
    assert result["sync_run"]["processed"] == 2
    assert calls[0][1] == {"team_id": "team-1"}
    assert calls[1][1] == {"team_id": "team-1", "after": "page-2"}
    assert calls[0][2] == "ca_test_linear"
    assert {item["external_id"] for item in brain.reality.list_connector_objects(
        "org_acme", provider="linear", object_type="issue"
    )} >= {"LIN-901", "LIN-902"}


def _connector_objects(brain, external_id):
    with brain.store.cursor() as cur:
        cur.execute(
            "SELECT payload FROM connector_objects WHERE external_id = ?",
            (external_id,),
        )
        return cur.fetchall()


def test_bitemporal_facts_preserve_history_and_resolve_conflicts(brain):
    first = brain.reality.assert_fact(
        organization_id="org_acme",
        subject_id="project_toda",
        predicate="status",
        object_value="planning",
        scope_id="scope_org_public",
        valid_from="2026-01-01T00:00:00+00:00",
        source_ids=["source_old"],
        confidence=0.8,
        authority=0.7,
    )
    known_before = first["system_from"]
    second = brain.reality.assert_fact(
        organization_id="org_acme",
        subject_id="project_toda",
        predicate="status",
        object_value="active",
        scope_id="scope_org_public",
        valid_from="2026-02-01T00:00:00+00:00",
        source_ids=["source_new"],
        confidence=0.95,
        authority=0.9,
    )
    current = brain.reality.current_facts(
        "org_acme",
        subject_id="project_toda",
        predicate="status",
        valid_at="2026-03-01T00:00:00+00:00",
    )
    assert [fact["object_value"] for fact in current] == ["active"]
    historical = brain.reality.current_facts(
        "org_acme",
        subject_id="project_toda",
        predicate="status",
        valid_at="2026-01-15T00:00:00+00:00",
        known_at=known_before,
    )
    assert [fact["object_value"] for fact in historical] == ["planning"]
    conflicts = brain.reality.list_conflicts("org_acme")
    conflict = next(
        item for item in conflicts if second["id"] in item["fact_ids"]
    )
    assert conflict["kind"] == "mutually_exclusive"
    assert conflict["resolution"]["winner_fact_id"] == second["id"]


def test_cases_correlate_events_across_tools(brain):
    one = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="message",
        external_id="slack-case-1",
        content="Issue: Acme export-worker failures started today.",
        scope_id="scope_org_public",
        entity_ids=["customer_acme", "service_export_worker"],
    )
    two = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="github",
        source_type="issue",
        external_id="gh-case-1",
        content="Issue: export-worker is failing for Acme after deploy.",
        scope_id="scope_org_public",
        entity_ids=["customer_acme", "service_export_worker"],
    )
    assert one["reality"]["case"]["id"] == two["reality"]["case"]["id"]
    timeline = brain.reality.case_timeline(one["reality"]["case"]["id"])
    assert {
        one["source"]["id"],
        two["source"]["id"],
    }.issubset({event["source_id"] for event in timeline})
    updated = brain.reality.update_case(
        one["reality"]["case"]["id"],
        status="resolved",
        updated_by="person_ajay",
        summary="Retry storm mitigated.",
    )
    assert updated["status"] == "resolved"
    assert updated["status_history"][-1]["from"] == "open"


def test_judgments_become_retrievable_guidance(brain):
    item = brain.reality.record_judgment(
        organization_id="org_acme",
        judgment_type="agent_feedback",
        principal_id="person_ajay",
        subject={"task_id": "task-1"},
        decision="correction",
        reason="Always include the customer impact.",
    )
    guidance = brain.reality.judgment_guidance(
        "org_acme", "person_ajay"
    )
    assert item["id"] in {value["id"] for value in guidance}


def test_conditional_policy_default_deny_and_forbid_precedence(brain):
    brain.policy.create_version(
        organization_id="org_acme",
        policy_key="linear-writes",
        effect="permit",
        actions=["integration_execute"],
        conditions={"destination.type": "dm"},
        approval_required=True,
        created_by="person_ajay",
        status="active",
    )
    allowed = brain.policy.evaluate(
        organization_id="org_acme",
        action="integration_execute",
        context={"destination": {"type": "dm"}},
    )
    denied_by_default = brain.policy.evaluate(
        organization_id="org_acme",
        action="integration_execute",
        context={"destination": {"type": "slack_channel"}},
    )
    assert allowed["decision"] == "allow"
    assert allowed["approval_required"] is True
    assert denied_by_default["decision"] == "deny"

    brain.policy.create_version(
        organization_id="org_acme",
        policy_key="incident-freeze",
        effect="forbid",
        actions=["integration_execute"],
        conditions={"action.arguments.status": "closed"},
        created_by="person_ajay",
        status="active",
    )
    forbidden = brain.policy.evaluate(
        organization_id="org_acme",
        action="integration_execute",
        context={
            "destination": {"type": "dm"},
            "action": {"arguments": {"status": "closed"}},
        },
    )
    assert forbidden["decision"] == "deny"


def test_execution_preconditions_detect_state_change(brain):
    states = [{"id": "LIN-1", "version": 1}, {"id": "LIN-1", "version": 2}]

    def reader(**_kwargs):
        return states.pop(0)

    engine = PolicyEngine(brain.store, brain.audit, external_reader=reader)
    captured = engine.capture_preconditions(
        organization_id="org_acme",
        approval_id="approval-precondition",
        specifications=[
            {
                "action_slug": "LINEAR_GET_ISSUE",
                "arguments": {"issue_id": "$args.issue_id"},
            }
        ],
        runtime={},
        arguments={"issue_id": "LIN-1"},
    )
    assert captured
    check = engine.revalidate(
        approval_id="approval-precondition",
        runtime={},
        arguments={"issue_id": "LIN-1"},
    )
    assert check["valid"] is False
    assert check["changes"][0]["status"] == "changed"


def test_approved_tool_is_not_executed_when_precondition_changes(brain):
    _account(brain)
    states = [
        {"data": {"id": "LIN-1", "version": 1}},
        {"data": {"id": "LIN-1", "version": 2}},
    ]
    brain.policy.external_reader = lambda **_kwargs: states.pop(0)
    brain.policy.create_version(
        organization_id="org_acme",
        policy_key="linear-safe-update",
        effect="permit",
        actions=["integration_execute"],
        approval_required=True,
        precondition={
            "action_slug": "LINEAR_GET_ISSUE",
            "connected_account_id": "ca_test_linear",
            "arguments": {"issue_id": "$args.arguments.issue_id"},
        },
        created_by="person_ajay",
        status="active",
    )
    decision = brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id="person_ajay",
        destination={"type": "dm", "id": "person_ajay", "is_private": True},
        purpose="reactive_agent",
    )
    run = brain.company_agent.create_invocation(
        envelope=InvocationEnvelope(
            organization_id="org_acme",
            principal_id="person_ajay",
            conversation_id="precondition-run",
            destination=DestinationEnvelope(
                type="dm", id="person_ajay", is_private=True
            ),
            purpose="reactive_agent",
            permission_decision_id=decision.id,
            allowed_scope_ids=decision.allowed_scope_ids,
        ),
        goal="Update LIN-1",
        trigger_kind="reactive",
    )
    proposal = brain.tool_executor.execute(
        run=run,
        call=ModelToolCall(
            id="call-precondition",
            name="integration_execute",
            arguments={
                "tool_slug": "LINEAR_UPDATE_ISSUE",
                "arguments": {"issue_id": "LIN-1", "status": "closed"},
                "connected_account_id": "ca_test_linear",
            },
        ),
    )
    assert proposal.status == "waiting_for_approval"
    approval, result = brain.tool_executor.resolve_approval(
        approval_id=proposal.approval_id,
        approved=True,
        decided_by="person_ajay",
    )
    assert approval["status"] == "stale"
    assert result.status == "failed"
    assert "changed after approval" in result.error



def test_reality_api_exposes_permission_filtered_facts_and_artifacts(api_client):
    fact = api_client.post(
        "/v1/facts",
        json={
            "organization_id": "org_acme",
            "subject_id": "project_toda",
            "predicate": "status",
            "object_value": "active",
            "scope_id": "scope_org_public",
            "valid_from": "2026-01-01T00:00:00+00:00",
        },
    )
    assert fact.status_code == 200
    facts = api_client.get(
        "/v1/facts",
        params={
            "organization_id": "org_acme",
            "acting_principal_id": "person_ajay",
            "subject_id": "project_toda",
        },
    )
    assert facts.status_code == 200
    assert any(
        item["id"] == fact.json()["id"] for item in facts.json()["facts"]
    )

    artifact = api_client.post(
        "/v1/artifacts",
        json={
            "organization_id": "org_acme",
            "artifact_type": "report",
            "title": "Incident report",
            "content": {"summary": "Export worker recovered."},
            "scope_id": "scope_org_public",
            "created_by": "person_ajay",
        },
    )
    assert artifact.status_code == 200
    artifacts = api_client.get(
        "/v1/artifacts",
        params={
            "organization_id": "org_acme",
            "acting_principal_id": "person_ajay",
        },
    )
    assert artifacts.status_code == 200
    assert any(
        item["id"] == artifact.json()["id"]
        for item in artifacts.json()["artifacts"]
    )

    private_fact = api_client.post(
        "/v1/facts",
        json={
            "organization_id": "org_acme",
            "subject_id": "company",
            "predicate": "status",
            "object_value": "confidential",
            "scope_id": "scope_channel_leadership_private",
            "valid_from": "2026-01-01T00:00:00+00:00",
        },
    ).json()
    private_artifact = api_client.post(
        "/v1/artifacts",
        json={
            "organization_id": "org_acme",
            "artifact_type": "report",
            "title": "Leadership only",
            "content": {"summary": "Private"},
            "scope_id": "scope_channel_leadership_private",
            "created_by": "person_ajay",
        },
    ).json()
    # Who is asking comes from the signed-in session, never from a request
    # parameter -- so seeing the API as a different person means signing in
    # as them, exactly as a real second user would.
    sign_in(api_client, principal_id="person_ravi")
    ravi_facts = api_client.get(
        "/v1/facts", params={"organization_id": "org_acme"}
    ).json()["facts"]
    ravi_artifacts = api_client.get(
        "/v1/artifacts", params={"organization_id": "org_acme"}
    ).json()["artifacts"]
    assert private_fact["id"] not in {item["id"] for item in ravi_facts}
    assert private_artifact["id"] not in {item["id"] for item in ravi_artifacts}
    assert api_client.get(
        f"/v1/artifacts/{private_artifact['id']}"
    ).status_code == 404

    # The parameter this route used to take is now inert. Ravi naming the
    # principal who CAN see the private scope must change nothing -- that
    # self-assertion was readable-as-anyone, since a DM destination unlocks
    # the named actor's entire membership set.
    still_hidden = api_client.get(
        "/v1/facts",
        params={
            "organization_id": "org_acme",
            "acting_principal_id": "person_ajay",
        },
    ).json()["facts"]
    assert private_fact["id"] not in {item["id"] for item in still_hidden}
    assert api_client.get(
        f"/v1/artifacts/{private_artifact['id']}",
        params={"acting_principal_id": "person_ajay"},
    ).status_code == 404


def test_policy_changes_are_pending_until_explicit_activation(api_client):
    created = api_client.post(
        "/v1/policies",
        json={
            "organization_id": "org_acme",
            "policy_key": "company-writes",
            "effect": "permit",
            "actions": ["integration_execute"],
            "conditions": {"destination.type": "dm"},
            "approval_required": True,
            "created_by": "person_ajay",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "pending"
    activated = api_client.post(
        f"/v1/policies/{created.json()['id']}:activate",
        json={"approved_by": "person_ajay"},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"


def test_tenant_deletion_covers_reality_and_artifact_tables(empty_brain):
    brain = empty_brain
    brain.store.put_organization({"id": "org_delete", "name": "Delete Me"})
    brain.store.upsert_scope(
        {
            "id": "scope_delete",
            "organization_id": "org_delete",
            "kind": "organization_public",
            "label": "Delete",
            "is_public": True,
        }
    )
    brain.reality.assert_fact(
        organization_id="org_delete",
        subject_id="thing",
        predicate="status",
        object_value="temporary",
        scope_id="scope_delete",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    brain.artifacts.create(
        organization_id="org_delete",
        artifact_type="report",
        title="Temporary",
        content="delete",
        scope_id="scope_delete",
    )
    result = brain.retention.delete_organization(
        "org_delete", confirmation="org_delete"
    )
    assert result["deleted"]["reality_facts"] == 1
    assert result["deleted"]["artifacts"] == 1


def test_short_generic_label_does_not_fuzzy_merge_into_unrelated_entity(brain):
    org = "org_acme"
    brain.graph.upsert_entity(
        {"id": "person_al_chen", "organization_id": org, "entity_type": "person", "name": "Al Chen"}
    )
    # "al" is short and low-entropy -- a fuzzy ratio against "Al Chen" can
    # score deceptively high, which is exactly the false-positive-merge
    # scenario the entropy gate (ported from Graphiti's dedup_helpers)
    # exists to catch. Should refuse to guess rather than silently merge.
    match = brain.reality._candidate_entity(
        org, {"namespace": "user_id", "value": "al", "label": "al", "strength": "hard"}
    )
    assert match is None


def test_exact_label_match_resolves_via_fast_path(brain):
    org = "org_acme"
    brain.graph.upsert_entity(
        {
            "id": "person_priya",
            "organization_id": org,
            "entity_type": "person",
            "name": "Priya Nair",
            "aliases": ["priya"],
        }
    )
    match = brain.reality._candidate_entity(
        org, {"namespace": "user_id", "value": "priya", "label": "priya", "strength": "hard"}
    )
    assert match is not None
    assert match["id"] == "person_priya"


def test_long_distinctive_label_still_fuzzy_resolves(brain):
    org = "org_acme"
    brain.graph.upsert_entity(
        {"id": "person_sarah", "organization_id": org, "entity_type": "person", "name": "Sarah Lee"}
    )
    match = brain.reality._candidate_entity(
        org, {"namespace": "user_id", "value": "sara-lee", "label": "sara lee", "strength": "hard"}
    )
    assert match is not None
    assert match["id"] == "person_sarah"


# ---------------------------------------------------------------------
# propose_entity_mention: the missing half of graph population.
#
# resolve_source_entities only ever creates a business entity from a "hard"
# identifier in a structured connector payload (an email, a customer_id).
# An ordinary Slack conversation has none of that -- a name mentioned in
# free text is the only signal chat-based extraction ever gets. Before this
# method existed, the graph could only ever grow person/conversation/
# source/task entities: every project, team, customer and incident anyone
# ever talked about was invisible to it.
# ---------------------------------------------------------------------


def test_propose_entity_mention_with_no_match_creates_a_new_entity(brain):
    org = "org_acme"
    entity_id, decision = brain.reality.propose_entity_mention(
        org,
        name="Project Atlas",
        entity_type="project",
        source_id="src_test",
    )
    assert decision == "created"
    assert entity_id is not None
    entity = brain.graph.get_entity(entity_id)
    assert entity["name"] == "Project Atlas"
    assert entity["entity_type"] == "project"
    assert entity["attributes"]["created_by_extraction"] is True


def test_propose_entity_mention_with_an_exact_match_links_directly(brain):
    org = "org_acme"
    brain.graph.upsert_entity(
        {
            "id": "customer_acme_corp",
            "organization_id": org,
            "entity_type": "customer",
            "name": "Acme Corp",
        }
    )
    before = len(brain.store.list_entities(org, entity_types=["customer"]))
    entity_id, decision = brain.reality.propose_entity_mention(
        org, name="Acme Corp", entity_type="customer", source_id="src_test"
    )
    assert decision == "matched"
    assert entity_id == "customer_acme_corp"
    # No duplicate was created for an exact match.
    assert len(brain.store.list_entities(org, entity_types=["customer"])) == before


def test_propose_entity_mention_with_a_fuzzy_match_queues_review_not_a_link(brain):
    """A merely-similar name might be a DIFFERENT real entity -- auto-
    linking it would silently merge two distinct things. This must queue a
    pending resolution candidate (the same review flow a soft identifier
    match already uses) and leave the mention unresolved, never invent a
    new duplicate entity, and never guess by auto-linking either."""
    org = "org_acme"
    brain.graph.upsert_entity(
        {
            "id": "project_atlas_v1",
            "organization_id": org,
            "entity_type": "project",
            "name": "Project Atlas",
        }
    )
    before = len(brain.store.list_entities(org, entity_types=["project"]))
    entity_id, decision = brain.reality.propose_entity_mention(
        org, name="Project Atlas Two", entity_type="project", source_id="src_test"
    )
    assert decision == "candidate"
    assert entity_id is None
    # No new entity was created for a merely-similar match either.
    assert len(brain.store.list_entities(org, entity_types=["project"])) == before
    candidates = brain.reality.list_resolution_candidates(org, status="pending")
    assert any(c["left_entity_id"] == "project_atlas_v1" for c in candidates)


def test_approving_a_mention_candidate_lets_the_next_mention_resolve_fast(brain):
    """Confirming the candidate must actually close the loop: the next time
    this exact name comes up, it should hit the fast exact/identifier path
    instead of queuing another candidate."""
    org = "org_acme"
    brain.graph.upsert_entity(
        {
            "id": "project_atlas_v1",
            "organization_id": org,
            "entity_type": "project",
            "name": "Project Atlas",
        }
    )
    brain.reality.propose_entity_mention(
        org, name="Project Atlas Two", entity_type="project", source_id="src_test"
    )
    [candidate] = brain.reality.list_resolution_candidates(org, status="pending")
    brain.reality.decide_resolution_candidate(
        candidate["id"], approved=True, decided_by="person_ajay"
    )

    entity_id, decision = brain.reality.propose_entity_mention(
        org, name="Project Atlas Two", entity_type="project", source_id="src_test_2"
    )
    assert decision == "matched"
    assert entity_id == "project_atlas_v1"


def test_propose_entity_mention_reuses_the_established_matching_logic(brain):
    """_candidate_entity's short/generic-label entropy gate must apply here
    too, via the shared _match_entity helper -- otherwise this new path
    would be looser than the one it was modeled on."""
    org = "org_acme"
    brain.graph.upsert_entity(
        {"id": "person_al", "organization_id": org, "entity_type": "person", "name": "Al Chen"}
    )
    entity_id, decision = brain.reality.propose_entity_mention(
        org, name="al", entity_type="person", source_id="src_test"
    )
    # Too short/low-entropy to trust a fuzzy ratio -- must not silently
    # merge into "Al Chen", so a new entity is created instead.
    assert decision == "created"
    assert entity_id != "person_al"
