"""HTTP API surface for Company Brain V1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from company_brain.services.container import BrainApp
from company_brain.config import Settings
from company_brain.harness.models import InvocationEnvelope, TriggerKind
from company_brain.harness.slack import verify_slack_signature
from company_brain.harness.tools import refresh_composio_tool_schemas

router = APIRouter()


def _app(request: Request) -> BrainApp:
    return request.app.state.brain


# ---- request models ----


class IngestRequest(BaseModel):
    organization_id: str
    source_system: str = "api"
    source_type: str = "message"
    external_id: str
    content: str
    scope_id: str
    author_entity_id: str | None = None
    channel_id: str | None = None
    is_private_channel: bool = False
    entity_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    learn: bool = True
    processing_mode: str = "memory"


class DocumentRequest(IngestRequest):
    """Stable document interface; a repeat external_id produces a source version."""


class CloudEventRequest(BaseModel):
    specversion: str = "1.0"
    id: str
    source: str
    type: str
    subject: str | None = None
    time: str | None = None
    datacontenttype: str = "application/json"
    organizationid: str
    principalid: str | None = None
    applicationid: str | None = None
    traceid: str | None = None
    scopeid: str | None = None
    purpose: str | None = None
    processing_mode: str = "memory"
    data: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    organization_id: str
    query: str
    acting_principal_id: str
    destination: dict[str, Any] | None = None
    purpose: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    limit: int = 20
    include_superseded: bool = False
    as_of: str | None = None


class ContextRequest(BaseModel):
    organization_id: str
    task: str
    acting_principal_id: str
    agent_id: str | None = None
    purpose: str | None = None
    destination: dict[str, Any] | None = None
    conversation: dict[str, Any] | None = None
    entity_ids: list[str] = Field(default_factory=list)
    token_budget: int = 8000
    freshness: str | None = None


class RelationshipRequest(BaseModel):
    organization_id: str
    from_entity_id: str
    to_entity_id: str
    acting_principal_id: str
    destination: dict[str, Any] | None = None
    purpose: str | None = None
    relationship_types: list[str] = Field(default_factory=list)


class OwnerRequest(BaseModel):
    organization_id: str
    issue_text: str
    acting_principal_id: str
    entity_ids: list[str] = Field(default_factory=list)
    include_on_call: bool = True
    destination: dict[str, Any] | None = None
    purpose: str | None = None


class ProjectContextRequest(BaseModel):
    organization_id: str
    project_id: str
    acting_principal_id: str
    destination: dict[str, Any] | None = None
    purpose: str | None = None


class EntityRequest(BaseModel):
    id: str
    organization_id: str
    entity_type: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class EdgeRequest(BaseModel):
    organization_id: str
    edge_type: str
    from_id: str
    to_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)


class SkillCreateRequest(BaseModel):
    organization_id: str
    name: str
    description: str
    goal: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    owner_id: str | None = None
    inputs: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    scope_id: str = "scope_org_public"
    status: str = "approved"


class SkillSearchRequest(BaseModel):
    organization_id: str
    query: str
    limit: int = 20


class RunStartRequest(BaseModel):
    organization_id: str
    task: str
    acting_principal_id: str
    harness_id: str
    skill_id: str | None = None
    target_entity_ids: list[str] = Field(default_factory=list)


class RunEventRequest(BaseModel):
    event_type: str
    agent_id: str | None = None
    action: str | None = None
    target_entity_id: str | None = None
    result: str | None = None
    evidence_source_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunCompleteRequest(BaseModel):
    outcome: str
    summary: str | None = None
    evidence_source_ids: list[str] = Field(default_factory=list)


class ProactiveRequest(BaseModel):
    organization_id: str
    trigger: dict[str, Any]
    acting_principal_id: str
    destination: dict[str, Any] | None = None
    mode: str = "dry_run"
    candidate: dict[str, Any] | None = None


class ScopeRequest(BaseModel):
    id: str
    organization_id: str
    kind: str
    label: str
    channel_id: str | None = None
    principal_ids: list[str] = Field(default_factory=list)
    is_public: bool = False


class MembershipRequest(BaseModel):
    organization_id: str
    principal_id: str
    scope_id: str


class AgentInvokeRequest(BaseModel):
    organization_id: str
    principal_id: str
    conversation_id: str
    destination: dict[str, Any]
    goal: str
    purpose: str = "reactive_agent"
    application_id: str = "company_brain_api"
    trigger_kind: str = "reactive"
    run_now: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    decided_by: str
    edited_arguments: dict[str, Any] | None = None
    run_now: bool = False


class ChannelPolicyRequest(BaseModel):
    organization_id: str
    source: str = "slack"
    channel_id: str
    enabled: bool = True
    observe: bool = True
    proactive: bool = False
    free_response: bool = False
    thread_require_mention: bool = False
    is_private: bool = False
    scope_id: str | None = None


class IdentityMappingRequest(BaseModel):
    organization_id: str
    source: str = "slack"
    source_user_id: str
    principal_id: str


class AutomationRequest(BaseModel):
    organization_id: str
    name: str
    goal: str
    principal_id: str
    conversation_id: str
    destination: dict[str, Any]
    interval_seconds: int = 3600
    enabled: bool = True


class ToolPolicyRequest(BaseModel):
    organization_id: str
    tool_name: str
    enabled: bool = True
    approval_required: bool | None = None


class ConnectorAccountRequest(BaseModel):
    id: str
    organization_id: str
    toolkit: str
    connected_account_id: str
    status: str = "active"


class HarnessFeedbackRequest(BaseModel):
    organization_id: str
    feedback_type: str
    principal_id: str
    run_id: str | None = None
    attention_evaluation_id: str | None = None
    comment: str | None = None


class SlackInstallationRequest(BaseModel):
    id: str
    organization_id: str
    workspace_id: str
    workspace_name: str | None = None
    bot_user_id: str | None = None


class RetentionRequest(BaseModel):
    organization_id: str
    older_than_days: int = Field(ge=1)
    confirmed_by: str | None = None
    apply: bool = False


class OrganizationDeletionRequest(BaseModel):
    organization_id: str
    confirmation: str


class DeferredMaintenanceRequest(BaseModel):
    organization_id: str
    entity_id: str | None = None


# ---- health ----


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "company-brain"}


@router.get("/v1/metrics")
def metrics(request: Request, organization_id: str | None = None) -> dict[str, Any]:
    brain = _app(request)
    org = organization_id or "org_acme"
    events = brain.audit.list_events(org, "http.request")
    durations = [float(event.get("payload", {}).get("duration_ms", 0)) for event in events]
    return {
        "organization_id": org,
        "requests": len(events),
        "average_request_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
        "sources": len(brain.store.list_sources(org, include_revoked=True)),
        "memories": len(brain.store.list_memories(org, latest_only=False)),
        "signals": len(brain.store.list_signals(org)),
        "runs": len(brain.store.list_runs(org)),
        "agent_runs": len(
            brain.store.list_harness_records("agent_runs", org)
        ),
        "harness_jobs": brain.store.harness_job_counts(org),
    }


# ---- managed company agent harness ----


@router.post("/v1/agent/invoke")
def invoke_agent(body: AgentInvokeRequest, request: Request) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=body.organization_id,
        acting_principal_id=body.principal_id,
        destination=body.destination,
        purpose=body.purpose,
    )
    envelope = InvocationEnvelope(
        organization_id=body.organization_id,
        principal_id=body.principal_id,
        application_id=body.application_id,
        conversation_id=body.conversation_id,
        destination=body.destination,
        purpose=body.purpose,
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    run = brain.company_agent.create_invocation(
        envelope=envelope,
        goal=body.goal,
        trigger_kind=body.trigger_kind,
        initial_message=body.goal,
        metadata=body.metadata,
    )
    job = brain.harness_jobs.enqueue(
        organization_id=body.organization_id,
        kind="agent.run",
        payload={"run_id": run["id"]},
        idempotency_key=f"agent.run:{run['id']}",
    )
    response: dict[str, Any] = {"run": run, "job": job}
    if body.run_now:
        response["worker"] = brain.harness_worker.drain_until_idle()
        response["run"] = brain.agent_runs.get(run["id"])
    return response


@router.get("/v1/agent/runs")
def list_agent_runs(
    request: Request,
    organization_id: str,
    state: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    records = _app(request).store.list_harness_records(
        "agent_runs",
        organization_id,
        where={"state": state} if state else None,
        descending=True,
        limit=min(limit, 500),
    )
    return {"runs": records, "count": len(records)}


@router.get("/v1/agent/runs/{run_id}")
def get_agent_run(run_id: str, request: Request) -> dict[str, Any]:
    brain = _app(request)
    run = brain.agent_runs.get(run_id)
    if not run:
        raise HTTPException(404, "agent run not found")
    return {
        "run": run,
        "messages": brain.agent_runs.messages(run["session_id"]),
        "turns": brain.store.list_harness_records(
            "agent_turns", run["organization_id"], where={"run_id": run_id}
        ),
        "tool_calls": brain.store.list_harness_records(
            "harness_tool_calls",
            run["organization_id"],
            where={"run_id": run_id},
        ),
        "approvals": brain.store.list_harness_records(
            "approvals", run["organization_id"], where={"run_id": run_id}
        ),
        "checkpoints": brain.store.list_harness_records(
            "checkpoints", run["organization_id"], where={"run_id": run_id}
        ),
    }


@router.post("/v1/agent/runs/{run_id}:cancel")
def cancel_agent_run(
    run_id: str, request: Request, cancelled_by: str
) -> dict[str, Any]:
    try:
        return _app(request).company_agent.cancel(
            run_id, cancelled_by=cancelled_by
        )
    except KeyError as exc:
        raise HTTPException(404, "agent run not found") from exc


@router.post("/v1/approvals/{approval_id}:decide")
def decide_approval(
    approval_id: str, body: ApprovalDecisionRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    approval = brain.store.get_harness_record("approvals", approval_id)
    if not approval:
        raise HTTPException(404, "approval not found")
    if body.decided_by not in set(
        approval.get("allowed_approver_ids")
        or [approval.get("requested_by")]
    ):
        raise HTTPException(403, "principal cannot decide this approval")
    job = brain.harness_jobs.enqueue(
        organization_id=approval["organization_id"],
        kind="approval.resolve",
        payload={
            "approval_id": approval_id,
            "approved": body.approved,
            "decided_by": body.decided_by,
            "edited_arguments": body.edited_arguments,
        },
        idempotency_key=f"approval.resolve:{approval_id}:{body.approved}",
    )
    response: dict[str, Any] = {"approval": approval, "job": job}
    if body.run_now:
        response["worker"] = brain.harness_worker.drain_until_idle()
        response["approval"] = brain.store.get_harness_record(
            "approvals", approval_id
        )
    return response


@router.get("/v1/approvals")
def list_approvals(
    request: Request,
    organization_id: str,
    status: str | None = None,
) -> dict[str, Any]:
    records = _app(request).store.list_harness_records(
        "approvals",
        organization_id,
        where={"status": status} if status else None,
        descending=True,
    )
    return {"approvals": records, "count": len(records)}


@router.post("/v1/harness/worker:drain")
def drain_harness_worker(
    request: Request, max_cycles: int = 100
) -> dict[str, Any]:
    return _app(request).harness_worker.drain_until_idle(
        max_cycles=min(max_cycles, 1000)
    )


@router.get("/v1/harness/jobs")
def harness_jobs(
    request: Request, organization_id: str, status: str | None = None
) -> dict[str, Any]:
    with _app(request).store.cursor() as cur:
        if status:
            cur.execute(
                """SELECT payload FROM harness_jobs
                WHERE organization_id = ? AND status = ? ORDER BY created_at DESC""",
                (organization_id, status),
            )
        else:
            cur.execute(
                """SELECT payload FROM harness_jobs
                WHERE organization_id = ? ORDER BY created_at DESC""",
                (organization_id,),
            )
        jobs = [json.loads(row["payload"]) for row in cur.fetchall()]
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/v1/harness/feedback")
def record_harness_feedback(
    body: HarnessFeedbackRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).harness_evaluation.record_feedback(
            **body.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/harness/evaluation")
def harness_evaluation(
    request: Request, organization_id: str
) -> dict[str, Any]:
    return _app(request).harness_evaluation.metrics(organization_id)


@router.get("/v1/harness/dashboard")
def harness_dashboard(
    request: Request, organization_id: str
) -> dict[str, Any]:
    brain = _app(request)
    return {
        "organization_id": organization_id,
        "evaluation": brain.harness_evaluation.metrics(organization_id),
        "runs": brain.store.list_harness_records(
            "agent_runs",
            organization_id,
            descending=True,
            limit=50,
        ),
        "approvals": brain.store.list_harness_records(
            "approvals",
            organization_id,
            descending=True,
            limit=50,
        ),
        "attention": brain.store.list_harness_records(
            "attention_evaluations",
            organization_id,
            descending=True,
            limit=50,
        ),
        "interventions": brain.store.list_harness_records(
            "agent_interventions",
            organization_id,
            descending=True,
            limit=50,
        ),
        "jobs": {
            "counts": brain.store.harness_job_counts(organization_id),
            "dead_letters": harness_jobs(
                request, organization_id, status="dead_letter"
            )["jobs"][:50],
        },
    }


@router.get("/v1/harness/status")
def harness_status(request: Request) -> dict[str, Any]:
    brain = _app(request)
    config = Settings()
    return {
        "database": {
            "backend": "sqlite",
            "wal": True,
            "path_configured": bool(brain.store.db_path),
        },
        "models": {
            "provider": config.model_provider,
            "main": getattr(brain.main_model, "model", ""),
            "attention": getattr(brain.attention_model, "model", ""),
            "extraction": getattr(brain.extraction_model, "model", ""),
            "openrouter_key_configured": bool(config.openrouter_api_key),
        },
        "embeddings": {
            "provider": type(brain.embeddings).__name__,
            "model": brain.embeddings.model,
        },
        "slack": {
            "events_ready": bool(
                config.slack_signing_secret and config.slack_bot_token
            ),
            "socket_mode_ready": bool(
                config.slack_app_token and config.slack_bot_token
            ),
            "bot_user_id_configured": bool(config.slack_bot_user_id),
        },
        "composio": {
            "api_ready": brain.composio.configured,
            "connected_account_configured": bool(
                config.composio_connected_account_id
            ),
        },
        "worker": {
            "enabled_in_api": config.worker_enabled,
            "concurrency": config.worker_concurrency,
            "batch_size": config.worker_batch_size,
        },
    }


@router.post("/v1/admin/retention")
def retention_control(
    body: RetentionRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    try:
        if body.apply:
            if not body.confirmed_by:
                raise HTTPException(
                    400, "confirmed_by is required when apply=true"
                )
            return brain.retention.apply(
                body.organization_id,
                older_than_days=body.older_than_days,
                confirmed_by=body.confirmed_by,
            )
        return brain.retention.preview(
            body.organization_id,
            older_than_days=body.older_than_days,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/v1/admin/organizations:delete")
def delete_organization_data(
    body: OrganizationDeletionRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).retention.delete_organization(
            body.organization_id, confirmation=body.confirmation
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/v1/admin/reindex")
def enqueue_reindex(
    body: DeferredMaintenanceRequest, request: Request
) -> dict[str, Any]:
    return _app(request).harness_jobs.enqueue(
        organization_id=body.organization_id,
        kind="index.backfill",
        payload={"organization_id": body.organization_id},
        idempotency_key=(
            f"index.backfill:{body.organization_id}:"
            f"{datetime.now(timezone.utc).isoformat()}"
        ),
    )


@router.post("/v1/admin/profiles:refresh")
def enqueue_profile_refresh(
    body: DeferredMaintenanceRequest, request: Request
) -> dict[str, Any]:
    if not body.entity_id:
        raise HTTPException(400, "entity_id is required")
    return _app(request).harness_jobs.enqueue(
        organization_id=body.organization_id,
        kind="profile.refresh",
        payload={
            "organization_id": body.organization_id,
            "entity_id": body.entity_id,
        },
        idempotency_key=(
            f"profile.refresh:{body.organization_id}:{body.entity_id}:"
            f"{datetime.now(timezone.utc).isoformat()}"
        ),
    )


@router.post("/v1/channel-policies")
def upsert_channel_policy(
    body: ChannelPolicyRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    data["id"] = (
        f"channel_policy:{body.organization_id}:{body.source}:{body.channel_id}"
    )
    _app(request).store.upsert_simple_harness_record("channel_policies", data)
    return data


@router.post("/v1/slack/installations")
def upsert_slack_installation(
    body: SlackInstallationRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = data["created_at"]
    _app(request).store.upsert_slack_installation(data)
    return data


@router.post("/v1/identity-mappings")
def upsert_identity_mapping(
    body: IdentityMappingRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    data["id"] = (
        f"identity:{body.organization_id}:{body.source}:{body.source_user_id}"
    )
    _app(request).store.upsert_simple_harness_record("identity_mappings", data)
    return data


@router.post("/v1/tool-policies")
def upsert_tool_policy(
    body: ToolPolicyRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    data["id"] = f"tool_policy:{body.organization_id}:{body.tool_name}"
    _app(request).store.upsert_simple_harness_record("tool_policies", data)
    return data


@router.get("/v1/tools")
def list_agent_tools(
    request: Request, organization_id: str
) -> dict[str, Any]:
    brain = _app(request)
    definitions = []
    for definition in brain.tool_registry.definitions():
        policies = brain.store.list_harness_records(
            "tool_policies",
            organization_id,
            where={"tool_name": definition.name},
            order_by="updated_at",
            limit=1,
        )
        definitions.append(
            {
                **definition.model_dump(mode="json"),
                "policy": policies[0] if policies else {"enabled": True},
            }
        )
    return {
        "tools": definitions,
        "count": len(definitions),
        "composio_configured": brain.composio.configured,
    }


@router.post("/v1/tools:refresh")
def refresh_agent_tools(request: Request) -> dict[str, Any]:
    brain = _app(request)
    if not brain.composio.configured:
        raise HTTPException(503, "Composio is not configured")
    return refresh_composio_tool_schemas(
        brain.tool_registry, brain.composio
    )


@router.post("/v1/connector-accounts")
def upsert_connector_account(
    body: ConnectorAccountRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    now = datetime.now(timezone.utc).isoformat()
    data.update({"created_at": now, "updated_at": now})
    with _app(request).store.cursor() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO connector_accounts
            (id, organization_id, toolkit, status, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data["id"],
                data["organization_id"],
                data["toolkit"],
                data["status"],
                json.dumps(data),
                now,
                now,
            ),
        )
    return data


@router.post("/v1/automations")
def create_automation(
    body: AutomationRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).automations.create(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/automations")
def list_automations(
    request: Request, organization_id: str
) -> dict[str, Any]:
    records = _app(request).automations.list(organization_id)
    return {"automations": records, "count": len(records)}


@router.post("/v1/automations:tick")
def tick_automations(
    request: Request, organization_id: str | None = None
) -> dict[str, Any]:
    return _app(request).automations.tick(organization_id)


@router.post("/v1/automations/{automation_id}:enable")
def enable_automation(
    automation_id: str, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).automations.set_enabled(automation_id, True)
    except KeyError as exc:
        raise HTTPException(404, "automation not found") from exc


@router.post("/v1/automations/{automation_id}:disable")
def disable_automation(
    automation_id: str, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).automations.set_enabled(automation_id, False)
    except KeyError as exc:
        raise HTTPException(404, "automation not found") from exc


@router.post("/v1/slack/events")
async def slack_events(request: Request) -> dict[str, Any]:
    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        return _app(request).slack_gateway.accept(body=body, headers=headers)
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid Slack payload: {exc}") from exc


@router.post("/v1/slack/interactions")
async def slack_interactions(request: Request) -> dict[str, Any]:
    body = await request.body()
    brain = _app(request)
    config = brain.slack_gateway.config
    if not config.slack_signing_secret:
        raise HTTPException(503, "Slack signing secret is not configured")
    if not verify_slack_signature(
        signing_secret=config.slack_signing_secret,
        timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
        signature=request.headers.get("X-Slack-Signature", ""),
        body=body,
        tolerance_seconds=config.slack_signature_tolerance_seconds,
    ):
        raise HTTPException(401, "Invalid Slack request signature")
    parsed = parse_qs(body.decode("utf-8"))
    try:
        payload = json.loads((parsed.get("payload") or ["{}"])[0])
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid interaction payload") from exc
    actions = payload.get("actions") or []
    if not actions:
        raise HTTPException(400, "Interaction has no action")
    action = actions[0]
    if action.get("action_id") not in {
        "company_brain_approve",
        "company_brain_deny",
    }:
        raise HTTPException(400, "Unsupported interaction")
    approval_id = action.get("value")
    approval = brain.store.get_harness_record("approvals", approval_id)
    if not approval:
        raise HTTPException(404, "approval not found")
    user_id = str((payload.get("user") or {}).get("id") or "unknown")
    mapping = brain.store.identity_mapping(
        approval["organization_id"], "slack", user_id
    )
    decided_by = mapping["principal_id"] if mapping else f"slack:{user_id}"
    if decided_by not in set(
        approval.get("allowed_approver_ids")
        or [approval.get("requested_by")]
    ):
        raise HTTPException(403, "Slack user cannot decide this approval")
    job = brain.harness_jobs.enqueue(
        organization_id=approval["organization_id"],
        kind="approval.resolve",
        payload={
            "approval_id": approval_id,
            "approved": action["action_id"] == "company_brain_approve",
            "decided_by": decided_by,
        },
        idempotency_key=f"approval.slack:{approval_id}:{action['action_id']}",
    )
    return {"accepted": True, "job_id": job["id"]}


# ---- ingestion ----


@router.post("/v1/ingest")
def ingest(body: IngestRequest, request: Request) -> dict[str, Any]:
    return _app(request).ingestion.ingest(**body.model_dump())


@router.post("/v1/documents")
def create_document(
    body: DocumentRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return _app(request).events.ingest_document(body.model_dump(), idempotency_key)


@router.put("/v1/documents/{external_id}")
def update_document(
    external_id: str,
    body: DocumentRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if body.external_id != external_id:
        raise HTTPException(400, "external_id must match path")
    return _app(request).events.ingest_document(body.model_dump(), idempotency_key)


@router.delete("/v1/documents/{source_id}")
def delete_document(source_id: str, request: Request) -> dict[str, Any]:
    source = _app(request).ingestion.revoke_source(source_id)
    if not source:
        raise HTTPException(404, "source not found")
    return {"source": source, "status": "revoked"}


@router.get("/v1/documents/{source_id}/status")
def document_status(source_id: str, request: Request) -> dict[str, Any]:
    status = _app(request).events.document_status(source_id)
    if not status:
        raise HTTPException(404, "source not found")
    return status


@router.post("/v1/events")
def receive_event(body: CloudEventRequest, request: Request) -> dict[str, Any]:
    return _app(request).events.receive_event(_cloud_event_payload(body))


@router.post("/v1/events:batch")
def receive_events_batch(body: list[CloudEventRequest], request: Request) -> dict[str, Any]:
    results = [_app(request).events.receive_event(_cloud_event_payload(event)) for event in body]
    return {"results": results, "count": len(results)}


def _cloud_event_payload(body: CloudEventRequest) -> dict[str, Any]:
    event = body.model_dump()
    event["organization_id"] = event.pop("organizationid")
    event["principal_id"] = event.pop("principalid")
    event["application_id"] = event.pop("applicationid")
    event["trace_id"] = event.pop("traceid")
    event["scope_id"] = event.pop("scopeid")
    return event


@router.post("/v1/sources/{source_id}:revoke")
def revoke_source(source_id: str, request: Request) -> dict[str, Any]:
    src = _app(request).ingestion.revoke_source(source_id)
    if not src:
        raise HTTPException(404, "source not found")
    return {"source": src}


@router.get("/v1/sources/{source_id}")
def get_source(
    source_id: str,
    request: Request,
    acting_principal_id: str,
    organization_id: str | None = None,
    destination_type: str = "internal_agent_session",
    destination_id: str = "default",
    destination_is_private: bool = True,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Fail-closed: requires principal + destination; never leak private sources."""
    brain = _app(request)
    src = brain.ingestion.get_source(source_id)
    if not src:
        raise HTTPException(404, "source not found")
    org = organization_id or src.get("organization_id")
    decision = brain.authz.resolve(
        organization_id=org,
        acting_principal_id=acting_principal_id,
        destination={
            "type": destination_type,
            "id": destination_id,
            "is_private": destination_is_private,
        },
        purpose=purpose or "source_read",
    )
    if not brain.authz.can_read_source(decision, src):
        # Do not reveal that a restricted source exists
        raise HTTPException(404, "source not found")
    out = dict(src)
    out["permission_decision_id"] = decision.id
    return out


# ---- graph admin ----


@router.post("/v1/entities")
def upsert_entity(body: EntityRequest, request: Request) -> dict[str, Any]:
    return _app(request).graph.upsert_entity(body.model_dump())


@router.get("/v1/entities/{entity_id}")
def get_entity(entity_id: str, request: Request) -> dict[str, Any]:
    ent = _app(request).graph.get_entity(entity_id)
    if not ent:
        raise HTTPException(404, "entity not found")
    return ent


@router.post("/v1/edges")
def add_edge(body: EdgeRequest, request: Request) -> dict[str, Any]:
    return _app(request).graph.add_edge(
        organization_id=body.organization_id,
        edge_type=body.edge_type,
        from_id=body.from_id,
        to_id=body.to_id,
        properties=body.properties,
        source_ids=body.source_ids,
    )


@router.get("/v1/graph/explorer")
def graph_explorer_data(
    request: Request, organization_id: str, acting_principal_id: str,
    destination_type: str = "internal_agent_session", destination_id: str = "default",
    destination_is_private: bool = True, purpose: str | None = None,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id, acting_principal_id=acting_principal_id,
        destination={"type": destination_type, "id": destination_id, "is_private": destination_is_private},
        purpose=purpose or "graph_explorer",
    )
    allowed_sources = brain.authz.filter_sources(decision, brain.store.list_sources(organization_id))
    view = brain.graph.explorer_view(organization_id, {source["id"] for source in allowed_sources})
    view["permission_decision_id"] = decision.id
    return view


@router.get("/explorer", include_in_schema=False)
def graph_explorer_page() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parents[1] / "static" / "explorer.html")


@router.get("/admin", include_in_schema=False)
def harness_admin_page() -> FileResponse:
    return FileResponse(
        Path(__file__).resolve().parents[1] / "static" / "admin.html"
    )


@router.post("/v1/scopes")
def upsert_scope(body: ScopeRequest, request: Request) -> dict[str, Any]:
    data = body.model_dump()
    _app(request).store.upsert_scope(data)
    for pid in body.principal_ids:
        _app(request).store.add_membership(body.organization_id, pid, body.id)
    return data


@router.post("/v1/memberships")
def add_membership(body: MembershipRequest, request: Request) -> dict[str, str]:
    _app(request).store.add_membership(body.organization_id, body.principal_id, body.scope_id)
    return {"status": "ok"}


# ---- reactive ops ----


@router.post("/v1/search")
def search(body: SearchRequest, request: Request) -> dict[str, Any]:
    return _app(request).retrieval.search(
        organization_id=body.organization_id,
        query=body.query,
        acting_principal_id=body.acting_principal_id,
        destination=body.destination,
        purpose=body.purpose,
        entity_ids=body.entity_ids or None,
        limit=body.limit,
        include_superseded=body.include_superseded,
        as_of=body.as_of,
    )


@router.post("/v1/context:prepare")
def prepare_context(body: ContextRequest, request: Request) -> dict[str, Any]:
    pkg = _app(request).context.prepare_context(
        organization_id=body.organization_id,
        task=body.task,
        acting_principal_id=body.acting_principal_id,
        destination=body.destination,
        purpose=body.purpose,
        entity_ids=body.entity_ids or None,
        token_budget=body.token_budget,
        agent_id=body.agent_id,
        conversation=body.conversation,
    )
    return pkg.model_dump(mode="json")


@router.post("/v1/relationships:query")
def relationship_query(body: RelationshipRequest, request: Request) -> dict[str, Any]:
    return _app(request).context.relationship_context(
        organization_id=body.organization_id,
        from_entity_id=body.from_entity_id,
        to_entity_id=body.to_entity_id,
        acting_principal_id=body.acting_principal_id,
        destination=body.destination,
        purpose=body.purpose,
    )


@router.post("/v1/owners:resolve")
def resolve_owner(body: OwnerRequest, request: Request) -> dict[str, Any]:
    return _app(request).context.resolve_owner(
        organization_id=body.organization_id,
        issue_text=body.issue_text,
        acting_principal_id=body.acting_principal_id,
        entity_ids=body.entity_ids or None,
        include_on_call=body.include_on_call,
        destination=body.destination,
        purpose=body.purpose,
    )


@router.post("/v1/projects:context")
def project_context(body: ProjectContextRequest, request: Request) -> dict[str, Any]:
    return _app(request).context.project_context(
        organization_id=body.organization_id,
        project_id=body.project_id,
        acting_principal_id=body.acting_principal_id,
        destination=body.destination,
        purpose=body.purpose,
    )


# ---- profiles ----


def _profile_response(
    entity_id: str, request: Request, organization_id: str, acting_principal_id: str,
    destination_type: str, destination_id: str, destination_is_private: bool, purpose: str | None,
) -> dict[str, Any]:
    profile = _app(request).profiles.get(
        organization_id=organization_id,
        entity_id=entity_id,
        acting_principal_id=acting_principal_id,
        destination={"type": destination_type, "id": destination_id, "is_private": destination_is_private},
        purpose=purpose,
    )
    if not profile:
        raise HTTPException(404, "profile entity not found")
    return profile


@router.get("/v1/profiles/people/{entity_id}")
def person_profile(entity_id: str, request: Request, organization_id: str, acting_principal_id: str,
                   destination_type: str = "internal_agent_session", destination_id: str = "default",
                   destination_is_private: bool = True, purpose: str | None = None) -> dict[str, Any]:
    return _profile_response(entity_id, request, organization_id, acting_principal_id, destination_type, destination_id, destination_is_private, purpose)


@router.get("/v1/profiles/projects/{entity_id}")
def project_profile(entity_id: str, request: Request, organization_id: str, acting_principal_id: str,
                    destination_type: str = "internal_agent_session", destination_id: str = "default",
                    destination_is_private: bool = True, purpose: str | None = None) -> dict[str, Any]:
    return _profile_response(entity_id, request, organization_id, acting_principal_id, destination_type, destination_id, destination_is_private, purpose)


@router.get("/v1/profiles/services/{entity_id}")
def service_profile(entity_id: str, request: Request, organization_id: str, acting_principal_id: str,
                    destination_type: str = "internal_agent_session", destination_id: str = "default",
                    destination_is_private: bool = True, purpose: str | None = None) -> dict[str, Any]:
    return _profile_response(entity_id, request, organization_id, acting_principal_id, destination_type, destination_id, destination_is_private, purpose)


@router.get("/v1/profiles/customers/{entity_id}")
def customer_profile(entity_id: str, request: Request, organization_id: str, acting_principal_id: str,
                     destination_type: str = "internal_agent_session", destination_id: str = "default",
                     destination_is_private: bool = True, purpose: str | None = None) -> dict[str, Any]:
    return _profile_response(entity_id, request, organization_id, acting_principal_id, destination_type, destination_id, destination_is_private, purpose)


@router.post("/v1/profiles/{entity_id}:refresh")
def refresh_profile(entity_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    organization_id = body.get("organization_id")
    if not organization_id:
        raise HTTPException(400, "organization_id required")
    profile = _app(request).profiles.refresh(organization_id, entity_id)
    if not profile:
        raise HTTPException(404, "profile entity not found")
    return profile


# ---- memories ----


@router.get("/v1/memories/{memory_id}")
def get_memory(
    memory_id: str,
    request: Request,
    acting_principal_id: str,
    organization_id: str | None = None,
    destination_type: str = "internal_agent_session",
    destination_id: str = "default",
    destination_is_private: bool = True,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Fail-closed: requires principal + destination before returning memory content."""
    brain = _app(request)
    mem = brain.memory.get_memory(memory_id)
    if not mem:
        raise HTTPException(404, "memory not found")
    org = organization_id or mem.get("organization_id")
    decision = brain.authz.resolve(
        organization_id=org,
        acting_principal_id=acting_principal_id,
        destination={
            "type": destination_type,
            "id": destination_id,
            "is_private": destination_is_private,
        },
        purpose=purpose or "memory_read",
    )
    if not brain.authz.can_read_memory(decision, mem):
        raise HTTPException(404, "memory not found")
    out = dict(mem)
    out["permission_decision_id"] = decision.id
    return out


@router.get("/v1/memories/{memory_id}/lineage")
def memory_lineage(
    memory_id: str,
    request: Request,
    acting_principal_id: str,
    organization_id: str | None = None,
    destination_type: str = "internal_agent_session",
    destination_id: str = "default",
    destination_is_private: bool = True,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Fail-closed lineage: only authorized memories appear in the chain."""
    brain = _app(request)
    mem = brain.memory.get_memory(memory_id)
    if not mem:
        raise HTTPException(404, "memory not found")
    org = organization_id or mem.get("organization_id")
    decision = brain.authz.resolve(
        organization_id=org,
        acting_principal_id=acting_principal_id,
        destination={
            "type": destination_type,
            "id": destination_id,
            "is_private": destination_is_private,
        },
        purpose=purpose or "memory_lineage",
    )
    if not brain.authz.can_read_memory(decision, mem):
        raise HTTPException(404, "memory not found")
    chain = brain.graph.memory_history(mem["organization_id"], memory_id)
    allowed_chain = [m for m in chain if brain.authz.can_read_memory(decision, m)]
    return {
        "memory_id": memory_id,
        "lineage": allowed_chain,
        "permission_decision_id": decision.id,
    }


@router.post("/v1/memories/{memory_id}:confirm")
def confirm_memory(memory_id: str, request: Request) -> dict[str, Any]:
    memory = _app(request).memory.confirm(memory_id)
    if not memory:
        raise HTTPException(404, "memory not found")
    return memory


@router.post("/v1/memories/{memory_id}:retract")
def retract_memory(memory_id: str, request: Request) -> dict[str, Any]:
    memory = _app(request).memory.retract(memory_id)
    if not memory:
        raise HTTPException(404, "memory not found")
    return memory


@router.post("/v1/memories/{memory_id}:forget")
def forget_memory(memory_id: str, request: Request) -> dict[str, Any]:
    memory = _app(request).memory.forget(memory_id)
    if not memory:
        raise HTTPException(404, "memory not found")
    return memory


@router.post("/v1/memories/{memory_id}:correct")
def correct_memory(memory_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    content = body.get("content")
    source_id = body.get("source_id")
    if not content or not source_id:
        raise HTTPException(400, "content and source_id required")
    memory = _app(request).memory.correct(memory_id, content=content, source_id=source_id)
    if not memory:
        raise HTTPException(404, "memory or source not found")
    return memory


# ---- skills ----


@router.post("/v1/skills")
def create_skill(body: SkillCreateRequest, request: Request) -> dict[str, Any]:
    return _app(request).skills.create(**body.model_dump())


@router.get("/v1/skills/{skill_id}")
def get_skill(skill_id: str, request: Request) -> dict[str, Any]:
    skill = _app(request).skills.get(skill_id)
    if not skill:
        raise HTTPException(404, "skill not found")
    return skill


@router.post("/v1/skills:search")
def search_skills(body: SkillSearchRequest, request: Request) -> dict[str, Any]:
    return {
        "results": _app(request).skills.search(body.organization_id, body.query, body.limit)
    }


@router.post("/v1/skills:prepare")
def prepare_skill(body: dict[str, Any], request: Request) -> dict[str, Any]:
    skill_id = body.get("skill_id")
    if not skill_id:
        raise HTTPException(400, "skill_id required")
    result = _app(request).skills.prepare(skill_id, body.get("context_warnings"))
    if not result:
        raise HTTPException(404, "skill not found")
    return result


@router.post("/v1/skills/{skill_id}:propose-update")
def propose_skill_update(skill_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    proposal = _app(request).skills.propose_update(skill_id, body.get("changes") or {})
    if not proposal:
        raise HTTPException(404, "skill not found")
    return proposal


@router.get("/v1/skills/{skill_id}/versions")
def skill_versions(skill_id: str, request: Request) -> dict[str, Any]:
    versions = _app(request).skills.versions(skill_id)
    if not versions:
        raise HTTPException(404, "skill not found")
    return {"versions": versions}


@router.post("/v1/skills/{skill_id}/versions/{version}:approve")
def approve_skill_version(skill_id: str, version: int, request: Request) -> dict[str, Any]:
    skill = _app(request).skills.approve(skill_id, version)
    if not skill:
        raise HTTPException(404, "skill version not found")
    return skill


@router.post("/v1/skills/{skill_id}:deprecate")
def deprecate_skill(skill_id: str, request: Request) -> dict[str, Any]:
    skill = _app(request).skills.deprecate(skill_id)
    if not skill:
        raise HTTPException(404, "skill not found")
    return skill


# ---- runs ----


@router.post("/v1/runs")
def start_run(body: RunStartRequest, request: Request) -> dict[str, Any]:
    return _app(request).runs.start(**body.model_dump())


@router.post("/v1/runs/{run_id}/events")
def run_event(run_id: str, body: RunEventRequest, request: Request) -> dict[str, Any]:
    run = _app(request).runs.add_event(run_id, body.model_dump())
    if not run:
        raise HTTPException(404, "run not found")
    return run


@router.post("/v1/runs/{run_id}:complete")
def complete_run(run_id: str, body: RunCompleteRequest, request: Request) -> dict[str, Any]:
    run = _app(request).runs.complete(
        run_id,
        outcome=body.outcome,
        summary=body.summary,
        evidence_source_ids=body.evidence_source_ids,
    )
    if not run:
        raise HTTPException(404, "run not found")
    return run


@router.get("/v1/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    run = _app(request).runs.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@router.get("/v1/runs/{run_id}/graph")
def run_graph(run_id: str, request: Request) -> dict[str, Any]:
    view = _app(request).runs.execution_graph_view(run_id)
    if not view:
        raise HTTPException(404, "run not found")
    return view


@router.post("/v1/runs/{run_id}:evaluate")
def evaluate_run(run_id: str, request: Request) -> dict[str, Any]:
    result = _app(request).runs.evaluate(run_id)
    if not result:
        raise HTTPException(404, "run not found")
    return result


# ---- proactive ----


@router.post("/v1/proactive:evaluate")
def proactive_evaluate(body: ProactiveRequest, request: Request) -> dict[str, Any]:
    return _app(request).proactive.evaluate(
        organization_id=body.organization_id,
        trigger=body.trigger,
        acting_principal_id=body.acting_principal_id,
        destination=body.destination,
        mode=body.mode,
        candidate=body.candidate,
    )


# ---- signals ----


@router.get("/v1/signals")
def list_signals(request: Request, organization_id: str, state: str | None = None) -> dict[str, Any]:
    signals = _app(request).signals.list(organization_id, state)
    return {"signals": signals, "count": len(signals)}


@router.post("/v1/signals/{signal_id}:acknowledge")
def acknowledge_signal(signal_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    signal = _app(request).signals.acknowledge(signal_id, body.get("acting_principal_id"))
    if not signal:
        raise HTTPException(404, "signal not found")
    return signal


@router.post("/v1/signals/{signal_id}:dismiss")
def dismiss_signal(signal_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    signal = _app(request).signals.dismiss(signal_id, body.get("acting_principal_id"))
    if not signal:
        raise HTTPException(404, "signal not found")
    return signal


@router.post("/v1/signals/{signal_id}:route")
def route_signal(signal_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    signal = _app(request).signals.route(signal_id, body.get("recipient_id"))
    if not signal:
        raise HTTPException(404, "signal not found or no valid recipient")
    return signal


# ---- audit ----


@router.get("/v1/audit/events")
def audit_events(
    request: Request,
    organization_id: str,
    event_type: str | None = None,
) -> dict[str, Any]:
    events = _app(request).audit.list_events(organization_id, event_type=event_type)
    return {"events": events, "count": len(events)}


@router.get("/v1/audit/authorization-decisions/{decision_id}")
def audit_authz(decision_id: str, request: Request) -> dict[str, Any]:
    d = _app(request).audit.get_authorization_decision(decision_id)
    if not d:
        raise HTTPException(404, "decision not found")
    return d
