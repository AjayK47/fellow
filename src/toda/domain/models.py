"""Core domain models for Toda V1."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EntityType(str, Enum):
    PERSON = "person"
    TEAM = "team"
    PROJECT = "project"
    SERVICE = "service"
    CUSTOMER = "customer"
    CHANNEL = "channel"
    SOURCE = "source"
    DOCUMENT = "document"
    TASK = "task"
    INCIDENT = "incident"
    SKILL = "skill"
    AGENT = "agent"
    APPLICATION = "application"
    CONVERSATION = "conversation"
    ISSUE = "issue"
    DECISION = "decision"
    COMMITMENT = "commitment"


class MemoryType(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    COMMITMENT = "commitment"
    ISSUE = "issue"
    RELATIONSHIP = "relationship"
    PREFERENCE = "preference"
    EPISODE_SUMMARY = "episode_summary"
    OUTCOME = "outcome"
    DERIVED = "derived"


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    RETRACTED = "retracted"
    DELETED = "deleted"
    QUARANTINED = "quarantined"


class MemoryEdgeType(str, Enum):
    UPDATES = "UPDATES"
    EXTENDS = "EXTENDS"
    DERIVES_FROM = "DERIVES_FROM"
    SUPPORTED_BY = "SUPPORTED_BY"
    CONTRADICTS = "CONTRADICTS"
    CORRECTS = "CORRECTS"
    ABOUT = "ABOUT"
    INVOLVES = "INVOLVES"
    SCOPED_TO = "SCOPED_TO"
    CAUSED_BY = "CAUSED_BY"
    RESULTED_IN = "RESULTED_IN"


class OrgEdgeType(str, Enum):
    MEMBER_OF = "MEMBER_OF"
    WORKS_ON = "WORKS_ON"
    OWNS = "OWNS"
    DEPENDS_ON = "DEPENDS_ON"
    ACCOUNT_OWNED_BY = "ACCOUNT_OWNED_BY"
    ON_CALL_FOR = "ON_CALL_FOR"
    AFFECTS = "AFFECTS"
    MENTIONED_IN = "MENTIONED_IN"
    PARTICIPATED_IN = "PARTICIPATED_IN"
    COMMITTED_TO = "COMMITTED_TO"
    RELATED_TO = "RELATED_TO"
    GENERATED_BY = "GENERATED_BY"
    ABOUT = "ABOUT"
    CAN_VIEW = "CAN_VIEW"
    CAN_EDIT = "CAN_EDIT"
    VISIBLE_TO = "VISIBLE_TO"
    ACTS_AS = "ACTS_AS"
    AUTHORIZED_FOR = "AUTHORIZED_FOR"
    INHERITS_ACL_FROM = "INHERITS_ACL_FROM"


class ScopeKind(str, Enum):
    ORGANIZATION_PUBLIC = "organization_public"
    PRIVATE_CHANNEL = "private_channel"
    PERSON_PRIVATE = "person_private"
    PROJECT = "project"
    TEAM = "team"
    CUSTOM = "custom"


class SkillStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class SignalType(str, Enum):
    VERIFIED_MISSING_ANSWER = "verified_missing_answer"
    VERIFIED_CORRECTION = "verified_correction"
    UNACKNOWLEDGED_CUSTOMER_ISSUE = "unacknowledged_customer_issue"
    VERIFIED_OPERATIONAL_INCIDENT = "verified_operational_incident"


class ProactiveDecision(str, Enum):
    SILENT = "silent"
    REMEMBER_ONLY = "remember_only"
    DIGEST = "digest"
    REPLY = "reply_in_context"
    PRIVATE_ROUTE = "private_route"
    ESCALATE = "escalate"


class IssueState(str, Enum):
    DETECTED = "detected"
    TRIAGED = "triaged"
    ROUTED = "routed"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    ESCALATED = "escalated"


# ---------------------------------------------------------------------------
# Core objects
# ---------------------------------------------------------------------------


class Organization(BaseModel):
    id: str
    name: str
    region: str = "us-east-1"
    about: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class SourceEvidence(BaseModel):
    id: str = Field(default_factory=lambda: new_id("src"))
    organization_id: str
    source_system: str
    source_type: str
    external_id: str
    parent_external_id: str | None = None
    author_entity_id: str | None = None
    occurred_at: datetime = Field(default_factory=utcnow)
    ingested_at: datetime = Field(default_factory=utcnow)
    content: str = ""
    content_uri: str = ""
    content_hash: str = ""
    version: int = 1
    deleted_at: datetime | None = None
    revoked: bool = False
    acl_snapshot_id: str = ""
    scope_id: str
    channel_id: str | None = None
    is_private_channel: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    entity_ids: list[str] = Field(default_factory=list)


class Entity(BaseModel):
    id: str
    organization_id: str
    entity_type: EntityType
    name: str
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class GraphEdge(BaseModel):
    id: str = Field(default_factory=lambda: new_id("edge"))
    organization_id: str
    edge_type: str
    from_id: str
    to_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    # valid_from/valid_until is when the relationship was/is true in the real
    # world; created_at (never revised) is this row's system time; system_to
    # is when we recorded that it stopped being current — the same
    # valid-time/system-time split reality_facts already has, so "who owns X"
    # and "who owned X last quarter" are both answerable without stale edges
    # silently coexisting with their replacements.
    valid_from: datetime = Field(default_factory=utcnow)
    valid_until: datetime | None = None
    system_to: datetime | None = None
    superseded_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class CanonicalClaim(BaseModel):
    subject: str
    predicate: str
    object: str


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    organization_id: str
    memory_type: MemoryType
    content: str
    canonical_claim: CanonicalClaim | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    confidence: float = 0.9
    authority: float = 0.8
    valid_from: datetime = Field(default_factory=utcnow)
    valid_until: datetime | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)
    superseded_at: datetime | None = None
    is_latest: bool = True
    scope_id: str
    extraction_method: str = "deterministic_v1"
    model_version: str = "heuristic-1"
    source_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    # Who actually asserted this, as distinct from everyone involved in it.
    # The author used to be smuggled in as a single-element participant list,
    # which told a reader these people were present but never which one spoke
    # -- so "who said that?" was unanswerable even with the data in hand.
    author_entity_id: str | None = None
    participant_ids: list[str] = Field(default_factory=list)
    source_spans: list[dict[str, Any]] = Field(default_factory=list)
    is_private: bool = False


class Scope(BaseModel):
    id: str
    organization_id: str
    kind: ScopeKind
    label: str
    channel_id: str | None = None
    principal_ids: list[str] = Field(default_factory=list)
    is_public: bool = False


class SkillStep(BaseModel):
    order: int
    name: str
    description: str
    required: bool = True
    approvals: list[str] = Field(default_factory=list)


class SkillActivationMode(str, Enum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class SkillActivation(BaseModel):
    destination_types: list[str] = Field(default_factory=list)
    trigger_kinds: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)


class SkillResponseValidation(BaseModel):
    enabled: bool = False
    instructions: str = ""


class SkillKind(str, Enum):
    BUILTIN = "builtin"
    CONNECTOR = "connector"
    COMPANY = "company"
    MARKETPLACE = "marketplace"


class Skill(BaseModel):
    id: str = Field(default_factory=lambda: new_id("skill"))
    organization_id: str
    name: str
    description: str
    goal: str
    version: int = 1
    status: SkillStatus = SkillStatus.ACTIVE
    inputs: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[SkillStep] = Field(default_factory=list)
    expected_evidence: list[str] = Field(default_factory=list)
    completion_conditions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    activation_mode: SkillActivationMode = SkillActivationMode.OPTIONAL
    activation: SkillActivation = Field(default_factory=SkillActivation)
    response_validation: SkillResponseValidation = Field(
        default_factory=SkillResponseValidation
    )
    instructions: str = ""
    kind: SkillKind = SkillKind.COMPANY
    immutable: bool = False
    content_hash: str = ""
    package_path: str = ""
    license: str = "unspecified"
    origin: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: dict[str, Any] = Field(default_factory=dict)
    platform_key: str | None = None
    owner_id: str | None = None
    scope_id: str = "scope_org_public"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sig"))
    organization_id: str
    signal_type: SignalType
    state: IssueState = IssueState.DETECTED
    confidence: float = 0.0
    content: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    destination_id: str | None = None
    recipient_id: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    acknowledged: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("audit"))
    organization_id: str
    event_type: str
    principal_id: str | None = None
    application_id: str | None = None
    trace_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class AuthorizationDecision(BaseModel):
    id: str = Field(default_factory=lambda: new_id("authz"))
    organization_id: str
    acting_principal_id: str
    destination_type: str | None = None
    destination_id: str | None = None
    purpose: str | None = None
    allowed_scope_ids: list[str] = Field(default_factory=list)
    denied_reasons: list[str] = Field(default_factory=list)
    fail_closed: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Destination(BaseModel):
    type: str
    id: str
    is_private: bool = False
    audience_principal_ids: list[str] = Field(default_factory=list)


class ContextPackage(BaseModel):
    request_id: str = Field(default_factory=lambda: new_id("ctx"))
    permission_decision_id: str
    viewer: dict[str, Any] = Field(default_factory=dict)
    organization_about: str = ""
    destination: dict[str, Any] = Field(default_factory=dict)
    task: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    entities: list[dict[str, Any]] = Field(default_factory=list)
    people: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    current_facts: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    open_commitments: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    outcomes: list[dict[str, Any]] = Field(default_factory=list)
    episodes: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    related_messages: list[dict[str, Any]] = Field(default_factory=list)
    active_runs: list[dict[str, Any]] = Field(default_factory=list)
    active_cases: list[dict[str, Any]] = Field(default_factory=list)
    judgment_guidance: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    freshness: dict[str, Any] = Field(default_factory=dict)
    token_estimate: int = 0
    token_budget: int = 8000


class SearchResult(BaseModel):
    memory: dict[str, Any]
    score: float
    graph_paths: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)


class OwnerResolution(BaseModel):
    primary_owner: dict[str, Any] | None = None
    on_call: dict[str, Any] | None = None
    team: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    fallback_path: list[dict[str, Any]] = Field(default_factory=list)
    permission_decision_id: str | None = None


class ProactiveEvaluationResult(BaseModel):
    decision: ProactiveDecision
    signal_type: str | None = None
    confidence: float = 0.0
    reason: str
    suggested_message: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    permission_decision_id: str | None = None
    policy_explanation: dict[str, Any] = Field(default_factory=dict)
    cooldown: dict[str, Any] = Field(default_factory=lambda: {"eligible": True})


class ProactiveCandidate(BaseModel):
    signal_type: str
    confidence: float = 0.0
    evidence_authority: float = 0.0
    novelty: float = 1.0
    usefulness: float = 0.0
    urgency: float = 0.0
    actionability: float = 0.0
    interruption_cost: float = 0.5
    privacy_risk: float = 0.0
    social_sensitivity: float = 0.0
    permission_safe: bool = False
    already_answered: bool = False
    cooldown_eligible: bool = True
    content: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
