"""HTTP API surface for Toda V1."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import hashlib
import hmac
import ipaddress
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from toda.adapters import (
    composio_connected_account_id,
    normalize_composio_harness_event,
)
from toda.services.artifacts import artifact_payload
from toda.services.container import TodaApp
from toda.domain.models import new_id, utcnow
from toda.harness.slack import (
    UnauthorizedApprover,
    _slack_identity_key,
)
from toda.services.dashboard_auth import DashboardAuthService

router = APIRouter()


def _app(request: Request) -> TodaApp:
    return request.app.state.brain


# ---- tenant enforcement ----

# Endpoints that legitimately run without a console login. Each one either
# authenticates by another means or exists precisely to establish a login,
# so requiring a session cookie here would break it:
#
#   * Slack/Composio ingress verifies a request signature inside the
#     handler (SlackGateway.accept, ComposioClient webhook verification).
#   * OAuth callbacks are redirect targets the provider hits directly.
#   * The session endpoints are how a login is read and cleared.
#   * Setup status/manifest are read by the first-run wizard before any
#     workspace exists to sign in to.
#   * /v1/events{,:batch} is the programmatic ingest path used by the SDK.
#     Non-local callers must present a configured bearer token; localhost
#     remains keyless for the deterministic developer experience.
_TENANT_EXEMPT_PATHS = frozenset(
    {
        "/v1/session",
        "/v1/session/logout",
        "/v1/auth/status",
        "/v1/auth/login",
        "/v1/slack/events",
        "/v1/slack/interactions",
        "/v1/mcp/oauth/callback",
        "/v1/composio/webhooks",
        "/v1/events",
        "/v1/events:batch",
    }
)

_ADMIN_WRITE_PREFIXES = (
    "/v1/organizations",
    "/v1/policies",
    "/v1/scopes",
    "/v1/memberships",
    "/v1/tools",
    "/v1/skills",
    "/v1/mcp",
    "/v1/channel-policies",
    "/v1/connectors",
)


async def enforce_tenant(request: Request) -> None:
    """Reject any request that names an organization the caller is not
    signed into.

    Every `/v1` data route takes `organization_id` as a client-supplied
    parameter. Without this, that parameter is self-asserted: a signed-in
    user of one workspace could read another workspace's tasks, sessions,
    memories and metrics simply by changing it, and an unauthenticated
    caller could do the same. The console already resolves the real
    organization server-side from the login cookie (`GET /v1/session`), so
    binding the parameter to that value costs the frontend nothing.
    """
    path = request.url.path
    if path in {"/v1/events", "/v1/events:batch"}:
        _enforce_ingest_auth(request)
    # Only the /v1 data API is tenant-scoped. Everything else is either
    # infrastructure (/health), an OAuth entry point (/slack/install,
    # /slack/login), or the console SPA itself -- and the SPA must stay
    # reachable while signed out or there is no way to reach the login
    # page at all.
    if not path.startswith("/v1/") or path in _TENANT_EXEMPT_PATHS:
        return
    from toda.config import settings as _settings

    if _settings.dashboard.auth_disabled:
        return

    session_id = request.session.get("dashboard_session_id")
    resolved = DashboardAuthService(_app(request).store).resolve_session(session_id or "")
    if not resolved:
        raise HTTPException(401, "Not signed in")
    user, member = resolved
    request.state.dashboard_user = user
    request.state.dashboard_member = member
    session_org = member["organization_id"]
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and path.startswith(_ADMIN_WRITE_PREFIXES)
        and member["role"] not in {"owner", "admin"}
    ):
        raise HTTPException(403, "Organization administrator access is required")

    requested = request.query_params.get("organization_id")
    if requested is None and request.method in {"POST", "PUT", "PATCH"}:
        requested = await _body_organization_id(request)
    if requested is not None and requested != session_org:
        raise HTTPException(403, "organization_id does not match the signed-in workspace")


def _is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _enforce_ingest_auth(request: Request) -> None:
    """Authenticate direct event ingestion without weakening webhook auth.

    Local SDK/demo calls remain zero-setup. Anything arriving over a network
    must use ``Authorization: Bearer <token>`` where the token is one of the
    comma-separated ``TODA_API_KEYS`` values. Comparison is constant-time.
    """
    host = request.client.host if request.client else ""
    if _is_loopback_host(host):
        return
    from toda.config import settings as _settings

    configured = _settings.server.api_keys
    authorization = request.headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    accepted = scheme.lower() == "bearer" and any(
        hmac.compare_digest(credential, candidate) for candidate in configured
    )
    if not accepted:
        raise HTTPException(
            401,
            "A valid Toda ingestion API key is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _body_organization_id(request: Request) -> str | None:
    """Read organization_id out of a JSON body without consuming it.

    Starlette caches the raw body on the Request after the first read, and
    the route handler parses that same cached copy, so reading here is
    safe. A body that is absent, malformed, or not an object simply yields
    no organization to check -- validation of it belongs to the route.
    """
    if "application/json" not in request.headers.get("content-type", ""):
        return None
    try:
        payload = await request.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        value = payload.get("organization_id")
        return value if isinstance(value, str) else None
    return None


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
    occurred_at: datetime | None = None


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
    replaces: str | None = None


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
    activation_mode: str = "optional"
    activation: dict[str, Any] = Field(default_factory=dict)
    response_validation: dict[str, Any] = Field(default_factory=dict)
    scope_id: str = "scope_org_public"
    status: str = "active"
    instructions: str = ""
    kind: str = "company"
    immutable: bool = False
    required_capabilities: dict[str, Any] = Field(default_factory=dict)


class SkillSearchRequest(BaseModel):
    organization_id: str
    query: str
    limit: int = 20


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


class OrganizationRequest(BaseModel):
    id: str
    name: str
    region: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    memory_md: str = Field(default="", max_length=4000)


class MembershipRequest(BaseModel):
    organization_id: str
    principal_id: str
    scope_id: str


class TaskInvokeRequest(BaseModel):
    organization_id: str
    principal_id: str
    conversation_id: str
    destination: dict[str, Any]
    prompt: str
    purpose: str = "reactive_agent"
    application_id: str = "toda_api"
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
    proactive: bool = True
    free_response: bool = False
    is_private: bool = False
    scope_id: str | None = None


class IdentityMappingRequest(BaseModel):
    organization_id: str
    source: str = "slack"
    source_user_id: str
    principal_id: str
    # Slack user IDs are unique per workspace, not per organization. Supplying
    # the workspace qualifies the mapping so a second workspace's identical
    # user id resolves to its own person; omitting it writes the legacy
    # unqualified row, which still resolves as a fallback.
    workspace_id: str | None = None


class RoleRequest(BaseModel):
    organization_id: str
    name: str
    trigger_description: str
    goal: str | None = None
    skill_ids: list[str] = Field(default_factory=list)
    scope_id: str | None = None
    enabled: bool = True


class AutomationRequest(BaseModel):
    organization_id: str
    name: str
    goal: str
    principal_id: str
    conversation_id: str
    destination: dict[str, Any]
    interval_seconds: int | None = 3600
    cron_expression: str | None = None
    timezone: str = "UTC"
    quiet_hours: dict[str, str] | None = None
    misfire_policy: str = "fire_once"
    overlap_policy: str = "skip_if_running"
    enabled: bool = True


class AutomationUpdateRequest(BaseModel):
    organization_id: str
    name: str | None = None
    goal: str | None = None
    principal_id: str | None = None
    conversation_id: str | None = None
    destination: dict[str, Any] | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    quiet_hours: dict[str, str] | None = None
    misfire_policy: str | None = None
    overlap_policy: str | None = None
    enabled: bool | None = None


class MCPServerRequest(BaseModel):
    organization_id: str
    name: str
    transport: Literal["streamable_http", "sse", "stdio"] = "streamable_http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    auth_type: Literal["none", "api_key", "oauth"] = "none"
    api_key_header: str = "Authorization"
    api_key: str | None = None
    oauth_scope: str | None = None
    enabled: bool = True


class OrganizationScopeRequest(BaseModel):
    organization_id: str


class ToolPolicyRequest(BaseModel):
    organization_id: str
    tool_name: str
    execution: Literal["inherit", "ai_review", "ask", "deny"] = "inherit"


class ToolPolicyModeRequest(BaseModel):
    organization_id: str
    mode: Literal["supervised", "autonomous", "full_access", "restricted"]


class ConnectorAccountRequest(BaseModel):
    id: str
    organization_id: str
    toolkit: str
    connected_account_id: str
    status: str = "active"
    composio_user_id: str | None = None
    principal_id: str | None = None
    scope_id: str = "scope_org_public"
    visibility: str = "organization_public"
    observe_enabled: bool = True
    proactivity_enabled: bool = True
    learn: bool = True
    destination_id: str | None = None
    destination: dict[str, Any] | None = None


class ComposioSyncRequest(BaseModel):
    organization_id: str
    composio_user_id: str | None = None
    principal_id: str | None = None
    scope_id: str = "scope_org_public"
    visibility: str = "organization_public"
    proactivity_enabled: bool = True


class ComposioConnectRequest(BaseModel):
    organization_id: str
    auth_config_id: str
    callback_url: str | None = None
    alias: str | None = None
    composio_user_id: str | None = None


class ComposioConnectStartRequest(BaseModel):
    organization_id: str
    toolkit: str


class ComposioCredentialConnectRequest(BaseModel):
    organization_id: str
    toolkit: str
    auth_scheme: str
    credentials: dict[str, str]
    composio_user_id: str | None = None
    principal_id: str | None = None
    scope_id: str = "scope_org_public"
    visibility: str = "organization_public"
    proactivity_enabled: bool = True


class ComposioTriggerRequest(BaseModel):
    organization_id: str
    connected_account_id: str
    trigger_slug: str
    trigger_config: dict[str, Any] = Field(default_factory=dict)


class ComposioTriggerStateRequest(BaseModel):
    organization_id: str
    enabled: bool


class HarnessFeedbackRequest(BaseModel):
    organization_id: str
    feedback_type: str
    principal_id: str
    task_id: str | None = None
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


class ConnectorBackfillRequest(BaseModel):
    organization_id: str
    connector_account_id: str
    provider: str
    object_type: str
    records: list[dict[str, Any]] = Field(default_factory=list)
    action_slug: str | None = None
    action_arguments: dict[str, Any] = Field(default_factory=dict)
    items_path: str = "data.items"
    next_cursor_path: str = "data.next_cursor"
    cursor_argument: str = "cursor"
    max_pages: int = Field(default=100, ge=1, le=1000)
    next_cursor: str | None = None
    mode: str = "incremental"
    run_now: bool = False


class ExternalIdentifierRequest(BaseModel):
    organization_id: str
    entity_id: str
    namespace: str
    value: str
    confidence: float = Field(default=1.0, ge=0, le=1)
    verified: bool = True
    source_id: str | None = None


class ResolutionDecisionRequest(BaseModel):
    approved: bool
    decided_by: str


class FactAssertionRequest(BaseModel):
    organization_id: str
    subject_id: str
    predicate: str
    object_value: str
    scope_id: str = "scope_org_public"
    valid_from: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    valid_to: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0, le=1)
    authority: float = Field(default=0.8, ge=0, le=1)


class ConflictResolutionRequest(BaseModel):
    winner_fact_id: str
    decided_by: str


class JudgmentRequest(BaseModel):
    organization_id: str
    judgment_type: str
    principal_id: str | None = None
    subject: dict[str, Any]
    decision: str
    reason: str | None = None


class CaseUpdateRequest(BaseModel):
    status: str
    updated_by: str
    summary: str | None = None
    title: str | None = None


class ConditionalPolicyRequest(BaseModel):
    organization_id: str
    policy_key: str
    effect: str
    actions: list[str]
    conditions: dict[str, Any] = Field(default_factory=dict)
    approval_required: bool = False
    precondition: dict[str, Any] | None = None
    created_by: str


class PolicyActivationRequest(BaseModel):
    approved_by: str


class ArtifactRequest(BaseModel):
    organization_id: str
    artifact_type: str
    title: str
    content: Any
    scope_id: str = "scope_org_public"
    task_id: str | None = None
    created_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayProviderSetupRequest(BaseModel):
    connection_id: str
    provider: str
    api_key: str
    base_url: str | None = None
    assignments: dict[str, str] = Field(default_factory=dict)
    supports_vision: bool | None = None


class GatewayModelCatalogRequest(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None
    capability: Literal["chat", "embedding", "image", "transcription", "tts"] = "chat"


class GatewaySlackSetupRequest(BaseModel):
    organization_id: str = "org_acme"
    organization_name: str = "Acme Corp"
    bot_token: str
    app_token: str
    signing_secret: str
    # Resolved to a Slack member ID server-side via users.lookupByEmail, so
    # nobody has to go hunting for their own ID in Slack's UI.
    admin_email: str | None = None
    admin_slack_user_id: str | None = None
    admin_name: str | None = None
    channel_id: str | None = None
    proactive: bool = True


class GatewayComposioSetupRequest(BaseModel):
    api_key: str
    webhook_secret: str | None = None


class GatewayResetRequest(BaseModel):
    confirm: str


class GatewayProbeModelRequest(BaseModel):
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    capability: Literal["chat", "embedding"] = "chat"


class GatewayProbeSearchRequest(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None


class GatewayFetchSetupRequest(BaseModel):
    use_firecrawl: bool = False
    api_key: str | None = None


class GatewayToolsSetupRequest(BaseModel):
    connection_id: str
    provider: str
    api_key: str | None = None
    base_url: str | None = None
    use_for_search: bool = False
    use_for_extraction: bool = False
    web_fetch_enabled: bool = True


class OwnerBootstrapRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)


class DashboardLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class AccessRequestBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=1024)


class InvitationCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["admin", "member"] = "member"


class AccessDecisionRequest(BaseModel):
    decision: Literal["approved", "denied"]


class MemberUpdateRequest(BaseModel):
    role: Literal["owner", "admin", "member"]
    status: Literal["active", "suspended"]


class SetupBootstrapRequest(BaseModel):
    token: str


# ---- health ----


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "toda"}


@router.post("/v1/organizations")
def create_organization(
    body: OrganizationRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    # One gateway serves exactly one tenant: `toda init` creates it, and the
    # authorization, retention and scope paths all assume a single
    # organization. Reject a second one outright rather than only rejecting
    # a duplicate id.
    if brain.store.list_organizations():
        raise HTTPException(409, "This Toda installation already has an organization")
    organization = body.model_dump(exclude_none=True)
    brain.store.put_organization(organization)
    brain.audit.record(
        body.id,
        "organization.created",
        {"organization_id": body.id, "name": body.name},
    )
    return organization


@router.get("/v1/organizations")
def list_organizations(request: Request) -> dict[str, Any]:
    organizations = _app(request).store.list_organizations()
    return {"organizations": organizations, "count": len(organizations)}


class OrganizationUpdateRequest(BaseModel):
    name: str | None = None
    about: str | None = None
    memory_md: str | None = Field(default=None, max_length=4000)
    proactive_heartbeat: dict[str, Any] | None = None


@router.patch("/v1/organizations/{organization_id}")
def update_organization(
    organization_id: str, body: OrganizationUpdateRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    organization = brain.store.get_organization(organization_id)
    if not organization:
        raise HTTPException(404, "organization not found")
    updates = body.model_dump(exclude_none=True)
    organization.update(updates)
    brain.store.put_organization(organization)
    brain.audit.record(
        organization_id,
        "organization.memory_updated" if "memory_md" in updates else "organization.updated",
        {
            "organization_id": organization_id,
            **({"memory_length": len(updates["memory_md"])} if "memory_md" in updates else updates),
        },
    )
    return organization


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
        "sessions": len(brain.store.list_sessions(org, limit=100_000)),
        "tasks": len(brain.store.list_tasks(org, limit=100_000)),
        "harness_jobs": brain.store.harness_job_counts(org),
        "harness_job_queue_age_seconds": brain.store.harness_job_queue_age_seconds(org),
        "episodes": brain.store.episode_size_stats(org),
        "connector_objects": len(
            brain.reality.list_connector_objects(org, latest_only=True)
        ),
        "temporal_facts": len(brain.reality.current_facts(org)),
        "open_cases": len(
            brain.reality.list_cases(
                org, statuses={"open", "investigating"}
            )
        ),
    }


# ---- managed company agent harness ----


@router.post("/v1/tasks/invoke")
def invoke_task(body: TaskInvokeRequest, request: Request) -> dict[str, Any]:
    """Wake the one Session Manager responsible for this conversation."""
    brain = _app(request)
    wake = brain.sessions.wake(
        organization_id=body.organization_id,
        conversation_key=body.conversation_id,
        destination=body.destination,
        role="user",
        content=body.prompt,
        principal_id=body.principal_id,
        metadata={**body.metadata, "force_task": True},
        message_metadata={"application_id": body.application_id},
        trigger_kind=body.trigger_kind,
        purpose=body.purpose,
    )
    job = brain.harness_jobs.enqueue(
        organization_id=body.organization_id,
        kind="session.dispatch",
        payload={"session_id": wake["session_id"]},
        idempotency_key=f"session.dispatch:{wake['session_id']}:{wake['revision']}",
    )
    response: dict[str, Any] = {
        "session": brain.sessions.get(wake["session_id"]),
        "wake": wake,
        "job": job,
    }
    if body.run_now:
        response["worker"] = brain.harness_worker.drain_until_idle()
        response["tasks"] = brain.store.list_tasks(
            body.organization_id, session_id=wake["session_id"]
        )
        response["session"] = brain.sessions.get(wake["session_id"])
    return response


def _task_detail(brain: TodaApp, task: dict[str, Any]) -> dict[str, Any]:
    task_id = task["id"]
    organization_id = task["organization_id"]

    def task_records(table: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Return Task-scoped auxiliary rows in their native schema."""
        rows = brain.store.list_harness_records(
            table, organization_id, where={"task_id": task_id}, limit=limit
        )
        return rows

    artifacts = [
        {key: value.get(key) for key in (
            "id", "task_id", "artifact_type", "title", "status", "scope_id",
            "metadata", "version", "created_at", "updated_at", "download_url",
        ) if value.get(key) is not None}
        for value in brain.artifacts.list(organization_id, task_id=task_id)
    ]
    return {
        "task": task,
        "session": brain.sessions.get(task["session_id"]),
        "messages": brain.store.list_session_messages(task["session_id"], limit=1000),
        "children": brain.store.list_tasks(
            organization_id, parent_task_id=task_id, limit=500
        ),
        "attempts": brain.store.list_task_attempts(task_id),
        "turns": task_records("task_turns"),
        "tool_calls": task_records("harness_tool_calls"),
        "approvals": task_records("approvals"),
        "checkpoints": task_records("checkpoints"),
        "skill_refs": task_records("task_skill_refs"),
        "capability_refs": task_records("task_capability_refs"),
        "plan": brain.store.get_harness_record(
            "plans", f"plan:{task.get('root_task_id') or task_id}"
        ),
        "artifacts": artifacts,
        "workspace": brain.workspaces.get_for_task(organization_id, task),
    }


@router.get("/v1/tasks")
def list_tasks(
    request: Request,
    organization_id: str,
    session_id: str | None = None,
    parent_task_id: str | None = None,
    root_task_id: str | None = None,
    state: str | None = None,
    worker_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    records = _app(request).store.list_tasks(
        organization_id,
        session_id=session_id,
        parent_task_id=parent_task_id,
        root_task_id=root_task_id,
        state=state,
        worker_type=worker_type,
        limit=min(limit, 500),
    )
    return {"tasks": records, "count": len(records)}


@router.get("/v1/tasks/{task_id}")
def get_task(task_id: str, request: Request, organization_id: str) -> dict[str, Any]:
    brain = _app(request)
    task = brain.store.get_task(task_id)
    if not task or task["organization_id"] != organization_id:
        raise HTTPException(404, "task not found")
    return _task_detail(brain, task)


@router.get("/v1/tasks/{task_id}/attempts")
def list_task_attempts(
    task_id: str, request: Request, organization_id: str
) -> dict[str, Any]:
    brain = _app(request)
    task = brain.store.get_task(task_id)
    if not task or task["organization_id"] != organization_id:
        raise HTTPException(404, "task not found")
    attempts = brain.store.list_task_attempts(task_id)
    return {"attempts": attempts, "count": len(attempts)}


@router.get("/v1/tasks/{task_id}/children")
def list_task_children(
    task_id: str, request: Request, organization_id: str
) -> dict[str, Any]:
    brain = _app(request)
    task = brain.store.get_task(task_id)
    if not task or task["organization_id"] != organization_id:
        raise HTTPException(404, "task not found")
    children = brain.store.list_tasks(
        organization_id, parent_task_id=task_id, limit=500
    )
    return {"children": children, "count": len(children)}


@router.get("/v1/tasks/{task_id}/artifacts")
def list_task_artifacts(
    task_id: str, request: Request, organization_id: str
) -> dict[str, Any]:
    brain = _app(request)
    task = brain.store.get_task(task_id)
    if not task or task["organization_id"] != organization_id:
        raise HTTPException(404, "task not found")
    artifacts = brain.artifacts.list(organization_id, task_id=task_id)
    return {"artifacts": artifacts, "count": len(artifacts)}


@router.post("/v1/tasks/{task_id}:cancel")
def cancel_task(task_id: str, request: Request, organization_id: str) -> dict[str, Any]:
    brain = _app(request)
    task = brain.store.get_task(task_id)
    if not task or task["organization_id"] != organization_id:
        raise HTTPException(404, "task not found")
    if task.get("worker_type") == "pi" and brain.child_task_executor.pi_executor:
        brain.child_task_executor.pi_executor.cancel(task_id, reason="operator cancel")
    changed = brain.store.cancel_task(task_id)
    if task.get("parent_task_id") is None:
        brain.sessions.notify(task["session_id"], reason="task.cancelled")
    return {"task": brain.store.get_task(task_id), "cancelled": changed}


@router.post("/v1/tasks/{task_id}:retry")
def retry_task(task_id: str, request: Request, organization_id: str) -> dict[str, Any]:
    brain = _app(request)
    task = brain.store.get_task(task_id)
    if not task or task["organization_id"] != organization_id:
        raise HTTPException(404, "task not found")
    if not brain.store.retry_task(task_id):
        raise HTTPException(409, "only failed or cancelled tasks can be retried")
    if task.get("parent_task_id") is None:
        wake = brain.sessions.notify(task["session_id"], reason="task.retried")
        job = brain.harness_jobs.enqueue(
            organization_id=organization_id,
            kind="session.dispatch",
            payload={"session_id": task["session_id"]},
            idempotency_key=f"task.retry:{task_id}:{wake}",
        )
    else:
        job = brain.harness_jobs.enqueue(
            organization_id=organization_id,
            kind="task.run_child",
            payload={"task_id": task_id},
            idempotency_key=f"task.retry:{task_id}:{utcnow().isoformat()}",
        )
    return {"task": brain.store.get_task(task_id), "job": job}


@router.get("/v1/sessions")
def list_sessions(
    request: Request,
    organization_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    """List the durable Session Manager conversations and their Task preview."""
    brain = _app(request)
    sessions = brain.store.list_sessions(organization_id, limit=min(limit, 500))
    results = []
    for session in sessions:
        tasks = brain.store.list_tasks(
            organization_id, session_id=session["id"], limit=500
        )
        latest_task = tasks[-1] if tasks else None
        messages = brain.store.session_messages(session["id"], limit=1)
        results.append(
            {
                "session": session,
                "task_count": len(tasks),
                "latest_task": latest_task,
                "message_count": len(
                    brain.store.session_messages(session["id"], limit=10_000)
                ),
                "last_message_preview": (messages[-1] if messages else None),
            }
        )
    return {"sessions": results, "count": len(results)}


@router.get("/v1/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    brain = _app(request)
    session = brain.store.get_session(session_id)
    if not session:
        raise HTTPException(404, "session not found")
    tasks = brain.store.list_tasks(
        session["organization_id"], session_id=session_id, limit=1000
    )
    return {
        "session": session,
        "tasks": tasks,
        "messages": brain.store.session_messages(session_id, limit=1000),
    }


@router.get("/v1/sessions/{session_id}/tasks")
def list_session_tasks(
    session_id: str,
    request: Request,
    organization_id: str,
    state: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    brain = _app(request)
    session = brain.store.get_session(session_id)
    if not session or session["organization_id"] != organization_id:
        raise HTTPException(404, "session not found")
    tasks = brain.store.list_tasks(
        organization_id, session_id=session_id, state=state, limit=min(limit, 1000)
    )
    return {"tasks": tasks, "count": len(tasks)}


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
    tasks = brain.store.list_tasks(organization_id, limit=50)
    approvals = brain.store.list_harness_records(
        "approvals",
        organization_id,
        descending=True,
        limit=50,
    )
    return {
        "organization_id": organization_id,
        "evaluation": brain.harness_evaluation.metrics(organization_id),
        "sessions": brain.store.list_sessions(organization_id, limit=50),
        "tasks": tasks,
        "approvals": approvals,
        "attention": brain.store.list_harness_records(
            "attention_evaluations",
            organization_id,
            descending=True,
            limit=50,
        ),
        "interventions": brain.store.list_harness_records(
            "task_interventions",
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
    from toda.config import settings as config

    capability_ids = brain.tool_executor.available_capabilities_for_organization(
        config.organization.id
    )
    return {
        "database": {
            "backend": "sqlite",
            "wal": True,
            "path_configured": bool(brain.store.db_path),
        },
        "models": {
            "assignments": {
                role: assignment.model
                for role, assignment in config.models.assignments.items()
            },
            "main": getattr(brain.main_model, "model", ""),
            "attention": getattr(brain.attention_model, "model", ""),
            "extraction": getattr(brain.extraction_model, "model", ""),
            "connections_configured": len(config.models.providers),
        },
        "embeddings": {
            "provider": type(brain.embeddings).__name__,
            "model": brain.embeddings.model,
            "assigned": "embedding" in config.models.assignments,
        },
        "slack": {
            "events_ready": bool(
                config.slack.signing_secret and config.slack.bot_token
            ),
            "socket_mode_ready": bool(
                config.slack.app_token and config.slack.bot_token
            ),
            "bot_user_id_configured": bool(config.slack.bot_user_id),
        },
        "composio": {
            "api_ready": brain.composio.configured,
            "connected_account_configured": bool(
                config.composio.connected_account_id
            ),
        },
        "worker": {
            "enabled_in_api": config.worker.enabled,
            "concurrency": config.worker.concurrency,
            "batch_size": config.worker.batch_size,
        },
        "child_tasks": {
            "enabled": True,
            "max_parallel": config.agent.child_max_parallel,
            "max_depth": config.agent.child_max_depth,
            "write_tools_allowed": False,
        },
        "capabilities": {
            "available": sorted(capability_ids),
            "registered": sorted(brain.tool_registry.capabilities()),
            "count": len(capability_ids),
        },
        "reality_engine": {
            "canonical_connector_objects": True,
            "historical_backfill": True,
            "entity_resolution": True,
            "bitemporal_facts": True,
            "contradiction_resolution": True,
            "cross_source_cases": True,
            "judgment_memory": True,
            "conditional_policies": True,
            "execution_preconditions": True,
            "durable_artifacts": True,
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


@router.get("/v1/channel-policies")
def list_channel_policies(
    request: Request, organization_id: str
) -> dict[str, Any]:
    records = _app(request).store.list_harness_records(
        "channel_policies", organization_id, order_by="updated_at"
    )
    return {"channel_policies": records, "count": len(records)}


@router.post("/v1/roles")
def create_role(body: RoleRequest, request: Request) -> dict[str, Any]:
    data = body.model_dump()
    now = datetime.now(timezone.utc).isoformat()
    data["id"] = new_id("role")
    data["status"] = "active" if body.enabled else "disabled"
    data["created_at"] = now
    data["updated_at"] = now
    _app(request).store.upsert_simple_harness_record("roles", data)
    return data


@router.get("/v1/roles")
def list_roles(request: Request, organization_id: str) -> dict[str, Any]:
    records = _app(request).store.list_harness_records(
        "roles", organization_id, order_by="updated_at"
    )
    return {"roles": records, "count": len(records)}


@router.post("/v1/slack/installations")
def upsert_slack_installation(
    body: SlackInstallationRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = data["created_at"]
    _app(request).store.upsert_slack_installation(data)
    return data


@router.get("/v1/slack/installations")
def list_slack_installations(
    request: Request, organization_id: str
) -> dict[str, Any]:
    records = _app(request).store.list_harness_records(
        "slack_installations", organization_id
    )
    redacted = [{k: v for k, v in record.items() if k != "bot_token"} for record in records]
    return {"installations": redacted, "count": len(redacted)}


@router.post("/v1/identity-mappings")
def upsert_identity_mapping(
    body: IdentityMappingRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    if body.source == "slack" and body.workspace_id:
        data["source_user_id"] = _slack_identity_key(
            body.workspace_id, body.source_user_id
        )
    data["id"] = (
        f"identity:{body.organization_id}:{body.source}:{data['source_user_id']}"
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


@router.post("/v1/tool-policy-mode")
def upsert_tool_policy_mode(
    body: ToolPolicyModeRequest, request: Request
) -> dict[str, Any]:
    data = {
        "id": f"tool_policy:{body.organization_id}:*",
        "organization_id": body.organization_id,
        "tool_name": "*",
        "mode": body.mode,
    }
    _app(request).store.upsert_simple_harness_record("tool_policies", data)
    return data


@router.get("/v1/tools")
def list_agent_tools(
    request: Request, organization_id: str
) -> dict[str, Any]:
    brain = _app(request)
    definitions = []
    for definition in brain.tool_registry.definitions():
        if (
            definition.allowed_organization_ids
            and organization_id not in definition.allowed_organization_ids
        ):
            continue
        policy = brain.tool_executor.effective_policy(organization_id, definition)
        definitions.append(
            {
                **definition.model_dump(mode="json"),
                "policy": policy,
            }
        )
    ready = sum(1 for definition in definitions if definition["runtime_ready"])
    return {
        "tools": definitions,
        "count": len(definitions),
        "ready_count": ready,
        "setup_needed_count": len(definitions) - ready,
        "policy_mode": brain.tool_executor.organization_policy_mode(organization_id),
        "composio_configured": brain.composio.configured,
    }


def _supersede_other_active_connector_accounts(
    brain: TodaApp, organization_id: str, toolkit: str, keep_connected_account_id: str
) -> None:
    """Enforce at most one active connector_account per (organization, toolkit).

    Composio mints a brand-new connected_account_id on every fresh OAuth
    completion, and connector_accounts rows are keyed on that id — so
    without this, reconnecting (or a sync pass that sees several accounts
    for the same toolkit) just accumulates rows instead of replacing the
    old one. Called after any active row is saved; best-effort revokes the
    superseded connections on Composio's side too, since the point is one
    real connection, not just one row that looks canonical locally.
    """
    for other in brain.store.list_connector_accounts(organization_id, active_only=True):
        if other.get("toolkit") != toolkit:
            continue
        if other.get("connected_account_id") == keep_connected_account_id:
            continue
        try:
            brain.composio.delete_connected_account(other["connected_account_id"])
        except Exception:
            pass  # best-effort revoke; still supersede the local row regardless
        other["status"] = "disconnected"
        brain.store.upsert_connector_account(other)


@router.post("/v1/connector-accounts")
def upsert_connector_account(
    body: ConnectorAccountRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    now = datetime.now(timezone.utc).isoformat()
    brain = _app(request)
    existing = brain.store.get_harness_record("connector_accounts", data["id"])
    data["created_at"] = (existing or {}).get("created_at") or now
    data["updated_at"] = now
    data["composio_user_id"] = (
        data.get("composio_user_id") or f"toda:{data['organization_id']}"
    )
    data["principal_id"] = (
        data.get("principal_id")
        or f"connector:{data['connected_account_id']}"
    )
    scope = brain.store.get_scope(data["scope_id"])
    if not scope or scope["organization_id"] != data["organization_id"]:
        raise HTTPException(400, "Connector scope does not belong to organization")
    if not brain.store.get_entity(data["principal_id"]):
        brain.graph.upsert_entity(
            {
                "id": data["principal_id"],
                "organization_id": data["organization_id"],
                "entity_type": "service_account",
                "name": f"{data['toolkit']} connector",
                "aliases": [data["connected_account_id"]],
                "attributes": {
                    "toolkit": data["toolkit"],
                    "connected_account_id": data["connected_account_id"],
                },
            }
        )
    brain.store.add_membership(
        data["organization_id"], data["principal_id"], data["scope_id"]
    )
    brain.store.upsert_connector_account(data)
    if data.get("status") == "active":
        _supersede_other_active_connector_accounts(
            brain, data["organization_id"], data["toolkit"], data["connected_account_id"]
        )
    return data


@router.get("/v1/connector-accounts")
def list_connector_accounts(
    request: Request, organization_id: str
) -> dict[str, Any]:
    accounts = _app(request).store.list_connector_accounts(organization_id)
    return {"accounts": accounts, "count": len(accounts)}


@router.post("/v1/composio/connections:sync")
def sync_composio_connections(
    body: ComposioSyncRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    if not brain.composio.configured:
        raise HTTPException(503, "Composio API key is not configured")
    user_id = body.composio_user_id or f"toda:{body.organization_id}"
    scope = brain.store.get_scope(body.scope_id)
    if not scope or scope["organization_id"] != body.organization_id:
        raise HTTPException(400, "Connector scope does not belong to organization")
    try:
        response = brain.composio.list_connected_accounts(user_id=user_id)
    except Exception as exc:
        raise HTTPException(502, f"Composio connection sync failed: {exc}") from exc
    saved: list[dict[str, Any]] = []
    # Oldest-first: when Composio reports several accounts for the same
    # toolkit (e.g. repeated reconnects), each active upsert supersedes any
    # prior active row for that toolkit, so processing in creation order
    # leaves the genuinely newest connection as the sole survivor rather
    # than whichever happened to be listed last by Composio.
    remote_items = sorted(
        response.get("items") or [], key=lambda item: str(item.get("created_at") or "")
    )
    for remote in remote_items:
        toolkit_data = remote.get("toolkit") or {}
        toolkit = toolkit_data.get("slug") or remote.get("toolkit_slug")
        account_id = remote.get("id") or remote.get("nanoid")
        if not toolkit or not account_id:
            continue
        account = ConnectorAccountRequest(
            id=f"connector:{body.organization_id}:{account_id}",
            organization_id=body.organization_id,
            toolkit=str(toolkit).lower(),
            connected_account_id=str(account_id),
            status=(
                "active"
                if str(remote.get("status") or "ACTIVE").upper() == "ACTIVE"
                else str(remote.get("status") or "inactive").lower()
            ),
            composio_user_id=user_id,
            principal_id=body.principal_id,
            scope_id=body.scope_id,
            visibility=body.visibility,
            proactivity_enabled=body.proactivity_enabled,
        )
        saved.append(upsert_connector_account(account, request))
    return {
        "organization_id": body.organization_id,
        "composio_user_id": user_id,
        "accounts": saved,
        "count": len(saved),
    }


@router.get("/v1/composio/auth-configs")
def list_composio_auth_configs(
    request: Request, toolkit: str | None = None
) -> dict[str, Any]:
    try:
        return _app(request).composio.list_auth_configs(toolkit=toolkit)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio auth-config listing failed: {exc}") from exc


@router.get("/v1/composio/status")
def composio_status(request: Request) -> dict[str, Any]:
    """Whether a Composio credential is configured.

    Read-only: the key lives in config.json like every other credential, so
    the console reports readiness rather than offering to write it.
    """
    return {"api_key_configured": _app(request).composio.configured}


@router.get("/v1/composio/toolkits")
def list_composio_toolkits(
    request: Request,
    search: str | None = None,
    category: str | None = None,
    cursor: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Browse Composio's full toolkit catalog (1000+ entries), not just the
    console's curated shortlist — search/category-filtered and cursor-
    paginated so the page never has to fetch or render all of them."""
    brain = _app(request)
    if not brain.composio.configured:
        raise HTTPException(503, "Composio API key is not configured")
    try:
        response = brain.composio.list_toolkits(
            search=search, category=category, cursor=cursor, limit=limit
        )
    except Exception as exc:
        raise HTTPException(502, f"Composio toolkit listing failed: {exc}") from exc
    items = [
        {
            "slug": item.get("slug"),
            "name": item.get("name"),
            "description": (item.get("meta") or {}).get("description"),
            "logo": (item.get("meta") or {}).get("logo"),
            "auth_schemes": item.get("auth_schemes")
            or item.get("composio_managed_auth_schemes")
            or [],
            "no_auth": bool(item.get("no_auth")),
            "categories": [
                c.get("name") for c in (item.get("meta") or {}).get("categories") or []
            ],
        }
        for item in response.get("items") or []
    ]
    return {
        "items": items,
        "next_cursor": response.get("next_cursor"),
        "total_items": response.get("total_items"),
    }


@router.get("/v1/composio/toolkits/categories")
def list_composio_toolkit_categories(request: Request) -> dict[str, Any]:
    brain = _app(request)
    if not brain.composio.configured:
        raise HTTPException(503, "Composio API key is not configured")
    try:
        return brain.composio.list_toolkit_categories()
    except Exception as exc:
        raise HTTPException(502, f"Composio category listing failed: {exc}") from exc


@router.get("/v1/composio/toolkits/{slug}")
def describe_composio_toolkit(slug: str, request: Request) -> dict[str, Any]:
    """Toolkit detail for building a connect UI: every supported auth
    scheme and, for each, the exact fields needed to open a connection
    under it (name/displayName/description/is_secret) — this is what lets
    the console render a correct credentials form instead of a single
    generic "API key" box that only happens to fit some toolkits."""
    brain = _app(request)
    if not brain.composio.configured:
        raise HTTPException(503, "Composio API key is not configured")
    try:
        detail = brain.composio.describe_toolkit(slug)
    except Exception as exc:
        raise HTTPException(502, f"Composio toolkit lookup failed: {exc}") from exc
    schemes = []
    for config in detail.get("auth_config_details") or []:
        fields = (config.get("fields") or {}).get("connected_account_initiation") or {}
        schemes.append(
            {
                "mode": config.get("mode"),
                "name": config.get("name"),
                "required_fields": fields.get("required") or [],
                "optional_fields": fields.get("optional") or [],
            }
        )
    return {
        "slug": detail.get("slug"),
        "name": detail.get("name"),
        "logo": (detail.get("meta") or {}).get("logo"),
        "description": (detail.get("meta") or {}).get("description"),
        "no_auth": bool(detail.get("no_auth")),
        "auth_schemes": schemes,
    }


@router.post("/v1/composio/connect")
def start_composio_connect(
    body: ComposioConnectStartRequest, request: Request
) -> dict[str, Any]:
    """One-click connect: find-or-create an auth config, then a connect link.

    Lets the console offer a plain "Connect GitHub" button without the
    caller needing to know Composio auth-config IDs up front.
    """
    brain = _app(request)
    if not brain.composio.configured:
        raise HTTPException(503, "Composio API key is not configured")
    if not brain.store.get_organization(body.organization_id):
        raise HTTPException(404, "Organization not found")
    toolkit = body.toolkit.strip().lower()
    user_id = f"toda:{body.organization_id}"
    try:
        existing = brain.composio.list_auth_configs(toolkit=toolkit)
        items = existing.get("items") or []
        auth_config_id = items[0]["id"] if items else None
        if not auth_config_id:
            created = brain.composio.create_auth_config(toolkit=toolkit)
            auth_config_id = created.get("id") or created.get("auth_config_id")
        if not auth_config_id:
            raise RuntimeError("Composio did not return an auth config id")
        link = brain.composio.create_connect_link(
            user_id=user_id, auth_config_id=auth_config_id
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio connect failed: {exc}") from exc
    return {
        "organization_id": body.organization_id,
        "toolkit": toolkit,
        "auth_config_id": auth_config_id,
        "redirect_url": link.get("redirect_url"),
        "connected_account_id": link.get("connected_account_id"),
    }


@router.post("/v1/composio/connections:credentials")
def connect_composio_with_credentials(
    body: ComposioCredentialConnectRequest, request: Request
) -> dict[str, Any]:
    """Non-OAuth connect: API key, bearer token, basic auth, etc. Unlike
    the OAuth path there is no redirect to wait on — a correctly-formed
    request activates the connection synchronously, so the caller gets
    back an already-saved connector account instead of a link to poll.
    """
    brain = _app(request)
    if not brain.composio.configured:
        raise HTTPException(503, "Composio API key is not configured")
    if not brain.store.get_organization(body.organization_id):
        raise HTTPException(404, "Organization not found")
    toolkit = body.toolkit.strip().lower()
    auth_scheme = body.auth_scheme.strip().upper()
    user_id = body.composio_user_id or f"toda:{body.organization_id}"
    scope = brain.store.get_scope(body.scope_id)
    if not scope or scope["organization_id"] != body.organization_id:
        raise HTTPException(400, "Connector scope does not belong to organization")
    try:
        existing = brain.composio.list_auth_configs(toolkit=toolkit)
        auth_config_id = next(
            (
                item["id"]
                for item in existing.get("items") or []
                if str(item.get("auth_scheme") or "").upper() == auth_scheme
            ),
            None,
        )
        if not auth_config_id:
            created = brain.composio.create_auth_config(
                toolkit=toolkit, auth_scheme=auth_scheme
            )
            auth_config_id = created.get("id") or created.get("auth_config_id")
        if not auth_config_id:
            raise RuntimeError("Composio did not return an auth config id")
        remote = brain.composio.create_connected_account(
            auth_config_id=auth_config_id,
            user_id=user_id,
            auth_scheme=auth_scheme,
            val={"status": "ACTIVE", **body.credentials},
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio credential connect failed: {exc}") from exc
    account_id = remote.get("id")
    if not account_id:
        raise HTTPException(502, "Composio did not return a connected account id")
    account = ConnectorAccountRequest(
        id=f"connector:{body.organization_id}:{account_id}",
        organization_id=body.organization_id,
        toolkit=toolkit,
        connected_account_id=str(account_id),
        status=(
            "active"
            if str(remote.get("status") or "ACTIVE").upper() == "ACTIVE"
            else str(remote.get("status") or "inactive").lower()
        ),
        composio_user_id=user_id,
        principal_id=body.principal_id,
        scope_id=body.scope_id,
        visibility=body.visibility,
        proactivity_enabled=body.proactivity_enabled,
    )
    saved = upsert_connector_account(account, request)
    return {
        "organization_id": body.organization_id,
        "toolkit": toolkit,
        "auth_config_id": auth_config_id,
        "account": saved,
    }


@router.post("/v1/composio/connections:link")
def create_composio_connection_link(
    body: ComposioConnectRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    if not brain.store.get_organization(body.organization_id):
        raise HTTPException(404, "Organization not found")
    user_id = (
        body.composio_user_id or f"toda:{body.organization_id}"
    )
    try:
        result = brain.composio.create_connect_link(
            user_id=user_id,
            auth_config_id=body.auth_config_id,
            callback_url=body.callback_url,
            alias=body.alias,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio connect-link creation failed: {exc}") from exc
    return {
        "organization_id": body.organization_id,
        "composio_user_id": user_id,
        **result,
    }


@router.delete("/v1/composio/connections/{connected_account_id}")
def disconnect_composio_account(
    connected_account_id: str,
    request: Request,
    organization_id: str,
) -> dict[str, Any]:
    brain = _app(request)
    account = brain.store.connector_account_by_external_id(
        connected_account_id
    )
    if not account or account["organization_id"] != organization_id:
        raise HTTPException(404, "Active connector account not found")
    try:
        remote = brain.composio.delete_connected_account(
            connected_account_id
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio disconnect failed: {exc}") from exc
    account["status"] = "disconnected"
    brain.store.upsert_connector_account(account)
    return {"account": account, "composio": remote}


@router.get("/v1/composio/trigger-types")
def list_composio_trigger_types(
    request: Request, toolkit: str | None = None
) -> dict[str, Any]:
    try:
        return _app(request).composio.list_trigger_types(toolkit=toolkit)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio trigger discovery failed: {exc}") from exc


@router.post("/v1/composio/triggers")
def create_composio_trigger(
    body: ComposioTriggerRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    account = brain.store.connector_account_by_external_id(
        body.connected_account_id
    )
    if not account or account["organization_id"] != body.organization_id:
        raise HTTPException(404, "Active connector account not found")
    try:
        result = brain.composio.create_trigger(
            slug=body.trigger_slug,
            connected_account_id=body.connected_account_id,
            user_id=account.get("composio_user_id"),
            trigger_config=body.trigger_config,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio trigger creation failed: {exc}") from exc
    return {
        "organization_id": body.organization_id,
        "connected_account_id": body.connected_account_id,
        "trigger": result,
    }


@router.get("/v1/composio/triggers")
def list_composio_triggers(
    request: Request, organization_id: str
) -> dict[str, Any]:
    brain = _app(request)
    accounts = brain.store.list_connector_accounts(
        organization_id, active_only=True
    )
    account_ids = [account["connected_account_id"] for account in accounts]
    if not account_ids:
        return {"items": [], "count": 0}
    try:
        return brain.composio.list_active_triggers(
            connected_account_ids=account_ids
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Composio trigger listing failed: {exc}") from exc


@router.patch("/v1/composio/triggers/{trigger_id}")
def set_composio_trigger_state(
    trigger_id: str,
    body: ComposioTriggerStateRequest,
    request: Request,
) -> dict[str, Any]:
    brain = _app(request)
    accounts = brain.store.list_connector_accounts(
        body.organization_id, active_only=True
    )
    try:
        active = brain.composio.list_active_triggers(
            connected_account_ids=[
                account["connected_account_id"] for account in accounts
            ]
        )
    except Exception as exc:
        raise HTTPException(502, f"Composio trigger lookup failed: {exc}") from exc
    items = active.get("items") or active.get("triggers") or []
    if not any(
        str(item.get("id") or item.get("trigger_id")) == trigger_id
        for item in items
    ):
        raise HTTPException(404, "Trigger does not belong to organization")
    try:
        return brain.composio.set_trigger_enabled(
            trigger_id, enabled=body.enabled
        )
    except Exception as exc:
        raise HTTPException(502, f"Composio trigger update failed: {exc}") from exc


@router.post("/v1/composio/webhooks")
async def receive_composio_webhook(request: Request) -> dict[str, Any]:
    """Accept every signed event belonging to a registered connector account."""
    brain = _app(request)
    raw_body = await request.body()
    try:
        payload = brain.composio.verify_webhook(
            raw_body=raw_body,
            webhook_id=request.headers.get("webhook-id", ""),
            webhook_timestamp=request.headers.get("webhook-timestamp", ""),
            webhook_signature=request.headers.get("webhook-signature", ""),
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    connected_account_id = composio_connected_account_id(payload)
    if not connected_account_id:
        raise HTTPException(422, "Composio event has no connected account")
    account = brain.store.connector_account_by_external_id(
        connected_account_id
    )
    if not account:
        raise HTTPException(403, "Composio account is not registered or active")
    external_user_id = str(
        (payload.get("metadata") or {}).get("user_id")
        or (payload.get("data") or {}).get("user_id")
        or ""
    )
    identity = (
        brain.store.identity_mapping(
            account["organization_id"], "composio", external_user_id
        )
        if external_user_id
        else None
    )
    principal_id = (
        (identity or {}).get("principal_id")
        or account.get("principal_id")
        or f"connector:{connected_account_id}"
    )
    try:
        event = normalize_composio_harness_event(
            payload, account=account, principal_id=principal_id
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    received = {
        "id": event["id"],
        "organization_id": event["organization_id"],
        "type": event["type"],
        "source": event["source"],
        "received_at": utcnow().isoformat(),
        "data": event,
        "raw_payload": payload,
    }
    if not brain.store.upsert_received_event(received):
        return {
            "accepted": False,
            "duplicate": True,
            "event_id": event["id"],
        }
    job = brain.harness_jobs.enqueue(
        organization_id=event["organization_id"],
        kind="event.route",
        payload={"event": event},
        idempotency_key=f"event.route:{event['dedupe_key']}",
    )
    brain.audit.record(
        event["organization_id"],
        "composio.event.accepted",
        {
            "event_id": event["id"],
            "job_id": job["id"],
            "trigger_slug": event["payload"].get("trigger_slug"),
            "connected_account_id": connected_account_id,
        },
        principal_id=principal_id,
    )
    return {
        "accepted": True,
        "event_id": event["id"],
        "organization_id": event["organization_id"],
        "job_id": job["id"],
    }


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


@router.put("/v1/automations/{automation_id}")
def update_automation(
    automation_id: str, body: AutomationUpdateRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).automations.update(
            automation_id, **body.model_dump(exclude_none=True)
        )
    except KeyError as exc:
        raise HTTPException(404, "automation not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/v1/automations/{automation_id}:run")
def run_automation_now(
    automation_id: str, body: OrganizationScopeRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).automations.run_now(
            automation_id, body.organization_id
        )
    except KeyError as exc:
        raise HTTPException(404, "automation not found") from exc


@router.delete("/v1/automations/{automation_id}")
def delete_automation(
    automation_id: str, request: Request, organization_id: str
) -> dict[str, Any]:
    automation = _app(request).store.get_harness_record("automations", automation_id)
    if not automation or automation.get("organization_id") != organization_id:
        raise HTTPException(404, "automation not found")
    return _app(request).automations.delete(automation_id)


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


@router.post("/v1/mcp/servers")
def create_mcp_server(body: MCPServerRequest, request: Request) -> dict[str, Any]:
    try:
        return _app(request).mcp.create(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/mcp/servers")
def list_mcp_servers(request: Request, organization_id: str) -> dict[str, Any]:
    servers = _app(request).mcp.list(organization_id)
    return {"servers": servers, "count": len(servers)}


@router.post("/v1/mcp/servers/{server_id}:test")
def test_mcp_server(
    server_id: str, body: OrganizationScopeRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).mcp.test(server_id, body.organization_id)
    except KeyError as exc:
        raise HTTPException(404, "MCP server not found") from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"MCP connection failed: {exc}") from exc


@router.post("/v1/mcp/servers/{server_id}:enable")
def enable_mcp_server(
    server_id: str, body: OrganizationScopeRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).mcp.set_enabled(server_id, body.organization_id, True)
    except KeyError as exc:
        raise HTTPException(404, "MCP server not found") from exc


@router.post("/v1/mcp/servers/{server_id}:disable")
def disable_mcp_server(
    server_id: str, body: OrganizationScopeRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).mcp.set_enabled(server_id, body.organization_id, False)
    except KeyError as exc:
        raise HTTPException(404, "MCP server not found") from exc


@router.delete("/v1/mcp/servers/{server_id}")
def delete_mcp_server(
    server_id: str, request: Request, organization_id: str
) -> dict[str, Any]:
    try:
        return _app(request).mcp.delete(server_id, organization_id)
    except KeyError as exc:
        raise HTTPException(404, "MCP server not found") from exc


@router.post("/v1/mcp/servers/{server_id}:oauth-start")
async def start_mcp_oauth(
    server_id: str, body: OrganizationScopeRequest, request: Request
) -> dict[str, Any]:
    from toda.config import settings as _settings

    public_url = (_settings.server.public_url or str(request.base_url)).rstrip("/")
    redirect_uri = f"{public_url}/v1/mcp/oauth/callback"
    try:
        return await _app(request).mcp.start_oauth(
            server_id, body.organization_id, redirect_uri
        )
    except KeyError as exc:
        raise HTTPException(404, "MCP server not found") from exc
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"OAuth setup failed: {exc}") from exc


@router.get("/v1/mcp/oauth/callback", response_class=HTMLResponse)
async def mcp_oauth_callback(
    request: Request,
    code: str,
    state: str,
    iss: str | None = None,
) -> HTMLResponse:
    try:
        await _app(request).mcp.complete_oauth(state=state, code=code, iss=iss)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"OAuth callback failed: {exc}") from exc
    return HTMLResponse(
        """<!doctype html><meta charset=\"utf-8\"><title>MCP connected</title>
        <body style=\"font-family:system-ui;padding:3rem\">MCP server connected. You can close this window.
        <script>window.opener?.postMessage({type:'toda:mcp-oauth-complete'}, window.location.origin); window.close();</script></body>"""
    )


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
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        return _app(request).slack_gateway.accept_interaction(
            body=body, headers=headers
        )
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except UnauthorizedApprover as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---- self-hosted organization identity ----


def _auth(request: Request) -> DashboardAuthService:
    return DashboardAuthService(_app(request).store)


def _single_organization(request: Request) -> dict[str, Any]:
    organizations = _app(request).store.list_organizations()
    if len(organizations) != 1:
        raise HTTPException(409, "This installation must contain exactly one organization")
    return organizations[0]


def _dashboard_session_response(
    request: Request, user: dict[str, Any], member: dict[str, Any]
) -> dict[str, Any]:
    """The console's view of who is signed in.

    Mirrors the shape returned by the auth-disabled branch of GET
    /v1/session, so the frontend reads one contract either way.
    """
    organization = _app(request).store.get_organization(member["organization_id"]) or {}
    return {
        "organization_id": member["organization_id"],
        "principal_id": member["principal_id"],
        "workspace_name": organization.get("name") or member["organization_id"],
        "user_id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": member["role"],
        "auth_disabled": False,
    }


def _start_dashboard_session(
    request: Request, user: dict[str, Any], member: dict[str, Any]
) -> dict[str, Any]:
    """Issue a dashboard session and bind it to the signed cookie.

    The cookie carries the opaque token; only its SHA-256 digest is stored,
    so ``resolve_session`` hashes what it is given. Putting the already
    hashed id in the cookie would never resolve.
    """
    session = _auth(request).create_session(member["organization_id"], user["id"])
    request.session["dashboard_session_id"] = session["token"]
    return _dashboard_session_response(request, user, member)


def _require_admin(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    session_id = request.session.get("dashboard_session_id")
    resolved = _auth(request).resolve_session(session_id or "")
    if not resolved:
        raise HTTPException(401, "Not signed in")
    user, member = resolved
    if member["role"] not in {"owner", "admin"}:
        raise HTTPException(403, "Organization administrator access is required")
    return user, member


@router.get("/v1/auth/status")
def dashboard_auth_status(request: Request) -> dict[str, Any]:
    organizations = _app(request).store.list_organizations()
    return {
        "owner_required": _auth(request).needs_owner,
        "organization": (
            {"id": organizations[0]["id"], "name": organizations[0].get("name")}
            if len(organizations) == 1
            else None
        ),
    }


@router.post("/v1/auth/login")
def login_dashboard(body: DashboardLoginRequest, request: Request) -> dict[str, Any]:
    store = _app(request).store
    remote = request.client.host if request.client else "unknown"
    attempt_key = hashlib.sha256(
        f"{remote}:{body.email.strip().casefold()}".encode()
    ).hexdigest()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(minutes=15)).isoformat()
    if store.dashboard_login_failure_count(attempt_key, since) >= 5:
        raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes")
    try:
        user, member = _auth(request).authenticate(body.email, body.password)
    except ValueError as exc:
        store.record_dashboard_login_failure(new_id("login"), attempt_key, now.isoformat())
        raise HTTPException(401, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    store.clear_dashboard_login_failures(attempt_key)
    _app(request).audit.record(
        member["organization_id"], "dashboard.session.login", {"user_id": user["id"]}
    )
    return _start_dashboard_session(request, user, member)


@router.get("/v1/session")
def get_dashboard_session(request: Request) -> dict[str, Any]:
    from toda.config import settings as _settings

    if _settings.dashboard.auth_disabled:
        return {
            "organization_id": _settings.organization.id,
            "principal_id": "person_ajay",
            "workspace_name": "Local Dev",
            "user_id": "dev",
            "email": "dev@localhost",
            "name": "Local developer",
            "role": "owner",
            "auth_disabled": True,
        }
    resolved = _auth(request).resolve_session(
        request.session.get("dashboard_session_id") or ""
    )
    if not resolved:
        raise HTTPException(401, "Not signed in")
    return _dashboard_session_response(request, *resolved)


@router.post("/v1/session/logout")
def logout_dashboard_session(request: Request) -> dict[str, Any]:
    session_id = request.session.get("dashboard_session_id")
    if session_id:
        _auth(request).revoke_session(session_id)
    request.session.clear()
    return {"ok": True}


@router.post("/v1/connectors/backfill")
def enqueue_connector_backfill(
    body: ConnectorBackfillRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    account = brain.store.get_harness_record(
        "connector_accounts", body.connector_account_id
    )
    if not account or account["organization_id"] != body.organization_id:
        raise HTTPException(404, "connector account not found")
    if not body.records and not body.action_slug:
        raise HTTPException(400, "records or action_slug is required")
    run = brain.reality.begin_sync(
        organization_id=body.organization_id,
        connector_account_id=body.connector_account_id,
        object_type=body.object_type,
        mode=body.mode,
    )
    job = brain.harness_jobs.enqueue(
        organization_id=body.organization_id,
        kind="connector.backfill",
        payload={
            "sync_run_id": run["id"],
            "provider": body.provider,
            "records": body.records,
            "next_cursor": body.next_cursor,
            "action_slug": body.action_slug,
            "action_arguments": body.action_arguments,
            "items_path": body.items_path,
            "next_cursor_path": body.next_cursor_path,
            "cursor_argument": body.cursor_argument,
            "max_pages": body.max_pages,
        },
        idempotency_key=f"connector.backfill:{run['id']}",
    )
    response: dict[str, Any] = {"sync_run": run, "job": job}
    if body.run_now:
        response["worker"] = brain.harness_worker.drain_until_idle()
        response["sync_run"] = brain.reality.get_sync_run(run["id"])
    return response


@router.get("/v1/connectors/sync-runs")
def list_connector_sync_runs(
    request: Request, organization_id: str
) -> dict[str, Any]:
    runs = _app(request).reality.list_sync_runs(organization_id)
    return {"sync_runs": runs, "count": len(runs)}


@router.get("/v1/connectors/objects")
def list_connector_objects(
    request: Request,
    organization_id: str,
    acting_principal_id: str,
    provider: str | None = None,
    object_type: str | None = None,
    external_id: str | None = None,
    latest_only: bool = True,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id,
        acting_principal_id=acting_principal_id,
        purpose="connector_object_query",
    )
    values = brain.reality.list_connector_objects(
        organization_id,
        provider=provider,
        object_type=object_type,
        external_id=external_id,
        latest_only=latest_only,
        scope_ids=decision.allowed_scope_ids,
    )
    return {
        "objects": values,
        "count": len(values),
        "permission_decision_id": decision.id,
    }


@router.post("/v1/entities/identifiers")
def link_external_identifier(
    body: ExternalIdentifierRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    entity = brain.store.get_entity(body.entity_id)
    if not entity or entity["organization_id"] != body.organization_id:
        raise HTTPException(404, "entity not found")
    try:
        return brain.reality.link_identifier(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/v1/entities/identifiers")
def list_external_identifiers(
    request: Request, organization_id: str, entity_id: str | None = None
) -> dict[str, Any]:
    values = _app(request).reality.list_identifiers(
        organization_id, entity_id=entity_id
    )
    return {"identifiers": values, "count": len(values)}


@router.get("/v1/entity-resolution/candidates")
def list_entity_resolution_candidates(
    request: Request, organization_id: str, status: str | None = None
) -> dict[str, Any]:
    candidates = _app(request).reality.list_resolution_candidates(
        organization_id, status=status
    )
    return {"candidates": candidates, "count": len(candidates)}


@router.post("/v1/entity-resolution/candidates/{candidate_id}:decide")
def decide_entity_resolution_candidate(
    candidate_id: str,
    body: ResolutionDecisionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return _app(request).reality.decide_resolution_candidate(
            candidate_id,
            approved=body.approved,
            decided_by=body.decided_by,
        )
    except KeyError as exc:
        raise HTTPException(404, "resolution candidate not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/v1/facts")
def assert_reality_fact(
    body: FactAssertionRequest, request: Request
) -> dict[str, Any]:
    brain = _app(request)
    scope = brain.store.get_scope(body.scope_id)
    if not scope or scope["organization_id"] != body.organization_id:
        raise HTTPException(400, "scope does not belong to organization")
    return brain.reality.assert_fact(**body.model_dump())


@router.get("/v1/facts")
def list_reality_facts(
    request: Request,
    organization_id: str,
    acting_principal_id: str,
    subject_id: str | None = None,
    predicate: str | None = None,
    valid_at: str | None = None,
    known_at: str | None = None,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id,
        acting_principal_id=acting_principal_id,
        purpose="fact_query",
    )
    facts = brain.reality.current_facts(
        organization_id,
        subject_id=subject_id,
        predicate=predicate,
        valid_at=valid_at,
        known_at=known_at,
        scope_ids=decision.allowed_scope_ids,
    )
    return {
        "facts": facts,
        "count": len(facts),
        "permission_decision_id": decision.id,
    }


@router.get("/v1/fact-conflicts")
def list_fact_conflicts(
    request: Request,
    organization_id: str,
    acting_principal_id: str,
    status: str | None = None,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id,
        acting_principal_id=acting_principal_id,
        purpose="fact_conflict_query",
    )
    conflicts = brain.reality.list_conflicts(
        organization_id,
        status=status,
        scope_ids=decision.allowed_scope_ids,
    )
    return {
        "conflicts": conflicts,
        "count": len(conflicts),
        "permission_decision_id": decision.id,
    }


@router.post("/v1/fact-conflicts/{conflict_id}:resolve")
def resolve_fact_conflict(
    conflict_id: str,
    body: ConflictResolutionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return _app(request).reality.resolve_conflict(
            conflict_id,
            winner_fact_id=body.winner_fact_id,
            decided_by=body.decided_by,
        )
    except KeyError as exc:
        raise HTTPException(404, "fact conflict not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/cases")
def list_reality_cases(
    request: Request,
    organization_id: str,
    acting_principal_id: str,
    status: str | None = None,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id,
        acting_principal_id=acting_principal_id,
        purpose="case_query",
    )
    cases = brain.reality.list_cases(
        organization_id,
        statuses={status} if status else None,
        scope_ids=decision.allowed_scope_ids,
    )
    return {
        "cases": cases,
        "count": len(cases),
        "permission_decision_id": decision.id,
    }


@router.get("/v1/cases/{case_id}/timeline")
def get_case_timeline(
    case_id: str,
    request: Request,
    organization_id: str,
    acting_principal_id: str,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id,
        acting_principal_id=acting_principal_id,
        purpose="case_query",
    )
    visible = {
        case["id"]
        for case in brain.reality.list_cases(
            organization_id, scope_ids=decision.allowed_scope_ids
        )
    }
    if case_id not in visible:
        raise HTTPException(404, "case not found")
    events = brain.reality.case_timeline(case_id)
    return {"case_id": case_id, "events": events, "count": len(events)}


@router.post("/v1/cases/{case_id}:update")
def update_reality_case(
    case_id: str, body: CaseUpdateRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).reality.update_case(
            case_id, **body.model_dump()
        )
    except KeyError as exc:
        raise HTTPException(404, "case not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/v1/judgments")
def record_judgment(
    body: JudgmentRequest, request: Request
) -> dict[str, Any]:
    return _app(request).reality.record_judgment(**body.model_dump())


@router.get("/v1/judgments")
def list_judgments(
    request: Request,
    organization_id: str,
    acting_principal_id: str,
    principal_id: str | None = None,
    judgment_type: str | None = None,
) -> dict[str, Any]:
    values = _app(request).reality.list_judgments(
        organization_id,
        principal_id=principal_id,
        judgment_type=judgment_type,
    )
    values = [
        item
        for item in values
        if item.get("principal_id") in {None, acting_principal_id}
    ]
    return {"judgments": values, "count": len(values)}


@router.post("/v1/policies")
def create_conditional_policy(
    body: ConditionalPolicyRequest, request: Request
) -> dict[str, Any]:
    data = body.model_dump()
    data["status"] = "pending"
    return _app(request).policy.create_version(**data)


@router.post("/v1/policies/{policy_id}:activate")
def activate_conditional_policy(
    policy_id: str, body: PolicyActivationRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).policy.activate(
            policy_id, approved_by=body.approved_by
        )
    except KeyError as exc:
        raise HTTPException(404, "policy not found") from exc


@router.get("/v1/policies")
def list_conditional_policies(
    request: Request,
    organization_id: str,
    policy_key: str | None = None,
) -> dict[str, Any]:
    values = _app(request).policy.list_versions(
        organization_id, policy_key=policy_key
    )
    return {"policies": values, "count": len(values)}


@router.post("/v1/artifacts")
def create_artifact(
    body: ArtifactRequest, request: Request
) -> dict[str, Any]:
    try:
        return _app(request).artifacts.create(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/artifacts")
def list_artifacts(
    request: Request,
    organization_id: str,
    acting_principal_id: str,
    artifact_type: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id,
        acting_principal_id=acting_principal_id,
        purpose="artifact_query",
    )
    values = brain.artifacts.list(
        organization_id,
        scope_ids=decision.allowed_scope_ids,
        artifact_type=artifact_type,
        task_id=task_id,
    )
    return {
        "artifacts": values,
        "count": len(values),
        "permission_decision_id": decision.id,
    }


@router.get("/v1/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: str,
    request: Request,
    acting_principal_id: str,
) -> dict[str, Any]:
    brain = _app(request)
    artifact = brain.artifacts.get(artifact_id)
    if not artifact:
        raise HTTPException(404, "artifact not found")
    decision = brain.authz.resolve(
        organization_id=artifact["organization_id"],
        acting_principal_id=acting_principal_id,
        purpose="artifact_query",
    )
    if artifact["scope_id"] not in set(decision.allowed_scope_ids):
        raise HTTPException(404, "artifact not found")
    return {
        "artifact": artifact,
        "permission_decision_id": decision.id,
    }


_INLINE_MIME_TYPES = {
    "text/html",
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/svg+xml",
}

# An SVG served inline from the console's own origin is an active document:
# it may carry <script> and same-origin fetches. chart_create/diagram_render
# only ever emit renderer output, but `brain_create_artifact` accepts an
# arbitrary content dict from the model, so an "image/svg+xml" artifact is not
# necessarily one of theirs. The header makes the inline response inert rather
# than trusting where the bytes came from.
_SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


@router.get("/v1/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, request: Request) -> Response:
    brain = _app(request)
    artifact = brain.artifacts.get(artifact_id)
    if not artifact:
        raise HTTPException(404, "artifact not found")
    # One implementation, shared with the Slack upload tool -- two copies is
    # how they diverged, and the tool's half-version shipped a base64
    # wrapper into a channel as a .json file.
    body, mime_type, safe_name = artifact_payload(artifact)
    disposition = "inline" if mime_type in _INLINE_MIME_TYPES else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{safe_name}"'}
    if mime_type == "image/svg+xml":
        headers["Content-Security-Policy"] = _SVG_CSP
    return Response(
        content=body,
        media_type=mime_type,
        headers=headers,
    )


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
        replaces=body.replaces,
    )


@router.post("/v1/edges/{edge_id}:supersede")
def supersede_edge(edge_id: str, request: Request) -> dict[str, Any]:
    edge = _app(request).graph.supersede_edge(edge_id)
    if not edge:
        raise HTTPException(404, "edge not found")
    return edge


@router.get("/v1/graph/explorer")
def graph_explorer_data(
    request: Request, organization_id: str, acting_principal_id: str,
    destination_type: str = "internal_agent_session", destination_id: str = "default",
    destination_is_private: bool = True, purpose: str | None = None,
    valid_at: str | None = None,
) -> dict[str, Any]:
    brain = _app(request)
    decision = brain.authz.resolve(
        organization_id=organization_id, acting_principal_id=acting_principal_id,
        destination={"type": destination_type, "id": destination_id, "is_private": destination_is_private},
        purpose=purpose or "graph_explorer",
    )
    allowed_sources = brain.authz.filter_sources(decision, brain.store.list_sources(organization_id))
    view = brain.graph.explorer_view(
        organization_id,
        {source["id"] for source in allowed_sources},
        valid_at=valid_at,
    )
    view["permission_decision_id"] = decision.id
    return view


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


@router.get("/v1/profiles/{entity_id}")
def entity_profile(entity_id: str, request: Request, organization_id: str, acting_principal_id: str,
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
    try:
        return _app(request).skills.create(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/skills/catalog")
def skill_catalog(request: Request, organization_id: str = "org_acme") -> dict[str, Any]:
    brain = _app(request)
    capabilities = brain.tool_executor.available_capabilities_for_organization(organization_id)
    return {
        "packages": brain.skill_packages.metadata(),
        "readiness": brain.skill_packages.readiness(capabilities),
        "capabilities": sorted(capabilities),
    }


@router.get("/v1/skills/readiness")
def skill_readiness(request: Request, organization_id: str = "org_acme") -> dict[str, Any]:
    brain = _app(request)
    capabilities = brain.tool_executor.available_capabilities_for_organization(organization_id)
    return {
        "organization_id": organization_id,
        "capabilities": sorted(capabilities),
        "readiness": brain.skill_packages.readiness(capabilities),
    }


@router.get("/v1/capabilities")
def list_capabilities(request: Request, organization_id: str = "org_acme") -> dict[str, Any]:
    brain = _app(request)
    registered = sorted(brain.tool_registry.capabilities())
    available = brain.tool_executor.available_capabilities_for_organization(organization_id)
    indexed = {
        str(item.get("capability_id")): item
        for item in brain.store.list_harness_records("capabilities", organization_id)
        if item.get("capability_id")
    }
    return {
        "organization_id": organization_id,
        "capabilities": [
            {
                "id": capability,
                "registered": True,
                "available": capability in available,
                "runtime_ready": bool((indexed.get(capability) or {}).get("runtime_ready", capability in available)),
                "setup_needed": (indexed.get(capability) or {}).get("setup_needed", []),
                "tool_names": (indexed.get(capability) or {}).get("tool_names", []),
            }
            for capability in registered
        ],
    }


@router.get("/v1/skills/catalog/{name}/resources/{relative_path:path}")
def skill_resource(name: str, relative_path: str, request: Request) -> dict[str, Any]:
    try:
        return _app(request).skill_packages.resource(name, relative_path)
    except KeyError as exc:
        raise HTTPException(404, "skill package not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "skill resource not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/skills/{name}/resources/{relative_path:path}")
def skill_resource_alias(name: str, relative_path: str, request: Request) -> dict[str, Any]:
    return skill_resource(name, relative_path, request)


@router.get("/v1/skills/{skill_id}")
def get_skill(skill_id: str, request: Request) -> dict[str, Any]:
    brain = _app(request)
    skill = brain.skills.get(skill_id)
    if not skill:
        package = brain.skill_packages.get(skill_id)
        if package:
            return {
                "metadata": next(
                    item for item in brain.skill_packages.metadata()
                    if item["name"] == skill_id
                ),
                "instructions": package.body,
            }
        raise HTTPException(404, "skill not found")
    return skill


@router.get("/v1/skills")
def list_skills(
    request: Request, organization_id: str
) -> dict[str, Any]:
    records = _app(request).skills.list(organization_id)
    return {"skills": records, "count": len(records)}


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


@router.post("/v1/skills:validate")
def validate_skill(body: dict[str, Any], request: Request) -> dict[str, Any]:
    return _app(request).skills.validate_definition(body)


@router.post("/v1/skills/{skill_id}:propose-update")
def propose_skill_update(skill_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    try:
        proposal = _app(request).skills.propose_update(skill_id, body.get("changes") or {})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
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
    try:
        skill = _app(request).skills.approve(skill_id, version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not skill:
        raise HTTPException(404, "skill version not found")
    return skill


@router.post("/v1/skills/{skill_id}:deprecate")
def deprecate_skill(skill_id: str, request: Request) -> dict[str, Any]:
    try:
        skill = _app(request).skills.deprecate(skill_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not skill:
        raise HTTPException(404, "skill not found")
    return skill


@router.get("/v1/workspaces")
def list_workspaces(request: Request, organization_id: str) -> dict[str, Any]:
    records = _app(request).workspaces.list(organization_id)
    return {"workspaces": records, "count": len(records)}


@router.get("/v1/workspaces/{workspace_id}")
def get_workspace(workspace_id: str, request: Request, organization_id: str) -> dict[str, Any]:
    record = _app(request).store.get_harness_record("workspaces", workspace_id)
    if not record or record.get("organization_id") != organization_id:
        raise HTTPException(404, "workspace not found")
    return record


@router.post("/v1/workspaces/{workspace_id}:expire")
def expire_workspace(workspace_id: str, request: Request, organization_id: str) -> dict[str, Any]:
    record = _app(request).workspaces.expire(workspace_id, organization_id)
    if not record:
        raise HTTPException(404, "workspace not found")
    return record


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


# ---- Toda Console SPA (built with Vite from web/) ----------------------
# Serves the React console at `/` and any non-API path. During development,
# run `npm run dev` inside web/ (proxies /v1 to this server).
_PACKAGED_CONSOLE_DIST = Path(__file__).resolve().parents[1] / "static" / "console"
_SOURCE_CONSOLE_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
_CONSOLE_DIST = (
    _PACKAGED_CONSOLE_DIST
    if _PACKAGED_CONSOLE_DIST.is_dir()
    else _SOURCE_CONSOLE_DIST
)
if _CONSOLE_DIST.is_dir():
    router.mount(
        "/assets",
        StaticFiles(directory=_CONSOLE_DIST / "assets"),
        name="console-assets",
    )

    @router.get("/", include_in_schema=False)
    def console_root() -> FileResponse:
        return FileResponse(_CONSOLE_DIST / "index.html")

    @router.get("/{full_path:path}", include_in_schema=False)
    def console_spa(full_path: str, request: Request) -> FileResponse:
        # Serve the SPA shell for any non-API client-side route. API routes
        # under /v1 are matched before this catch-all kicks in.
        if full_path.startswith("v1/") or full_path in {"docs", "openapi.json", "redoc"}:
            raise HTTPException(404)
        candidate = _CONSOLE_DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_CONSOLE_DIST / "index.html")
else:
    @router.get("/", include_in_schema=False)
    def console_not_built() -> dict[str, str]:
        return {
            "message": (
                "Toda Console not built. Run: cd web && npm install && npm run build"
            ),
            "api_docs": "/docs",
        }
