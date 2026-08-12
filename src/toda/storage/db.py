"""SQLite-backed control plane and property graph store."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Iterator
from uuid import uuid4

import sqlite_vec

log = logging.getLogger("toda.store")

# Tables holding ANN vector indexes, keyed by the id of the row they index
# (chunk_id / memory_id / episode_id). These are a *derived* index over the
# JSON `embedding` columns in chunks/memory_embeddings/episodes, which stay
# the source of truth — reindex_vectors() rebuilds them from those columns,
# so nothing is lost if the vec0 tables are dropped or the embedding
# dimension changes.
#
# Every one is partitioned by organization_id. That is not only a tenancy
# guard: an unpartitioned KNN takes the global top-k across ALL orgs, so a
# large tenant's rows crowd a small tenant's equally-relevant rows out of
# the shortlist entirely and the small tenant silently gets zero semantic
# results. Partitioning makes `k` mean "k within this org", which is the
# only reading that is correct in a shared database.
_VEC_TABLES = ("vec_chunks", "vec_memories", "vec_episodes", "vec_tasks")

# The vec0 column width is fixed at CREATE time, but the true width is a
# property of whatever embedding provider is configured — not of any
# setting (TODA_EMBEDDING_DIMENSIONS only ever described the offline
# deterministic fallback; a real provider returns whatever its model
# returns, e.g. 1536 or 4096). So the width is *observed* from the first
# vector actually written and recorded here, and the vec tables are rebuilt
# if it ever changes. Guessing it from config meant every ingest died with
# "Dimension mismatch" the moment a real provider was configured.
_VECTOR_META_TABLE = "vector_index_meta"


def _ser(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    return json.dumps(value, default=str)


def _de(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class BrainStore:
    """Tenant-aware store for evidence, graph, memories, Tasks, and audit."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._vector_lock = threading.Lock()
        self._local = threading.local()
        bootstrap = self._new_connection()
        bootstrap.execute("PRAGMA journal_mode=WAL")
        bootstrap.close()
        self._migrate()

    def close(self) -> None:
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            conn.close()
            del self._local.connection
        self._closed = True

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("BrainStore is closed")
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = self._new_connection()
            self._local.connection = conn
        return conn

    @contextmanager
    def cursor(self, *, immediate: bool = False) -> Iterator[sqlite3.Cursor]:
        conn = self._connection()
        cur = conn.cursor()
        try:
            if immediate:
                cur.execute("BEGIN IMMEDIATE")
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def _migrate(self) -> None:
        with self.cursor() as cur:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    external_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    group_key TEXT,
                    occurred_at TEXT,
                    payload TEXT NOT NULL,
                    UNIQUE (organization_id, external_key, version)
                );

                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entity_aliases (
                    organization_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    PRIMARY KEY (organization_id, entity_id, normalized, kind)
                );

                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    from_id TEXT NOT NULL,
                    to_id TEXT NOT NULL,
                    valid_until TEXT,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_latest INTEGER NOT NULL DEFAULT 1,
                    scope_id TEXT NOT NULL,
                    is_private INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scopes (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE (organization_id, id, version)
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS authz_decisions (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processing_jobs (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS received_events (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );

                -- The single ordered record of what was actually said in a
                -- Slack conversation, inbound and outbound alike.
                --
                -- Before this existed, a conversation timeline was
                -- RECONSTRUCTED on every read by unioning raw
                -- `received_events` with `deliveries` joined to `tasks`.
                -- Two sources, two write paths, two different latencies --
                -- and two real bugs: a Task could be shown an inbound
                -- message that had not been routed to it yet (answering it,
                -- and acting on it, before the Task that owned it existed),
                -- while anything Toda sent through the slack_send_message
                -- TOOL was invisible in its own history, because only
                -- delivery-path sends wrote a row the reconstruction could
                -- find. One append-only log written at the two real choke
                -- points (EventRouter.route for inbound, SlackSender.send
                -- for every outbound) removes both classes of bug by
                -- construction.
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT,
                    role TEXT NOT NULL,
                    author TEXT,
                    text TEXT NOT NULL,
                    at TEXT NOT NULL,
                    UNIQUE (organization_id, workspace_id, channel_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    organization_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    key TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (organization_id, operation, key)
                );

                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    profile_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, profile_type, entity_id)
                );

                CREATE TABLE IF NOT EXISTS skill_versions (
                    skill_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (skill_id, version)
                );

                CREATE TABLE IF NOT EXISTS task_turns (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (task_id, turn_number)
                );

                CREATE TABLE IF NOT EXISTS harness_tool_calls (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_states (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, conversation_key)
                );

                CREATE TABLE IF NOT EXISTS attention_evaluations (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_interventions (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_feedback (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT,
                    attention_evaluation_id TEXT,
                    feedback_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS harness_jobs (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS slack_installations (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (workspace_id)
                );

                CREATE TABLE IF NOT EXISTS channel_policies (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, source, channel_id)
                );

                CREATE TABLE IF NOT EXISTS identity_mappings (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_user_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE (organization_id, source, source_user_id)
                );

                CREATE TABLE IF NOT EXISTS connector_accounts (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    toolkit TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_policies (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, tool_name)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (task_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    group_kind TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    last_source_id TEXT,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    summary TEXT,
                    summary_embedding TEXT,
                    extraction_revision INTEGER NOT NULL DEFAULT 0,
                    scope_id TEXT NOT NULL,
                    privacy_class TEXT NOT NULL DEFAULT 'internal',
                    error TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS episode_sources (
                    episode_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    UNIQUE (episode_id, source_id)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
                    episode_id UNINDEXED, organization_id UNINDEXED, scope_id UNINDEXED, summary
                );

                CREATE TABLE IF NOT EXISTS automations (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    next_run_at TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, name)
                );

                -- Harness capability/provenance projections.  These tables
                -- deliberately keep the complete record in JSON so package
                -- and connector schemas can evolve without losing history.
                CREATE TABLE IF NOT EXISTS skill_packages (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capabilities (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_skill_refs (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_capability_refs (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_scratchpads (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_watermarks (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_pattern_signals (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Session/Task architecture: one durable Session per Slack
                -- conversation, one logical Session Manager per Session
                -- (atomically claimed -- manager_owner/manager_lease_expires_at
                -- form a compare-and-swap lock, same pattern as
                -- lease_harness_jobs), and one unified Task type. Real
                -- columns exist only where claims/filtering/uniqueness need
                -- them; everything else lives in payload.
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    manager_state TEXT NOT NULL DEFAULT 'idle',
                    manager_owner TEXT,
                    manager_lease_expires_at TEXT,
                    manager_revision INTEGER NOT NULL DEFAULT 0,
                    processed_revision INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, conversation_key)
                );

                CREATE TABLE IF NOT EXISTS session_messages (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_call_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    parent_task_id TEXT,
                    root_task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    worker_type TEXT NOT NULL,
                    execution_owner TEXT,
                    execution_lease_expires_at TEXT,
                    wake_revision INTEGER NOT NULL DEFAULT 0,
                    processed_revision INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
                    task_id UNINDEXED, organization_id UNINDEXED, content
                );
                CREATE TABLE IF NOT EXISTS task_embeddings (
                    task_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_attempts (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_signals (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL
                );

                -- A standing, semantic trigger: whenever a company event's text
                -- matches trigger_description, the attention pipeline wakes the
                -- main agent with the bound goal/skill bundle activated, the same
                -- way related_cases already surfaces token-overlap matches. Not
                -- bound to any channel/scope by default since real companies
                -- don't have clean, purpose-specific channel structures.
                CREATE TABLE IF NOT EXISTS roles (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED, organization_id UNINDEXED, scope_id UNINDEXED, content
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    memory_id UNINDEXED, organization_id UNINDEXED, scope_id UNINDEXED, content
                );

                CREATE TABLE IF NOT EXISTS principal_memberships (
                    organization_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    PRIMARY KEY (organization_id, principal_id, scope_id)
                );

                CREATE TABLE IF NOT EXISTS connector_objects (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    connector_account_id TEXT,
                    provider TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    occurred_at TEXT,
                    observed_at TEXT NOT NULL,
                    deleted_at TEXT,
                    payload TEXT NOT NULL,
                    UNIQUE (
                        organization_id, provider, object_type, external_id, version
                    )
                );

                CREATE TABLE IF NOT EXISTS connector_sync_runs (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    connector_account_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cursor_before TEXT,
                    cursor_after TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS connector_sync_cursors (
                    organization_id TEXT NOT NULL,
                    connector_account_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    cursor TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (
                        organization_id, connector_account_id, object_type
                    )
                );

                CREATE TABLE IF NOT EXISTS external_identifiers (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (organization_id, namespace, normalized_value)
                );

                CREATE TABLE IF NOT EXISTS entity_resolution_candidates (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    left_entity_id TEXT NOT NULL,
                    right_entity_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reality_facts (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_value TEXT NOT NULL,
                    status TEXT NOT NULL,
                    valid_from TEXT,
                    valid_to TEXT,
                    system_from TEXT NOT NULL,
                    system_to TEXT,
                    scope_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fact_conflicts (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    case_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS case_events (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    occurred_at TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (case_id, source_id)
                );

                CREATE TABLE IF NOT EXISTS judgment_events (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    principal_id TEXT,
                    judgment_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS judgment_embeddings (
                    judgment_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS policy_versions (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    policy_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (organization_id, policy_key, version)
                );

                CREATE TABLE IF NOT EXISTS action_preconditions (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    checked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    task_id TEXT,
                    artifact_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conversation_messages_channel
                    ON conversation_messages(organization_id, workspace_id, channel_id, at);
                CREATE INDEX IF NOT EXISTS idx_memories_org_latest
                    ON memories(organization_id, is_latest, status);
                CREATE INDEX IF NOT EXISTS idx_edges_org_type
                    ON edges(organization_id, edge_type);
                CREATE INDEX IF NOT EXISTS idx_edges_from_id
                    ON edges(organization_id, from_id, edge_type, valid_until);
                CREATE INDEX IF NOT EXISTS idx_edges_to_id
                    ON edges(organization_id, to_id, edge_type, valid_until);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_active_unique
                    ON edges(organization_id, edge_type, from_id, to_id)
                    WHERE valid_until IS NULL;
                CREATE INDEX IF NOT EXISTS idx_sources_org
                    ON sources(organization_id);
                CREATE INDEX IF NOT EXISTS idx_entities_org
                    ON entities(organization_id, entity_type);
                CREATE INDEX IF NOT EXISTS idx_entity_alias_lookup
                    ON entity_aliases(organization_id, normalized, entity_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_org_scope ON chunks(organization_id, scope_id);
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_org_scope
                    ON memory_embeddings(organization_id, scope_id);
                CREATE INDEX IF NOT EXISTS idx_connector_objects_lookup
                    ON connector_objects(
                        organization_id, provider, object_type, external_id
                    );
                CREATE INDEX IF NOT EXISTS idx_sync_runs_org_status
                    ON connector_sync_runs(organization_id, status);
                CREATE INDEX IF NOT EXISTS idx_external_identifiers_entity
                    ON external_identifiers(organization_id, entity_id);
                CREATE INDEX IF NOT EXISTS idx_resolution_candidates_status
                    ON entity_resolution_candidates(organization_id, status);
                CREATE INDEX IF NOT EXISTS idx_reality_facts_current
                    ON reality_facts(
                        organization_id, subject_id, predicate, system_to
                    );
                CREATE INDEX IF NOT EXISTS idx_cases_org_status
                    ON cases(organization_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_case_events_case
                    ON case_events(case_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_judgments_org
                    ON judgment_events(organization_id, judgment_type);
                CREATE INDEX IF NOT EXISTS idx_policy_versions_current
                    ON policy_versions(organization_id, policy_key, status);
                CREATE INDEX IF NOT EXISTS idx_artifacts_org
                    ON artifacts(organization_id, artifact_type, updated_at);
                CREATE INDEX IF NOT EXISTS idx_processing_jobs_org ON processing_jobs(organization_id, status);
                CREATE INDEX IF NOT EXISTS idx_profiles_org_entity ON profiles(organization_id, entity_id);
                CREATE INDEX IF NOT EXISTS idx_skill_versions_org ON skill_versions(organization_id, skill_id);
                CREATE INDEX IF NOT EXISTS idx_task_turns_task
                    ON task_turns(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_task
                    ON harness_tool_calls(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_approvals_task_status
                    ON approvals(task_id, status);
                CREATE INDEX IF NOT EXISTS idx_attention_event
                    ON attention_evaluations(organization_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_interventions_conversation
                    ON task_interventions(organization_id, conversation_key, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_feedback_org
                    ON agent_feedback(organization_id, feedback_type, created_at);
                CREATE INDEX IF NOT EXISTS idx_harness_jobs_ready
                    ON harness_jobs(status, available_at, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_manager_state
                    ON sessions(organization_id, manager_state);
                CREATE INDEX IF NOT EXISTS idx_session_messages_session
                    ON session_messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_session
                    ON tasks(session_id, state);
                CREATE INDEX IF NOT EXISTS idx_tasks_parent
                    ON tasks(parent_task_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_root
                    ON tasks(root_task_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_org_state
                    ON tasks(organization_id, state, worker_type);
                CREATE INDEX IF NOT EXISTS idx_tasks_expired_lease
                    ON tasks(organization_id, state, execution_lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_recovery_waiting
                    ON tasks(organization_id, state, parent_task_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_audit_events_since
                    ON audit_events(organization_id, event_type, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_attempts_task
                    ON task_attempts(task_id, attempt_number);
                CREATE INDEX IF NOT EXISTS idx_task_signals_task
                    ON task_signals(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_episodes_org_group
                    ON episodes(organization_id, group_key, updated_at);
                CREATE INDEX IF NOT EXISTS idx_sources_org_group
                    ON sources(organization_id, group_key, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_episode_sources_episode
                    ON episode_sources(episode_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_episode_sources_source
                    ON episode_sources(source_id);
                CREATE INDEX IF NOT EXISTS idx_episode_sources_org
                    ON episode_sources(organization_id);
                """
            )
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {_VECTOR_META_TABLE} (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    dimensions INTEGER NOT NULL
                )"""
            )

    # ---- vector index lifecycle ----

    def vector_dimensions(self) -> int | None:
        """The width the vec0 tables are currently built for, or None if no
        vector has ever been written."""
        with self.cursor() as cur:
            cur.execute(f"SELECT dimensions FROM {_VECTOR_META_TABLE} WHERE id = 1")
            row = cur.fetchone()
            return int(row["dimensions"]) if row else None

    def _ensure_vector_tables(self, dimensions: int) -> bool:
        """Make the vec0 tables exist at `dimensions` wide. Returns True if
        they were (re)built, meaning previously indexed vectors are gone and
        a reindex is needed.

        A width change means the embedding model itself changed, so the old
        ANN entries are meaningless anyway — dropping is the correct
        response, not an error. The JSON embedding columns remain the source
        of truth throughout.
        """
        if dimensions <= 0:
            return False
        with self._vector_lock:
            current = self.vector_dimensions()
            if current == dimensions:
                # A newer Toda release may add another derived vector index
                # while retaining the same embedding width. Create only the
                # missing table; rebuilding healthy indexes would be wasteful.
                with self.cursor() as cur:
                    for table in _VEC_TABLES:
                        cur.execute(
                            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (table,),
                        )
                        if cur.fetchone() is None:
                            cur.execute(
                                f"""CREATE VIRTUAL TABLE {table} USING vec0(
                                    organization_id TEXT partition key,
                                    id TEXT PRIMARY KEY,
                                    embedding float[{int(dimensions)}] distance_metric=cosine
                                )"""
                            )
                return False
            with self.cursor() as cur:
                if current is not None:
                    log.warning(
                        "Embedding width changed %s -> %s; rebuilding vector indexes.",
                        current,
                        dimensions,
                    )
                for table in _VEC_TABLES:
                    cur.execute(f"DROP TABLE IF EXISTS {table}")
                    # vec0 virtual tables are created one statement at a
                    # time (not in the main executescript) because the
                    # width is only known here, at first write.
                    cur.execute(
                        f"""CREATE VIRTUAL TABLE {table} USING vec0(
                            organization_id TEXT partition key,
                            id TEXT PRIMARY KEY,
                            embedding float[{int(dimensions)}] distance_metric=cosine
                        )"""
                    )
                cur.execute(
                    f"INSERT INTO {_VECTOR_META_TABLE}(id, dimensions) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET dimensions = excluded.dimensions",
                    (int(dimensions),),
                )
            return True

    def reindex_vectors(self, *, force: bool = False) -> dict[str, int]:
        """Rebuild the vec0 ANN tables from the JSON embedding columns that
        remain the source of truth.

        Rows whose stored embedding is a different width than the current
        index are skipped and counted, never inserted — those pre-date an
        embedding model change and need re-embedding (IndexingService.
        backfill), which is a separate, provider-calling operation. Skipping
        keeps reindex a safe, offline, always-succeeding repair.
        """
        target = self.vector_dimensions()
        if force and target:
            self._force_rebuild_vector_tables(target)

        counts = {"chunks": 0, "memories": 0, "episodes": 0, "tasks": 0, "skipped_wrong_width": 0}
        for table, source_sql, upsert in (
            (
                "chunks",
                "SELECT id, organization_id, embedding AS vec FROM chunks",
                self.vec_upsert_chunk,
            ),
            (
                "memories",
                "SELECT memory_id AS id, organization_id, embedding AS vec FROM memory_embeddings",
                self.vec_upsert_memory,
            ),
            (
                "episodes",
                "SELECT id, organization_id, summary_embedding AS vec FROM episodes "
                "WHERE summary_embedding IS NOT NULL AND summary_embedding != 'null'",
                self.vec_upsert_episode,
            ),
            (
                "tasks",
                "SELECT task_id AS id, organization_id, embedding AS vec FROM task_embeddings",
                self.vec_upsert_task,
            ),
        ):
            with self.cursor() as cur:
                cur.execute(source_sql)
                rows = cur.fetchall()
            for row in rows:
                embedding = _de(row["vec"]) or []
                if not embedding:
                    continue
                target = self.vector_dimensions()
                if target is not None and len(embedding) != target:
                    counts["skipped_wrong_width"] += 1
                    continue
                upsert(row["organization_id"], row["id"], embedding)
                counts[table] += 1
        return counts

    def _force_rebuild_vector_tables(self, dimensions: int) -> None:
        with self._vector_lock, self.cursor() as cur:
            for table in _VEC_TABLES:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
                cur.execute(
                    f"""CREATE VIRTUAL TABLE {table} USING vec0(
                        organization_id TEXT partition key,
                        id TEXT PRIMARY KEY,
                        embedding float[{int(dimensions)}] distance_metric=cosine
                    )"""
                )

    # ---- generic helpers ----

    def put_json(self, table: str, id_: str, organization_id: str, payload: dict[str, Any], **cols: Any) -> None:
        with self.cursor() as cur:
            if table == "organizations":
                cur.execute(
                    "INSERT OR REPLACE INTO organizations(id, payload) VALUES (?, ?)",
                    (id_, _ser(payload)),
                )
                return
            columns = ["id", "organization_id", "payload"] + list(cols.keys())
            placeholders = ", ".join("?" for _ in columns)
            col_names = ", ".join(columns)
            values = [id_, organization_id, _ser(payload)] + list(cols.values())
            # Use upsert-ish REPLACE for simplicity
            cur.execute(f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})", values)

    def get_json(self, table: str, id_: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(f"SELECT payload FROM {table} WHERE id = ?", (id_,))
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def list_by_org(self, table: str, organization_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(f"SELECT payload FROM {table} WHERE organization_id = ?", (organization_id,))
            return [_de(r["payload"]) for r in cur.fetchall()]

    # ---- sources ----

    def upsert_source(self, source: dict[str, Any]) -> None:
        """Insert or update a source by primary id. Versions of the same external_key coexist."""
        external_key = f"{source['source_system']}:{source['external_id']}"
        group_key = (source.get("metadata") or {}).get("group_key")
        occurred_at = source.get("occurred_at")
        if not isinstance(occurred_at, str):
            occurred_at = str(occurred_at) if occurred_at else None
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sources
                (id, organization_id, external_key, version, revoked, deleted_at, group_key, occurred_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    organization_id=excluded.organization_id,
                    external_key=excluded.external_key,
                    version=excluded.version,
                    revoked=excluded.revoked,
                    deleted_at=excluded.deleted_at,
                    group_key=excluded.group_key,
                    occurred_at=excluded.occurred_at,
                    payload=excluded.payload
                """,
                (
                    source["id"],
                    source["organization_id"],
                    external_key,
                    source.get("version", 1),
                    1 if source.get("revoked") else 0,
                    source.get("deleted_at"),
                    group_key,
                    occurred_at,
                    _ser(source),
                ),
            )

    def next_source_version(
        self, organization_id: str, source_system: str, external_id: str
    ) -> int:
        external_key = f"{source_system}:{external_id}"
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS max_v
                FROM sources
                WHERE organization_id = ? AND external_key = ?
                """,
                (organization_id, external_key),
            )
            row = cur.fetchone()
            return int(row["max_v"] or 0) + 1

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        return self.get_json("sources", source_id)

    def list_source_versions(
        self, organization_id: str, source_system: str, external_id: str
    ) -> list[dict[str, Any]]:
        external_key = f"{source_system}:{external_id}"
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT payload FROM sources
                WHERE organization_id = ? AND external_key = ?
                ORDER BY version ASC
                """,
                (organization_id, external_key),
            )
            return [_de(r["payload"]) for r in cur.fetchall()]

    def list_sources(self, organization_id: str, include_revoked: bool = False) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            if include_revoked:
                cur.execute("SELECT payload FROM sources WHERE organization_id = ?", (organization_id,))
            else:
                cur.execute(
                    "SELECT payload FROM sources WHERE organization_id = ? AND revoked = 0 AND deleted_at IS NULL",
                    (organization_id,),
                )
            return [_de(r["payload"]) for r in cur.fetchall()]

    def list_sources_by_group(
        self,
        organization_id: str,
        group_key: str,
        *,
        after_occurred_at: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Sources belonging to one episode group, in occurrence order — an
        indexed lookup (idx_sources_org_group) rather than a Python scan
        over every source the org has ever ingested, which is what made the
        old Slack-only consolidate_conversation reload the entire history
        on every quiet-window tick."""
        clauses = ["organization_id = ?", "group_key = ?", "revoked = 0", "deleted_at IS NULL"]
        params: list[Any] = [organization_id, group_key]
        if after_occurred_at:
            clauses.append("occurred_at > ?")
            params.append(after_occurred_at)
        sql = (
            f"SELECT payload FROM sources WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_at ASC LIMIT ?"
        )
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [_de(row["payload"]) for row in cur.fetchall()]

    def revoke_source(self, source_id: str) -> dict[str, Any] | None:
        src = self.get_source(source_id)
        if not src:
            return None
        src["revoked"] = True
        src["deleted_at"] = datetime.now(timezone.utc).isoformat()
        self.upsert_source(src)
        return src

    # ---- episodes ----

    def get_episode_by_group_key(self, organization_id: str, group_key: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                "SELECT payload FROM episodes WHERE organization_id = ? AND group_key = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (organization_id, group_key),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def upsert_episode(self, episode: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO episodes
                (id, organization_id, group_key, group_kind, source_system, started_at, ended_at,
                 last_source_id, event_count, status, summary, summary_embedding, extraction_revision,
                 scope_id, privacy_class, error, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    episode["id"], episode["organization_id"], episode["group_key"], episode["group_kind"],
                    episode["source_system"], episode["started_at"], episode["ended_at"],
                    episode.get("last_source_id"), episode.get("event_count", 0), episode["status"],
                    episode.get("summary"), _ser(episode.get("summary_embedding")),
                    episode.get("extraction_revision", 0), episode["scope_id"],
                    episode.get("privacy_class", "internal"), episode.get("error"),
                    _ser(episode), episode["created_at"], episode["updated_at"],
                ),
            )
            cur.execute("DELETE FROM episodes_fts WHERE episode_id = ?", (episode["id"],))
            summary = episode.get("summary") or ""
            if summary.strip():
                cur.execute(
                    "INSERT INTO episodes_fts(episode_id, organization_id, scope_id, summary) VALUES (?, ?, ?, ?)",
                    (episode["id"], episode["organization_id"], episode["scope_id"], summary),
                )
        if episode.get("summary_embedding"):
            self.vec_upsert_episode(
                episode["organization_id"], episode["id"], episode["summary_embedding"]
            )

    def list_episodes(
        self, organization_id: str, *, scope_ids: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        if scope_ids is not None:
            if not scope_ids:
                return []
            placeholders = ", ".join("?" for _ in scope_ids)
            clauses.append(f"scope_id IN ({placeholders})")
            params.extend(scope_ids)
        sql = f"SELECT payload FROM episodes WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [_de(row["payload"]) for row in cur.fetchall()]

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute("SELECT payload FROM episodes WHERE id = ?", (episode_id,))
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def add_episode_sources(self, episode_id: str, sources: list[dict[str, Any]], *, start_ordinal: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor() as cur:
            for offset, source in enumerate(sources):
                cur.execute(
                    """INSERT OR IGNORE INTO episode_sources
                    (episode_id, organization_id, source_id, ordinal, occurred_at, added_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        episode_id,
                        source["organization_id"],
                        source["id"],
                        start_ordinal + offset,
                        source.get("occurred_at") or now,
                        now,
                    ),
                )

    def list_episode_sources(self, episode_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT source_id, ordinal, occurred_at FROM episode_sources "
                "WHERE episode_id = ? ORDER BY ordinal ASC",
                (episode_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def episode_fts_ranks(self, query: str, organization_id: str, scope_ids: list[str]) -> dict[str, float]:
        tokens = [token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) > 2]
        if not tokens or not scope_ids:
            return {}
        match = " OR ".join(tokens)
        placeholders = ", ".join("?" for _ in scope_ids)
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT episode_id, bm25(episodes_fts) AS rank FROM episodes_fts
                WHERE episodes_fts MATCH ? AND organization_id = ? AND scope_id IN ({placeholders})""",
                [match, organization_id, *scope_ids],
            )
            return {row["episode_id"]: -float(row["rank"]) for row in cur.fetchall()}

    # ---- entities / edges ----

    def upsert_entity(self, entity: dict[str, Any]) -> None:
        organization_id = entity["organization_id"]
        aliases: list[tuple[str, str]] = [(str(entity["name"]), "name")]
        aliases.extend((str(value), "alias") for value in entity.get("aliases") or [])
        aliases.append((str(entity["id"]), "id"))
        for key in ("email", "slack_id", "external_id"):
            value = entity.get(key) or (entity.get("attributes") or {}).get(key)
            if value:
                aliases.append((str(value), key))
        alias_rows: set[tuple[str, str, str]] = set()
        for alias, kind in aliases:
            normalized = alias.casefold().strip()
            if not normalized:
                continue
            alias_rows.add((alias, normalized, kind))
            for token in re.findall(r"[\w@.+-]{2,}", normalized):
                alias_rows.add((alias, token, f"{kind}_token"))

        with self.cursor(immediate=True) as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO entities(id, organization_id, entity_type, name, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entity["id"],
                    organization_id,
                    entity["entity_type"] if isinstance(entity["entity_type"], str) else entity["entity_type"].value,
                    entity["name"],
                    _ser(entity),
                ),
            )
            cur.execute(
                "DELETE FROM entity_aliases WHERE organization_id = ? AND entity_id = ?",
                (organization_id, entity["id"]),
            )
            cur.executemany(
                """INSERT INTO entity_aliases
                (organization_id, entity_id, alias, normalized, kind)
                VALUES (?, ?, ?, ?, ?)""",
                [
                    (organization_id, entity["id"], alias, normalized, kind)
                    for alias, normalized, kind in sorted(alias_rows)
                ],
            )

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        return self.get_json("entities", entity_id)

    def list_entities(
        self,
        organization_id: str,
        entity_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        types = list(entity_types or [])
        with self.cursor() as cur:
            if types:
                placeholders = ",".join("?" * len(types))
                cur.execute(
                    "SELECT payload FROM entities WHERE organization_id = ? "
                    f"AND entity_type IN ({placeholders})",
                    (organization_id, *types),
                )
            else:
                cur.execute("SELECT payload FROM entities WHERE organization_id = ?", (organization_id,))
            return [_de(r["payload"]) for r in cur.fetchall()]

    def find_entity_candidates(
        self, organization_id: str, text: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        tokens = [token.lower() for token in re.findall(r"[\w.-]{2,}", text or "")]
        if not tokens:
            return []
        selected = tokens[:12]
        placeholders = ",".join("?" for _ in selected)
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT e.payload, COUNT(*) AS matches
                FROM entity_aliases a
                JOIN entities e ON e.id = a.entity_id
                WHERE a.organization_id = ? AND a.normalized IN ({placeholders})
                GROUP BY e.id
                ORDER BY matches DESC, e.name
                LIMIT ?""",
                (organization_id, *selected, int(limit)),
            )
            return [_de(row["payload"]) for row in cur.fetchall()]

    def get_active_edge(
        self, organization_id: str, edge_type: str, from_id: str, to_id: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM edges WHERE organization_id = ?
                AND edge_type = ? AND from_id = ? AND to_id = ?
                AND valid_until IS NULL LIMIT 1""",
                (organization_id, edge_type, from_id, to_id),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def upsert_edge(self, edge: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO edges
                (id, organization_id, edge_type, from_id, to_id, valid_until, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge["id"],
                    edge["organization_id"],
                    edge["edge_type"],
                    edge["from_id"],
                    edge["to_id"],
                    edge.get("valid_until"),
                    _ser(edge),
                ),
            )

    def list_edges(
        self,
        organization_id: str,
        edge_type: str | None = None,
        from_id: str | None = None,
        to_id: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        if edge_type:
            clauses.append("edge_type = ?")
            params.append(edge_type)
        if from_id:
            clauses.append("from_id = ?")
            params.append(from_id)
        if to_id:
            clauses.append("to_id = ?")
            params.append(to_id)
        sql = f"SELECT payload, valid_until FROM edges WHERE {' AND '.join(clauses)}"
        with self.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        results = []
        for r in rows:
            edge = _de(r["payload"])
            if active_only and edge.get("valid_until"):
                continue
            results.append(edge)
        return results

    def neighbors(self, organization_id: str, node_id: str, edge_types: list[str] | None = None) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?", "valid_until IS NULL", "(from_id = ? OR to_id = ?)"]
        params: list[Any] = [organization_id, node_id, node_id]
        if edge_types:
            placeholders = ",".join("?" for _ in edge_types)
            clauses.append(f"edge_type IN ({placeholders})")
            params.extend(edge_types)
        with self.cursor() as cur:
            cur.execute(
                f"SELECT payload FROM edges WHERE {' AND '.join(clauses)}",
                params,
            )
            return [_de(row["payload"]) for row in cur.fetchall()]

    # ---- memories ----

    def upsert_memory(self, memory: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO memories
                (id, organization_id, memory_type, status, is_latest, scope_id, is_private,
                 content, valid_from, valid_until, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory["id"],
                    memory["organization_id"],
                    memory["memory_type"] if isinstance(memory["memory_type"], str) else memory["memory_type"].value,
                    memory["status"] if isinstance(memory["status"], str) else memory["status"].value,
                    1 if memory.get("is_latest", True) else 0,
                    memory["scope_id"],
                    1 if memory.get("is_private") else 0,
                    memory["content"],
                    memory.get("valid_from"),
                    memory.get("valid_until"),
                    _ser(memory),
                ),
            )

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        return self.get_json("memories", memory_id)

    def list_memories(
        self,
        organization_id: str,
        latest_only: bool = False,
        status: str | None = None,
        scope_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        if latest_only:
            clauses.append("is_latest = 1")
        if status:
            clauses.append("status = ?")
            params.append(status)
        if scope_ids is not None:
            if not scope_ids:
                return []
            placeholders = ",".join("?" for _ in scope_ids)
            clauses.append(f"scope_id IN ({placeholders})")
            params.extend(scope_ids)
        sql = f"SELECT payload FROM memories WHERE {' AND '.join(clauses)}"
        with self.cursor() as cur:
            cur.execute(sql, params)
            rows = [_de(r["payload"]) for r in cur.fetchall()]
        return rows

    def list_current_memories(
        self,
        organization_id: str,
        *,
        entity_ids: Sequence[str] | None = None,
        memory_types: Sequence[str] | None = None,
        scope_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "m.organization_id = ?",
            "m.is_latest = 1",
            "m.status IN ('active', 'confirmed')",
        ]
        params: list[Any] = [organization_id]
        if memory_types:
            placeholders = ",".join("?" for _ in memory_types)
            clauses.append(f"m.memory_type IN ({placeholders})")
            params.extend(memory_types)
        if scope_ids is not None:
            if not scope_ids:
                return []
            placeholders = ",".join("?" for _ in scope_ids)
            clauses.append(f"m.scope_id IN ({placeholders})")
            params.extend(scope_ids)
        if entity_ids:
            placeholders = ",".join("?" for _ in entity_ids)
            clauses.append(
                "EXISTS (SELECT 1 FROM edges e WHERE e.organization_id = m.organization_id "
                "AND e.from_id = m.id AND e.edge_type = 'ABOUT' "
                f"AND e.to_id IN ({placeholders}) AND e.valid_until IS NULL)"
            )
            params.extend(entity_ids)
        with self.cursor() as cur:
            cur.execute(
                f"SELECT m.payload FROM memories m WHERE {' AND '.join(clauses)}",
                params,
            )
            return [_de(row["payload"]) for row in cur.fetchall()]

    def mark_superseded(self, memory_id: str, superseded_at: str) -> None:
        mem = self.get_memory(memory_id)
        if not mem:
            return
        mem["status"] = "superseded"
        mem["is_latest"] = False
        mem["superseded_at"] = superseded_at
        mem["valid_until"] = superseded_at
        self.upsert_memory(mem)

    # ---- search indexes ----

    def _vec_upsert(
        self, table: str, organization_id: str, row_id: str, embedding: list[float]
    ) -> None:
        if not embedding:
            return
        # First write of a given width builds (or rebuilds) the indexes at
        # that width -- see _ensure_vector_tables for why the width is
        # observed rather than configured.
        self._ensure_vector_tables(len(embedding))
        with self.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE id = ? AND organization_id = ?",
                (row_id, organization_id),
            )
            cur.execute(
                f"INSERT INTO {table}(organization_id, id, embedding) VALUES (?, ?, ?)",
                (organization_id, row_id, sqlite_vec.serialize_float32(embedding)),
            )

    def vec_upsert_chunk(self, organization_id: str, chunk_id: str, embedding: list[float]) -> None:
        self._vec_upsert("vec_chunks", organization_id, chunk_id, embedding)

    def vec_upsert_memory(self, organization_id: str, memory_id: str, embedding: list[float]) -> None:
        self._vec_upsert("vec_memories", organization_id, memory_id, embedding)

    def vec_upsert_episode(self, organization_id: str, episode_id: str, embedding: list[float]) -> None:
        self._vec_upsert("vec_episodes", organization_id, episode_id, embedding)

    def vec_upsert_task(self, organization_id: str, task_id: str, embedding: list[float]) -> None:
        self._vec_upsert("vec_tasks", organization_id, task_id, embedding)

    def _vec_search(
        self, table: str, organization_id: str, embedding: list[float], k: int
    ) -> list[tuple[str, float]]:
        if not embedding or k <= 0:
            return []
        target = self.vector_dimensions()
        if target is None or len(embedding) != target:
            # No index yet, or the query vector predates a model change --
            # return nothing rather than raising; callers degrade to their
            # lexical/FTS signal, which is still correct, just less precise.
            return []
        with self.cursor() as cur:
            cur.execute(
                f"SELECT id, distance FROM {table} "
                "WHERE embedding MATCH ? AND k = ? AND organization_id = ? ORDER BY distance",
                (sqlite_vec.serialize_float32(embedding), int(k), organization_id),
            )
            return [(row["id"], float(row["distance"])) for row in cur.fetchall()]

    def vec_search_chunks(
        self, organization_id: str, embedding: list[float], k: int = 200
    ) -> list[tuple[str, float]]:
        """Nearest chunk ids within one organization, by cosine distance
        (0=identical). A derived ANN shortlist, not a permission decision —
        callers must still authorize every id before returning content."""
        return self._vec_search("vec_chunks", organization_id, embedding, k)

    def vec_search_memories(
        self, organization_id: str, embedding: list[float], k: int = 200
    ) -> list[tuple[str, float]]:
        return self._vec_search("vec_memories", organization_id, embedding, k)

    def vec_search_episodes(
        self, organization_id: str, embedding: list[float], k: int = 50
    ) -> list[tuple[str, float]]:
        return self._vec_search("vec_episodes", organization_id, embedding, k)

    def vec_search_tasks(
        self, organization_id: str, embedding: list[float], k: int = 50
    ) -> list[tuple[str, float]]:
        return self._vec_search("vec_tasks", organization_id, embedding, k)

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute("SELECT payload FROM chunks WHERE id = ?", (chunk_id,))
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def upsert_chunk(self, chunk: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO chunks
                (id, organization_id, source_id, scope_id, position, content, model, embedding, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chunk["id"], chunk["organization_id"], chunk["source_id"], chunk["scope_id"],
                 chunk["position"], chunk["content"], chunk["model"], _ser(chunk["embedding"]), _ser(chunk)),
            )
            cur.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk["id"],))
            cur.execute("INSERT INTO chunks_fts(chunk_id, organization_id, scope_id, content) VALUES (?, ?, ?, ?)",
                        (chunk["id"], chunk["organization_id"], chunk["scope_id"], chunk["content"]))
        self.vec_upsert_chunk(chunk["organization_id"], chunk["id"], chunk["embedding"] or [])

    def upsert_memory_embedding(
        self, *, memory_id: str, organization_id: str, scope_id: str, model: str,
        embedding: list[float], content_hash: str,
    ) -> None:
        memory = self.get_memory(memory_id)
        if not memory:
            return
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO memory_embeddings
                (memory_id, organization_id, scope_id, model, embedding, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (memory_id, organization_id, scope_id, model, _ser(embedding), content_hash),
            )
            cur.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            cur.execute("INSERT INTO memories_fts(memory_id, organization_id, scope_id, content) VALUES (?, ?, ?, ?)",
                        (memory_id, organization_id, scope_id, memory.get("content") or ""))
        self.vec_upsert_memory(organization_id, memory_id, embedding or [])

    def memory_embeddings(self, memory_ids: list[str]) -> dict[str, list[float]]:
        if not memory_ids:
            return {}
        placeholders = ", ".join("?" for _ in memory_ids)
        with self.cursor() as cur:
            cur.execute(f"SELECT memory_id, embedding FROM memory_embeddings WHERE memory_id IN ({placeholders})", memory_ids)
            return {row["memory_id"]: _de(row["embedding"]) for row in cur.fetchall()}

    def upsert_judgment_embedding(
        self, *, judgment_id: str, organization_id: str, model: str,
        embedding: list[float],
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO judgment_embeddings
                (judgment_id, organization_id, model, embedding)
                VALUES (?, ?, ?, ?)""",
                (judgment_id, organization_id, model, _ser(embedding)),
            )

    def judgment_embeddings(self, judgment_ids: list[str]) -> dict[str, list[float]]:
        if not judgment_ids:
            return {}
        placeholders = ", ".join("?" for _ in judgment_ids)
        with self.cursor() as cur:
            cur.execute(
                f"SELECT judgment_id, embedding FROM judgment_embeddings "
                f"WHERE judgment_id IN ({placeholders})",
                judgment_ids,
            )
            return {row["judgment_id"]: _de(row["embedding"]) for row in cur.fetchall()}

    def list_chunks(
        self,
        organization_id: str,
        *,
        scope_ids: list[str] | None = None,
        source_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        if scope_ids is not None:
            if not scope_ids:
                return []
            placeholders = ", ".join("?" for _ in scope_ids)
            clauses.append(f"scope_id IN ({placeholders})")
            params.extend(scope_ids)
        if source_ids is not None:
            if not source_ids:
                return []
            placeholders = ", ".join("?" for _ in source_ids)
            clauses.append(f"source_id IN ({placeholders})")
            params.extend(source_ids)
        sql = (
            f"SELECT payload FROM chunks WHERE {' AND '.join(clauses)} "
            "ORDER BY rowid DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [_de(row["payload"]) for row in cur.fetchall()]

    def chunk_fts_ranks(
        self, query: str, organization_id: str, scope_ids: list[str]
    ) -> dict[str, float]:
        tokens = [
            token
            for token in re.findall(r"[a-z0-9_]+", query.lower())
            if len(token) > 2
        ]
        if not tokens or not scope_ids:
            return {}
        match = " OR ".join(tokens)
        placeholders = ", ".join("?" for _ in scope_ids)
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts
                WHERE chunks_fts MATCH ? AND organization_id = ?
                AND scope_id IN ({placeholders})""",
                [match, organization_id, *scope_ids],
            )
            return {
                row["chunk_id"]: -float(row["rank"]) for row in cur.fetchall()
            }

    def has_chunks_for_source(self, source_id: str) -> bool:
        with self.cursor() as cur:
            cur.execute("SELECT 1 FROM chunks WHERE source_id = ? LIMIT 1", (source_id,))
            return cur.fetchone() is not None

    def has_memory_embedding(self, memory_id: str, content: str) -> bool:
        import hashlib

        with self.cursor() as cur:
            cur.execute("SELECT content_hash FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
            row = cur.fetchone()
        return bool(row and row["content_hash"] == hashlib.sha256(content.encode()).hexdigest())

    def memory_fts_ranks(self, query: str, memory_ids: list[str]) -> dict[str, float]:
        """Return native FTS5 BM25 ranks only for already-authorized memory ids."""
        tokens = [token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) > 2]
        if not tokens or not memory_ids:
            return {}
        match = " OR ".join(tokens)
        placeholders = ", ".join("?" for _ in memory_ids)
        with self.cursor() as cur:
            cur.execute(
                f"SELECT memory_id, bm25(memories_fts) AS rank FROM memories_fts "
                f"WHERE memories_fts MATCH ? AND memory_id IN ({placeholders})",
                [match, *memory_ids],
            )
            # FTS5 BM25 returns smaller (normally negative) values for better matches.
            return {row["memory_id"]: -float(row["rank"]) for row in cur.fetchall()}

    # ---- scopes / memberships ----

    def upsert_scope(self, scope: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO scopes(id, organization_id, payload) VALUES (?, ?, ?)",
                (scope["id"], scope["organization_id"], _ser(scope)),
            )

    def get_scope(self, scope_id: str) -> dict[str, Any] | None:
        return self.get_json("scopes", scope_id)

    def list_scopes(self, organization_id: str) -> list[dict[str, Any]]:
        return self.list_by_org("scopes", organization_id)

    def add_membership(self, organization_id: str, principal_id: str, scope_id: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO principal_memberships(organization_id, principal_id, scope_id)
                VALUES (?, ?, ?)
                """,
                (organization_id, principal_id, scope_id),
            )

    def memberships_for(self, organization_id: str, principal_id: str) -> list[str]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT scope_id FROM principal_memberships
                WHERE organization_id = ? AND principal_id = ?
                """,
                (organization_id, principal_id),
            )
            return [r["scope_id"] for r in cur.fetchall()]

    def scope_member_ids(self, organization_id: str, scope_id: str) -> list[str]:
        """The inverse of memberships_for: who belongs to this scope.

        Needed when a scope has to be narrowed after the fact -- correcting a
        mislabelled public room to private must keep everyone who already
        spoke in it, or the repair locks out the room's own participants.
        """
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT principal_id FROM principal_memberships
                WHERE organization_id = ? AND scope_id = ?
                """,
                (organization_id, scope_id),
            )
            return [r["principal_id"] for r in cur.fetchall()]

    # ---- skills / signals / audit ----

    def upsert_skill(self, skill: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO skills(id, organization_id, name, version, status, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    skill["id"],
                    skill["organization_id"],
                    skill["name"],
                    skill.get("version", 1),
                    skill["status"] if isinstance(skill["status"], str) else skill["status"].value,
                    _ser(skill),
                ),
            )

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return self.get_json("skills", skill_id)

    def list_skills(self, organization_id: str) -> list[dict[str, Any]]:
        return self.list_by_org("skills", organization_id)

    def upsert_skill_version(self, skill: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO skill_versions
                (skill_id, version, organization_id, status, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (skill["id"], int(skill.get("version") or 1), skill["organization_id"], skill["status"],
                 _ser(skill), skill.get("created_at") or datetime.now(timezone.utc).isoformat()),
            )

    def get_skill_version(self, skill_id: str, version: int) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute("SELECT payload FROM skill_versions WHERE skill_id = ? AND version = ?", (skill_id, version))
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def list_skill_versions(self, skill_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute("SELECT payload FROM skill_versions WHERE skill_id = ? ORDER BY version", (skill_id,))
            return [_de(row["payload"]) for row in cur.fetchall()]

    def upsert_signal(self, signal: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO signals(id, organization_id, signal_type, payload) VALUES (?, ?, ?, ?)",
                (
                    signal["id"],
                    signal["organization_id"],
                    signal["signal_type"] if isinstance(signal["signal_type"], str) else signal["signal_type"].value,
                    _ser(signal),
                ),
            )

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        return self.get_json("signals", signal_id)

    def list_signals(self, organization_id: str) -> list[dict[str, Any]]:
        return self.list_by_org("signals", organization_id)

    def append_audit(self, event: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_events(id, organization_id, event_type, created_at, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["organization_id"],
                    event["event_type"],
                    event.get("created_at") or datetime.now(timezone.utc).isoformat(),
                    _ser(event),
                ),
            )

    def list_audit(self, organization_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            if event_type:
                cur.execute(
                    "SELECT payload FROM audit_events WHERE organization_id = ? AND event_type = ? ORDER BY created_at",
                    (organization_id, event_type),
                )
            else:
                cur.execute(
                    "SELECT payload FROM audit_events WHERE organization_id = ? ORDER BY created_at",
                    (organization_id,),
                )
            return [_de(r["payload"]) for r in cur.fetchall()]

    def list_audit_since(
        self,
        organization_id: str,
        *,
        after: str | None,
        event_types: Sequence[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return only new heartbeat-relevant events in durable order."""
        kinds = list(dict.fromkeys(event_types))
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        clauses = ["organization_id = ?", f"event_type IN ({placeholders})"]
        params: list[Any] = [organization_id, *kinds]
        if after:
            clauses.append("created_at > ?")
            params.append(after)
        params.append(max(1, int(limit)))
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT payload FROM audit_events
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, id LIMIT ?""",
                params,
            )
            return [_de(row["payload"]) for row in cur.fetchall()]

    def save_authz(self, decision: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO authz_decisions(id, organization_id, payload) VALUES (?, ?, ?)",
                (decision["id"], decision["organization_id"], _ser(decision)),
            )

    def get_authz(self, decision_id: str) -> dict[str, Any] | None:
        return self.get_json("authz_decisions", decision_id)

    def put_organization(self, org: dict[str, Any]) -> None:
        self.put_json("organizations", org["id"], org["id"], org)

    def get_organization(self, org_id: str) -> dict[str, Any] | None:
        return self.get_json("organizations", org_id)

    def list_organizations(self) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute("SELECT payload FROM organizations")
            return [_de(row["payload"]) for row in cur.fetchall()]

    # ---- ingestion jobs, events, idempotency ----

    def upsert_job(self, job: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO processing_jobs
                (id, organization_id, kind, status, source_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (job["id"], job["organization_id"], job["kind"], job["status"], job.get("source_id"),
                 _ser(job), job["created_at"], job["updated_at"]),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.get_json("processing_jobs", job_id)

    def list_jobs(self, organization_id: str) -> list[dict[str, Any]]:
        return self.list_by_org("processing_jobs", organization_id)

    def upsert_received_event(self, event: dict[str, Any]) -> bool:
        with self.cursor() as cur:
            try:
                cur.execute(
                    """INSERT INTO received_events(id, organization_id, event_type, source, payload, received_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (event["id"], event["organization_id"], event["type"], event["source"],
                     _ser(event), event["received_at"]),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def idempotency_response(self, organization_id: str, operation: str, key: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                "SELECT response FROM idempotency_keys WHERE organization_id = ? AND operation = ? AND key = ?",
                (organization_id, operation, key),
            )
            row = cur.fetchone()
            return _de(row["response"]) if row else None

    def save_idempotency_response(
        self, organization_id: str, operation: str, key: str, response: dict[str, Any]
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO idempotency_keys(organization_id, operation, key, response, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (organization_id, operation, key, _ser(response), datetime.now(timezone.utc).isoformat()),
            )

    # ---- profile projections ----

    def upsert_profile(self, profile: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO profiles(id, organization_id, profile_type, entity_id, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (profile["id"], profile["organization_id"], profile["profile_type"], profile["entity_id"],
                 _ser(profile), profile["updated_at"]),
            )

    def get_profile(self, organization_id: str, profile_type: str, entity_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                "SELECT payload FROM profiles WHERE organization_id = ? AND profile_type = ? AND entity_id = ?",
                (organization_id, profile_type, entity_id),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    # ---- durable agent harness ----

    _HARNESS_TABLES = {
        "task_turns",
        "harness_tool_calls",
        "approvals",
        "conversation_states",
        "attention_evaluations",
        "task_interventions",
        "agent_feedback",
        "deliveries",
        "slack_installations",
        "channel_policies",
        "identity_mappings",
        "connector_accounts",
        "tool_policies",
        "checkpoints",
        "episodes",
        "automations",
        "mcp_servers",
        "roles",
        "skill_packages",
        "capabilities",
        "task_skill_refs",
        "task_capability_refs",
        "workspaces",
        "plans",
        "task_scratchpads",
        "automation_watermarks",
        "task_pattern_signals",
    }

    def _checked_harness_table(self, table: str) -> str:
        if table not in self._HARNESS_TABLES:
            raise ValueError(f"unsupported harness table: {table}")
        return table

    def get_harness_record(self, table: str, id_: str) -> dict[str, Any] | None:
        table = self._checked_harness_table(table)
        with self.cursor() as cur:
            cur.execute(f"SELECT payload FROM {table} WHERE id = ?", (id_,))
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def list_harness_records(
        self,
        table: str,
        organization_id: str,
        *,
        where: dict[str, Any] | None = None,
        order_by: str = "created_at",
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        table = self._checked_harness_table(table)
        allowed_columns = {
            "id",
            "organization_id",
            "state",
            "status",
            "session_id",
            "task_id",
            "event_id",
            "conversation_key",
            "workspace_id",
            "channel_id",
            "source",
            "tool_name",
            "trigger_kind",
            "kind",
        }
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        for column, value in (where or {}).items():
            if column not in allowed_columns:
                raise ValueError(f"unsupported harness filter: {column}")
            clauses.append(f"{column} = ?")
            params.append(value)
        safe_order = (
            order_by
            if order_by in {"created_at", "updated_at", "sequence", "turn_number"}
            else "created_at"
        )
        sql = (
            f"SELECT payload FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY {safe_order} {'DESC' if descending else 'ASC'}"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [_de(row["payload"]) for row in cur.fetchall()]

    # ---- Session / Task architecture ----
    #
    # These tables carry their own ownership/lease/revision columns
    # (manager_state/manager_owner/manager_lease_expires_at/manager_revision
    # on sessions; state/execution_owner/execution_lease_expires_at/
    # wake_revision on tasks) as the SOURCE OF TRUTH for concurrency control.
    # `update_session`/`update_task` touch only `payload` + `updated_at` --
    # never the ownership columns -- so a caller holding a stale in-memory
    # copy can never clobber a concurrently-changed lease by saving the
    # whole record back. Ownership only ever changes through
    # claim_*/release_*/renew_*_lease, each a single atomic UPDATE guarded
    # by an immediate transaction, same pattern as lease_harness_jobs.

    @staticmethod
    def _session_row(row: Any) -> dict[str, Any]:
        record = _de(row["payload"])
        record.update(
            {
                "manager_state": row["manager_state"],
                "manager_owner": row["manager_owner"],
                "manager_lease_expires_at": row["manager_lease_expires_at"],
                "manager_revision": row["manager_revision"],
                "processed_revision": row["processed_revision"],
            }
        )
        return record

    @staticmethod
    def _task_row(row: Any) -> dict[str, Any]:
        record = _de(row["payload"])
        record.update(
            {
                "state": row["state"],
                "execution_owner": row["execution_owner"],
                "execution_lease_expires_at": row["execution_lease_expires_at"],
                "wake_revision": row["wake_revision"],
                "processed_revision": row["processed_revision"],
            }
        )
        return record

    _SESSION_COLUMNS = (
        "payload, manager_state, manager_owner, manager_lease_expires_at, "
        "manager_revision, processed_revision"
    )
    _TASK_COLUMNS = (
        "payload, state, execution_owner, execution_lease_expires_at, "
        "wake_revision, processed_revision"
    )

    def create_session(self, session: dict[str, Any]) -> dict[str, Any]:
        session = dict(session)
        session.setdefault("manager_state", "idle")
        session.setdefault("manager_owner", None)
        session.setdefault("manager_lease_expires_at", None)
        session.setdefault("manager_revision", 0)
        session.setdefault("processed_revision", 0)
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO sessions
                (id, organization_id, conversation_key, manager_state, manager_owner,
                 manager_lease_expires_at, manager_revision, processed_revision,
                 payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session["id"],
                    session["organization_id"],
                    session["conversation_key"],
                    session["manager_state"],
                    session["manager_owner"],
                    session["manager_lease_expires_at"],
                    int(session["manager_revision"]),
                    int(session["processed_revision"]),
                    _ser(session),
                    session["created_at"],
                    session["updated_at"],
                ),
            )
        return session

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {self._SESSION_COLUMNS} FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            return self._session_row(row) if row else None

    def list_sessions(
        self,
        organization_id: str,
        *,
        manager_state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        if manager_state:
            clauses.append("manager_state = ?")
            params.append(manager_state)
        params.append(int(limit))
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT {self._SESSION_COLUMNS} FROM sessions
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC LIMIT ?""",
                params,
            )
            return [self._session_row(row) for row in cur.fetchall()]

    def find_session_by_conversation(
        self, organization_id: str, conversation_key: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT {self._SESSION_COLUMNS} FROM sessions
                WHERE organization_id = ? AND conversation_key = ?""",
                (organization_id, conversation_key),
            )
            row = cur.fetchone()
            return self._session_row(row) if row else None

    def update_session(self, session: dict[str, Any]) -> None:
        """Persist ordinary session fields (summary, participants, metadata).
        Never touches manager_state/owner/lease/revision -- those are
        exclusively owned by claim_session_manager/release_session_manager/
        bump_session_revision."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET payload = ?, updated_at = ? WHERE id = ?",
                (_ser(session), session["updated_at"], session["id"]),
            )

    def claim_session_manager(
        self, session_id: str, *, owner: str, lease_seconds: int
    ) -> bool:
        """Atomic compare-and-swap: succeeds only if the session is
        `queued` (fresh wake) or `running` with an expired lease (crashed
        executor) and unowned or owned by a lease that's expired."""
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.cursor(immediate=True) as cur:
            cur.execute(
                """UPDATE sessions
                SET manager_state = 'running', manager_owner = ?,
                    manager_lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                  AND manager_state IN ('queued', 'running')
                  AND (manager_owner IS NULL OR manager_lease_expires_at < ?)""",
                (owner, expiry, now_s, session_id, now_s),
            )
            return cur.rowcount > 0

    def renew_session_manager_lease(
        self, session_id: str, *, owner: str, lease_seconds: int
    ) -> bool:
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """UPDATE sessions SET manager_lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND manager_owner = ?""",
                (expiry, now.isoformat(), session_id, owner),
            )
            return cur.rowcount > 0

    def release_session_manager(
        self, session_id: str, *, owner: str, idle: bool
    ) -> bool:
        """Guarded by `manager_owner = ?` so a stale/expired owner can never
        release (and thus corrupt) a session someone else has since
        claimed -- the old-owner-cannot-commit-after-losing-lease
        invariant."""
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """UPDATE sessions
                SET manager_state = CASE
                        WHEN ? = 0 THEN 'queued'
                        WHEN processed_revision >= manager_revision THEN 'idle'
                        ELSE 'queued'
                    END,
                    manager_owner = NULL,
                    manager_lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND manager_owner = ?""",
                (1 if idle else 0, now, session_id, owner),
            )
            return cur.rowcount > 0

    def bump_session_revision(self, session_id: str) -> int:
        """Increment manager_revision (new input needs attention) and, if
        the session is currently idle, flip it to queued so a dispatcher
        picks it up -- a currently-running session just sees the higher
        revision at its next iteration boundary instead."""
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor(immediate=True) as cur:
            cur.execute(
                """UPDATE sessions
                SET manager_revision = manager_revision + 1,
                    manager_state = CASE WHEN manager_state = 'idle' THEN 'queued' ELSE manager_state END,
                    updated_at = ?
                WHERE id = ?""",
                (now, session_id),
            )
            cur.execute(
                "SELECT manager_revision FROM sessions WHERE id = ?", (session_id,)
            )
            row = cur.fetchone()
            return int(row["manager_revision"]) if row else 0

    def mark_session_revision_processed(self, session_id: str, revision: int) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET processed_revision = ? WHERE id = ? AND processed_revision < ?",
                (revision, session_id, revision),
            )

    def append_session_message(self, message: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO session_messages
                (id, organization_id, session_id, task_id, role, content,
                 tool_call_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message["id"],
                    message["organization_id"],
                    message["session_id"],
                    message.get("task_id"),
                    message["role"],
                    message.get("content") or "",
                    message.get("tool_call_id"),
                    _ser(message),
                    message["created_at"],
                ),
            )

    def list_session_messages(
        self,
        session_id: str,
        *,
        limit: int = 200,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            if task_id is None:
                cur.execute(
                    """SELECT payload FROM session_messages WHERE session_id = ?
                    ORDER BY created_at DESC LIMIT ?""",
                    (session_id, limit),
                )
            else:
                cur.execute(
                    """SELECT payload FROM session_messages
                    WHERE session_id = ? AND task_id = ?
                    ORDER BY created_at DESC LIMIT ?""",
                    (session_id, task_id, limit),
                )
            rows = [_de(row["payload"]) for row in cur.fetchall()]
        return list(reversed(rows))

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task = dict(task)
        task.setdefault("execution_owner", None)
        task.setdefault("execution_lease_expires_at", None)
        task.setdefault("wake_revision", 0)
        task.setdefault("processed_revision", 0)
        task.setdefault("completed_at", None)
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO tasks
                (id, organization_id, session_id, parent_task_id, root_task_id,
                 state, worker_type, execution_owner, execution_lease_expires_at,
                 wake_revision, processed_revision, payload, created_at,
                 updated_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task["id"],
                    task["organization_id"],
                    task["session_id"],
                    task.get("parent_task_id"),
                    task["root_task_id"],
                    task["state"],
                    task["worker_type"],
                    task.get("execution_owner"),
                    task.get("execution_lease_expires_at"),
                    int(task.get("wake_revision", 0)),
                    int(task.get("processed_revision", 0)),
                    _ser(task),
                    task["created_at"],
                    task["updated_at"],
                    task.get("completed_at"),
                ),
            )
            if task.get("parent_task_id") is None:
                cur.execute(
                    "INSERT INTO tasks_fts(task_id, organization_id, content) VALUES (?, ?, ?)",
                    (task["id"], task["organization_id"], str(task.get("goal") or "")),
                )
        return task

    def index_root_task(self, task: dict[str, Any]) -> None:
        if task.get("parent_task_id") is not None:
            return
        content = "\n".join(
            str(value or "")
            for value in (task.get("goal"), task.get("completion_summary"), task.get("result"))
        )
        with self.cursor() as cur:
            cur.execute("DELETE FROM tasks_fts WHERE task_id = ?", (task["id"],))
            cur.execute(
                "INSERT INTO tasks_fts(task_id, organization_id, content) VALUES (?, ?, ?)",
                (task["id"], task["organization_id"], content),
            )

    def upsert_task_embedding(
        self,
        *,
        task_id: str,
        organization_id: str,
        model: str,
        embedding: list[float],
        content_hash: str,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO task_embeddings
                (task_id, organization_id, model, embedding, content_hash)
                VALUES (?, ?, ?, ?, ?)""",
                (task_id, organization_id, model, _ser(embedding), content_hash),
            )
        self.vec_upsert_task(organization_id, task_id, embedding)

    def search_root_tasks(
        self, organization_id: str, *, query: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        with self.cursor() as cur:
            if query:
                tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)
                if not tokens:
                    return []
                expression = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
                cur.execute(
                    """SELECT t.payload, t.state, t.execution_owner,
                               t.execution_lease_expires_at, t.wake_revision,
                               t.processed_revision
                    FROM tasks_fts f JOIN tasks t ON t.id = f.task_id
                    WHERE f.organization_id = ? AND tasks_fts MATCH ?
                      AND t.parent_task_id IS NULL
                    ORDER BY bm25(tasks_fts), t.updated_at DESC LIMIT ?""",
                    (organization_id, expression, int(limit)),
                )
            else:
                cur.execute(
                    f"""SELECT {self._TASK_COLUMNS} FROM tasks
                    WHERE organization_id = ? AND parent_task_id IS NULL
                    ORDER BY updated_at DESC LIMIT ?""",
                    (organization_id, int(limit)),
                )
            return [self._task_row(row) for row in cur.fetchall()]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {self._TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
            )
            row = cur.fetchone()
            return self._task_row(row) if row else None

    def list_tasks(
        self,
        organization_id: str,
        *,
        session_id: str | None = None,
        parent_task_id: str | None = None,
        root_task_id: str | None = None,
        state: str | None = None,
        worker_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        for column, value in (
            ("session_id", session_id),
            ("parent_task_id", parent_task_id),
            ("root_task_id", root_task_id),
            ("state", state),
            ("worker_type", worker_type),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = (
            f"SELECT {self._TASK_COLUMNS} FROM tasks WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [self._task_row(row) for row in cur.fetchall()]

    def list_expired_running_tasks(
        self, organization_id: str, *, now: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT {self._TASK_COLUMNS} FROM tasks
                WHERE organization_id = ? AND state = 'running'
                  AND execution_lease_expires_at IS NOT NULL
                  AND execution_lease_expires_at < ?
                ORDER BY execution_lease_expires_at LIMIT ?""",
                (organization_id, now, max(1, int(limit))),
            )
            return [self._task_row(row) for row in cur.fetchall()]

    def list_parents_waiting_for_children(
        self, organization_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT {self._TASK_COLUMNS} FROM tasks
                WHERE organization_id = ? AND state = 'waiting'
                  AND parent_task_id IS NULL
                  AND json_extract(payload, '$.waiting_for.type') = 'child_task'
                ORDER BY updated_at LIMIT ?""",
                (organization_id, max(1, int(limit))),
            )
            return [self._task_row(row) for row in cur.fetchall()]

    def list_completed_tasks_missing_delivery(
        self, organization_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT {self._TASK_COLUMNS} FROM tasks t
                WHERE t.organization_id = ? AND t.state = 'completed'
                  AND t.parent_task_id IS NULL
                  AND COALESCE(json_extract(t.payload, '$.final_answer'), '') <> ''
                  AND NOT EXISTS (
                    SELECT 1 FROM deliveries d
                    WHERE d.organization_id = t.organization_id
                      AND d.idempotency_key = 'slack.final:' || t.id
                  )
                ORDER BY t.completed_at LIMIT ?""",
                (organization_id, max(1, int(limit))),
            )
            return [self._task_row(row) for row in cur.fetchall()]

    def update_task(self, task: dict[str, Any]) -> None:
        """Persist ordinary task fields (result, error, prompt revisions,
        iteration/usage counters, metadata). Never touches
        state/execution_owner/execution_lease_expires_at/wake_revision --
        those are exclusively owned by claim_task/release_task/
        renew_task_lease/bump_task_revision."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET payload = ?, updated_at = ? WHERE id = ?",
                (_ser(task), task["updated_at"], task["id"]),
            )

    def update_task_owned(self, task: dict[str, Any], *, owner: str) -> bool:
        """Persist executor-owned payload only while its lease is still owned.

        This closes the stale-executor race left by update_task(): once another
        executor has reclaimed the Task, the old process cannot overwrite its
        counters, prompt, result, or metadata with an obsolete snapshot.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """UPDATE tasks SET payload = ?, updated_at = ?
                WHERE id = ? AND execution_owner = ?
                  AND execution_lease_expires_at >= ?""",
                (_ser(task), task["updated_at"], task["id"], owner, now),
            )
            return cur.rowcount > 0

    def update_unclaimed_task(self, task: dict[str, Any]) -> bool:
        """Update a newly-created Task before its first executor claim.

        Workspace/capability metadata is attached between ``create_task``
        and enqueueing execution.  Keep that legitimate initialization
        atomic without reopening the stale-executor hole: once any executor
        claims the Task (or its state changes), this compare-and-swap fails.
        """
        with self.cursor() as cur:
            cur.execute(
                """UPDATE tasks SET payload = ?, updated_at = ?
                WHERE id = ? AND state = 'queued' AND execution_owner IS NULL""",
                (_ser(task), task["updated_at"], task["id"]),
            )
            return cur.rowcount > 0

    def claim_task(self, task_id: str, *, owner: str, lease_seconds: int) -> bool:
        # Claimable from any non-terminal state, not just queued/running --
        # a Task resuming from the generic waiting state back into RUNNING
        # must be re-claimable exactly
        # like a fresh queued Task. Only completed/failed/cancelled (and
        # whatever's currently held under a live lease) are ineligible.
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.cursor(immediate=True) as cur:
            cur.execute(
                """UPDATE tasks
                SET state = 'running', execution_owner = ?,
                    execution_lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled')
                  AND (execution_owner IS NULL OR execution_lease_expires_at < ?)""",
                (owner, expiry, now_s, task_id, now_s),
            )
            return cur.rowcount > 0

    def renew_task_lease(self, task_id: str, *, owner: str, lease_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """UPDATE tasks SET execution_lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND execution_owner = ?
                  AND execution_lease_expires_at >= ?""",
                (expiry, now_s, task_id, owner, now_s),
            )
            return cur.rowcount > 0

    def release_task(
        self,
        task_id: str,
        *,
        owner: str,
        next_state: str,
        task: dict[str, Any] | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        terminal = next_state in ("completed", "failed", "cancelled")
        with self.cursor() as cur:
            if terminal:
                cur.execute(
                    """UPDATE tasks SET state = ?, execution_owner = NULL,
                    execution_lease_expires_at = NULL, updated_at = ?, completed_at = ?,
                    payload = COALESCE(?, payload)
                    WHERE id = ? AND execution_owner = ?
                      AND execution_lease_expires_at >= ?""",
                    (next_state, now, now, _ser(task) if task else None, task_id, owner, now),
                )
            else:
                cur.execute(
                    """UPDATE tasks SET state = ?, execution_owner = NULL,
                    execution_lease_expires_at = NULL, updated_at = ?,
                    payload = COALESCE(?, payload)
                    WHERE id = ? AND execution_owner = ?
                      AND execution_lease_expires_at >= ?""",
                    (next_state, now, _ser(task) if task else None, task_id, owner, now),
                )
            return cur.rowcount > 0

    def cancel_task(self, task_id: str) -> bool:
        """Unconditional cancel -- unlike release_task, does not require
        matching the current execution_owner. Cancellation always wins,
        whether the task is queued (never claimed), actively claimed by a
        live executor, or paused waiting on something: an active executor's
        own _loop() detects this at its next iteration-top re-fetch
        (`persisted["state"] == CANCELLED`) and stops itself; nothing here
        has to know who currently holds the lease, or even whether anyone
        does. Only touches ownership/state columns -- callers wanting a
        reason recorded in the payload (error/events) do that separately
        via update_task, the same split release_task/update_task already
        use elsewhere."""
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """UPDATE tasks SET state = 'cancelled', execution_owner = NULL,
                execution_lease_expires_at = NULL, updated_at = ?, completed_at = ?
                WHERE id = ? AND state NOT IN ('completed', 'failed', 'cancelled')""",
                (now, now, task_id),
            )
            return cur.rowcount > 0

    def retry_task(self, task_id: str) -> bool:
        """Requeue a terminal Task for an explicit operator retry."""
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor(immediate=True) as cur:
            cur.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return False
            task = _de(row["payload"])
            if task.get("state") not in {"failed", "cancelled"}:
                return False
            task["state"] = "queued"
            task["error"] = None
            task["updated_at"] = now
            task.setdefault("events", []).append(
                {"type": "task.retried", "at": now}
            )
            cur.execute(
                """UPDATE tasks SET state = 'queued', execution_owner = NULL,
                execution_lease_expires_at = NULL,
                payload = ?, updated_at = ?, completed_at = NULL
                WHERE id = ? AND state IN ('failed', 'cancelled')""",
                (_ser(task), now, task_id),
            )
            return cur.rowcount > 0

    def requeue_expired_task(self, task_id: str, *, now: str | None = None) -> bool:
        now = now or datetime.now(timezone.utc).isoformat()
        with self.cursor(immediate=True) as cur:
            cur.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return False
            task = _de(row["payload"])
            task["state"] = "queued"
            task["updated_at"] = now
            task.setdefault("events", []).append({"type": "task.stale_requeued", "at": now})
            cur.execute(
                """UPDATE tasks SET state = 'queued', execution_owner = NULL,
                   execution_lease_expires_at = NULL, payload = ?, updated_at = ?
                   WHERE id = ? AND state = 'running'
                     AND execution_lease_expires_at < ?""",
                (_ser(task), now, task_id, now),
            )
            return cur.rowcount > 0

    def bump_task_revision(self, task_id: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor(immediate=True) as cur:
            cur.execute(
                "UPDATE tasks SET wake_revision = wake_revision + 1, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            cur.execute("SELECT wake_revision FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return int(row["wake_revision"]) if row else 0

    def mark_task_revision_processed(self, task_id: str, revision: int) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET processed_revision = ? WHERE id = ? AND processed_revision < ?",
                (revision, task_id, revision),
            )

    def create_task_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO task_attempts
                (id, organization_id, task_id, attempt_number, status, payload,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt["id"],
                    attempt["organization_id"],
                    attempt["task_id"],
                    int(attempt["attempt_number"]),
                    attempt["status"],
                    _ser(attempt),
                    attempt["created_at"],
                    attempt["updated_at"],
                ),
            )
        return attempt

    def update_task_attempt(self, attempt: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE task_attempts SET status = ?, payload = ?, updated_at = ? WHERE id = ?",
                (attempt["status"], _ser(attempt), attempt["updated_at"], attempt["id"]),
            )

    def list_task_attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM task_attempts WHERE task_id = ?
                ORDER BY attempt_number""",
                (task_id,),
            )
            return [_de(row["payload"]) for row in cur.fetchall()]

    def push_task_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO task_signals
                (id, organization_id, task_id, kind, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    signal["id"],
                    signal["organization_id"],
                    signal["task_id"],
                    signal["kind"],
                    signal.get("message"),
                    signal["created_at"],
                ),
            )
        return signal

    def pop_task_signal(self, task_id: str) -> dict[str, Any] | None:
        with self.cursor(immediate=True) as cur:
            cur.execute(
                """SELECT id, kind, message, created_at FROM task_signals
                WHERE task_id = ? ORDER BY created_at ASC LIMIT 1""",
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("DELETE FROM task_signals WHERE id = ?", (row["id"],))
            return {
                "id": row["id"],
                "kind": row["kind"],
                "message": row["message"],
                "created_at": row["created_at"],
            }

    def find_root_task_for_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the Session's own active root Task, if any."""
        terminal = ("completed", "failed", "cancelled")
        with self.cursor() as cur:
            cur.execute(
                f"""SELECT {self._TASK_COLUMNS} FROM tasks
                WHERE session_id = ? AND parent_task_id IS NULL
                  AND state NOT IN (?, ?, ?)
                ORDER BY
                    CASE state
                        WHEN 'queued' THEN 0
                        WHEN 'running' THEN 1
                        ELSE 2
                    END,
                    updated_at DESC
                LIMIT 1""",
                (session_id, *terminal),
            )
            row = cur.fetchone()
            return self._task_row(row) if row else None

    def append_task_turn(self, turn: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO task_turns
                (id, organization_id, task_id, turn_number, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    turn["id"],
                    turn["organization_id"],
                    turn["task_id"],
                    turn["turn_number"],
                    _ser(turn),
                    turn["created_at"],
                ),
            )

    def upsert_harness_tool_call(self, call: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO harness_tool_calls
                (id, organization_id, task_id, tool_name, status, idempotency_key,
                 payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call["id"],
                    call["organization_id"],
                    call["task_id"],
                    call["tool_name"],
                    call["status"],
                    call["idempotency_key"],
                    _ser(call),
                    call["created_at"],
                    call["updated_at"],
                ),
            )

    def tool_call_by_idempotency(
        self, organization_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM harness_tool_calls
                WHERE organization_id = ? AND idempotency_key = ?""",
                (organization_id, idempotency_key),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def upsert_approval(self, approval: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO approvals
                (id, organization_id, task_id, tool_call_id, status, payload,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval["id"],
                    approval["organization_id"],
                    approval["task_id"],
                    approval["tool_call_id"],
                    approval["status"],
                    _ser(approval),
                    approval["created_at"],
                    approval["updated_at"],
                ),
            )

    def upsert_conversation_state(self, state: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO conversation_states
                (id, organization_id, conversation_key, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    state["id"],
                    state["organization_id"],
                    state["conversation_key"],
                    _ser(state),
                    state["updated_at"],
                ),
            )

    def conversation_state(
        self, organization_id: str, conversation_key: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM conversation_states
                WHERE organization_id = ? AND conversation_key = ?""",
                (organization_id, conversation_key),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def insert_attention_evaluation(self, evaluation: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO attention_evaluations
                (id, organization_id, event_id, decision, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    evaluation["id"],
                    evaluation["organization_id"],
                    evaluation["event_id"],
                    evaluation["decision"],
                    _ser(evaluation),
                    evaluation["created_at"],
                ),
            )

    def insert_intervention(self, intervention: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO task_interventions
                (id, organization_id, conversation_key, task_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    intervention["id"],
                    intervention["organization_id"],
                    intervention["conversation_key"],
                    intervention["task_id"],
                    _ser(intervention),
                    intervention["created_at"],
                ),
            )

    def recent_intervention(
        self,
        organization_id: str,
        conversation_key: str,
        *,
        since: str,
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM task_interventions
                WHERE organization_id = ? AND conversation_key = ? AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1""",
                (organization_id, conversation_key, since),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def recent_slack_messages(
        self,
        organization_id: str,
        *,
        workspace_id: str,
        channel_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return one Slack conversation timeline, including Toda replies.

        Reads the single append-only conversation log. This used to
        RECONSTRUCT the timeline by unioning raw `received_events` with
        `deliveries` joined to `tasks` -- see the conversation_messages
        table comment for the two production bugs that design caused.
        """
        with self.cursor() as cur:
            cur.execute(
                """SELECT at, message_id, author, text, role
                FROM conversation_messages
                WHERE organization_id = ? AND workspace_id = ? AND channel_id = ?
                  AND TRIM(text) <> ''
                ORDER BY at DESC, rowid DESC LIMIT ?""",
                (organization_id, workspace_id, channel_id, max(1, int(limit))),
            )
            timeline = [dict(row) for row in cur.fetchall()]
        timeline.reverse()
        return timeline[-max(1, int(limit)) :]

    def append_conversation_message(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        channel_id: str,
        role: str,
        text: str,
        author: str | None = None,
        message_id: str | None = None,
        at: str | None = None,
    ) -> None:
        """Append one message to a conversation's ordered log.

        Idempotent on message_id so a redelivered Slack event or a retried
        send never doubles up in the timeline. Never raises: a failure to
        record history must not break the actual send or the ingest that
        triggered it.
        """
        if not workspace_id or not channel_id or not (text or "").strip():
            return
        try:
            with self.cursor() as cur:
                cur.execute(
                    """INSERT OR IGNORE INTO conversation_messages
                    (id, organization_id, workspace_id, channel_id, message_id,
                     role, author, text, at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"cmsg_{uuid4().hex[:16]}",
                        organization_id,
                        workspace_id,
                        channel_id,
                        message_id,
                        role,
                        author,
                        text,
                        at or datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except sqlite3.Error:
            log.warning(
                "Could not record conversation message for %s/%s",
                workspace_id,
                channel_id,
                exc_info=True,
            )

    def reserve_delivery(
        self, delivery: dict[str, Any], *, reclaim_stale_before: str | None = None
    ) -> dict[str, Any] | None:
        """Atomically claim a delivery's idempotency key BEFORE the actual
        outbound send happens -- closes the gap a read-then-send check
        leaves open (confirmed real: Slack's chat.postMessage does not
        deduplicate by client_msg_id despite that having been assumed
        here, so any double-invocation of a send -- a job retried after a
        transient failure between a successful post and recording it, or
        any other race -- previously meant two real Slack messages with
        only one delivery row to show for it, since the write-after-send
        step upserts by the same UNIQUE(organization_id, idempotency_key)
        and silently collapses to one row).

        Relies on that same UNIQUE constraint the way enqueue_harness_job
        already does: an INSERT either wins outright, or fails with
        IntegrityError because a row for this key already exists -- in
        which case the caller must not send again. Returns None when this
        call won the reservation (safe to proceed with the real send);
        returns the EXISTING row otherwise."""
        with self.cursor() as cur:
            try:
                cur.execute(
                    """INSERT INTO deliveries
                    (id, organization_id, task_id, status, idempotency_key, payload,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        delivery["id"],
                        delivery["organization_id"],
                        delivery.get("task_id"),
                        delivery["status"],
                        delivery["idempotency_key"],
                        _ser(delivery),
                        delivery["created_at"],
                        delivery["updated_at"],
                    ),
                )
                return None
            except sqlite3.IntegrityError:
                # A reservation that never reached a terminal status is not
                # proof the message went out: the send may have raised
                # (network blip, Slack 5xx) or the process may have died
                # between reserving and sending. Leaving those rows to block
                # forever would turn one transient Slack error into a
                # permanently lost message -- strictly worse than the
                # double-send this reservation exists to prevent. Reclaim
                # them atomically: the conditional UPDATE only wins if the
                # row is genuinely abandoned, so a live in-flight send is
                # still protected.
                if reclaim_stale_before:
                    cur.execute(
                        """UPDATE deliveries
                        SET id = ?, status = ?, payload = ?, updated_at = ?
                        WHERE organization_id = ? AND idempotency_key = ?
                          AND (status = 'failed'
                               OR (status = 'sending' AND updated_at < ?))""",
                        (
                            delivery["id"],
                            delivery["status"],
                            _ser(delivery),
                            delivery["updated_at"],
                            delivery["organization_id"],
                            delivery["idempotency_key"],
                            reclaim_stale_before,
                        ),
                    )
                    if cur.rowcount == 1:
                        return None
                cur.execute(
                    """SELECT payload FROM deliveries
                    WHERE organization_id = ? AND idempotency_key = ?""",
                    (delivery["organization_id"], delivery["idempotency_key"]),
                )
                row = cur.fetchone()
                return _de(row["payload"]) if row else None

    def upsert_delivery(self, delivery: dict[str, Any]) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO deliveries
                (id, organization_id, task_id, status, idempotency_key, payload,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    delivery["id"],
                    delivery["organization_id"],
                    delivery.get("task_id"),
                    delivery["status"],
                    delivery["idempotency_key"],
                    _ser(delivery),
                    delivery["created_at"],
                    delivery["updated_at"],
                ),
            )

    def delivery_by_idempotency_key(
        self, organization_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM deliveries
                WHERE organization_id = ? AND idempotency_key = ?""",
                (organization_id, idempotency_key),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def upsert_simple_harness_record(
        self, table: str, record: dict[str, Any]
    ) -> None:
        """Upsert low-volume configuration/projection records."""
        table = self._checked_harness_table(table)
        now = datetime.now(timezone.utc).isoformat()
        if table == "channel_policies":
            sql = """INSERT OR REPLACE INTO channel_policies
                (id, organization_id, source, channel_id, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)"""
            values = (
                record["id"],
                record["organization_id"],
                record["source"],
                record["channel_id"],
                _ser(record),
                record.get("updated_at") or now,
            )
        elif table == "identity_mappings":
            sql = """INSERT OR REPLACE INTO identity_mappings
                (id, organization_id, source, source_user_id, principal_id, payload)
                VALUES (?, ?, ?, ?, ?, ?)"""
            values = (
                record["id"],
                record["organization_id"],
                record["source"],
                record["source_user_id"],
                record["principal_id"],
                _ser(record),
            )
        elif table == "tool_policies":
            sql = """INSERT OR REPLACE INTO tool_policies
                (id, organization_id, tool_name, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)"""
            values = (
                record["id"],
                record["organization_id"],
                record["tool_name"],
                _ser(record),
                record.get("updated_at") or now,
            )
        elif table == "roles":
            sql = """INSERT OR REPLACE INTO roles
                (id, organization_id, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)"""
            values = (
                record["id"],
                record["organization_id"],
                record.get("status") or "active",
                _ser(record),
                record.get("created_at") or now,
                record.get("updated_at") or now,
            )
        else:
            raise ValueError(f"no simple upsert mapping for {table}")
        with self.cursor() as cur:
            cur.execute(sql, values)

    def upsert_harness_projection(
        self, table: str, record: dict[str, Any]
    ) -> None:
        """Persist common Task/workspace/capability projections safely."""
        allowed = {
            "skill_packages", "capabilities", "task_skill_refs",
            "task_capability_refs", "workspaces", "plans", "automation_watermarks",
            "task_pattern_signals", "task_scratchpads",
        }
        if table not in allowed:
            raise ValueError(f"unsupported harness projection table: {table}")
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor() as cur:
            cur.execute(
                f"""INSERT OR REPLACE INTO {table}
                (id, organization_id, status, task_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"],
                    record["organization_id"],
                    record.get("status", "active"),
                    record.get("task_id"),
                    _ser(record),
                    record.get("created_at", now),
                    record.get("updated_at", now),
                ),
            )

    def delete_harness_projection(self, table: str, record_id: str) -> None:
        allowed = {
            "skill_packages", "capabilities", "task_skill_refs",
            "task_capability_refs", "workspaces", "plans", "automation_watermarks",
            "task_pattern_signals", "task_scratchpads",
        }
        if table not in allowed:
            raise ValueError(f"unsupported harness projection table: {table}")
        with self.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))

    def identity_mapping(
        self, organization_id: str, source: str, source_user_id: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM identity_mappings
                WHERE organization_id = ? AND source = ? AND source_user_id = ?""",
                (organization_id, source, source_user_id),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def channel_policy(
        self, organization_id: str, source: str, channel_id: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM channel_policies
                WHERE organization_id = ? AND source = ? AND channel_id = ?""",
                (organization_id, source, channel_id),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def connector_account(
        self, organization_id: str, toolkit: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT payload FROM connector_accounts
                WHERE organization_id = ? AND toolkit = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT 1""",
                (organization_id, toolkit),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def connector_account_by_external_id(
        self, connected_account_id: str, *, active_only: bool = True
    ) -> dict[str, Any] | None:
        """Resolve webhook tenancy from trusted server-side account state."""
        with self.cursor() as cur:
            sql = (
                "SELECT payload FROM connector_accounts "
                "WHERE json_extract(payload, '$.connected_account_id') = ?"
            )
            params: list[Any] = [connected_account_id]
            if active_only:
                sql += " AND status = 'active'"
            sql += " ORDER BY updated_at DESC LIMIT 1"
            cur.execute(sql, params)
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def list_connector_accounts(
        self, organization_id: str, *, active_only: bool = False
    ) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            sql = (
                "SELECT payload FROM connector_accounts "
                "WHERE organization_id = ?"
            )
            params: list[Any] = [organization_id]
            if active_only:
                sql += " AND status = 'active'"
            sql += " ORDER BY updated_at DESC"
            cur.execute(sql, params)
            return [_de(row["payload"]) for row in cur.fetchall()]

    def upsert_connector_account(self, account: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        record = dict(account)
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO connector_accounts
                (id, organization_id, toolkit, status, payload, created_at,
                 updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"],
                    record["organization_id"],
                    record["toolkit"],
                    record.get("status", "active"),
                    _ser(record),
                    record["created_at"],
                    record["updated_at"],
                ),
            )

    def slack_installation_by_workspace(
        self, workspace_id: str
    ) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                "SELECT payload FROM slack_installations WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def upsert_slack_installation(self, installation: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO slack_installations
                (id, organization_id, workspace_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    installation["id"],
                    installation["organization_id"],
                    installation["workspace_id"],
                    _ser(installation),
                    installation.get("created_at") or now,
                    installation.get("updated_at") or now,
                ),
            )

    # ---- durable leased work queue ----

    def enqueue_harness_job(self, job: dict[str, Any]) -> dict[str, Any]:
        with self.cursor() as cur:
            try:
                cur.execute(
                    """INSERT INTO harness_jobs
                    (id, organization_id, kind, status, available_at, lease_owner,
                     lease_expires_at, attempts, max_attempts, idempotency_key,
                     payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job["id"],
                        job["organization_id"],
                        job["kind"],
                        job.get("status", "queued"),
                        job["available_at"],
                        job.get("lease_owner"),
                        job.get("lease_expires_at"),
                        int(job.get("attempts", 0)),
                        int(job.get("max_attempts", 5)),
                        job["idempotency_key"],
                        _ser(job),
                        job["created_at"],
                        job["updated_at"],
                    ),
                )
                return job
            except sqlite3.IntegrityError:
                cur.execute(
                    """SELECT payload FROM harness_jobs
                    WHERE organization_id = ? AND idempotency_key = ?""",
                    (job["organization_id"], job["idempotency_key"]),
                )
                row = cur.fetchone()
                return _de(row["payload"]) if row else job

    def get_harness_job(self, job_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute("SELECT payload FROM harness_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            return _de(row["payload"]) if row else None

    def lease_harness_jobs(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        # Selection and claiming are one atomic writer transaction across
        # threads and gateway processes. A competitor waits, then re-runs the
        # query after these rows are no longer eligible.
        with self.cursor(immediate=True) as cur:
            clauses = [
                "available_at <= ?",
                "(status = 'queued' OR (status = 'leased' AND lease_expires_at < ?))",
                "attempts < max_attempts",
            ]
            params: list[Any] = [now_s, now_s]
            if kinds:
                placeholders = ", ".join("?" for _ in kinds)
                clauses.append(f"kind IN ({placeholders})")
                params.extend(kinds)
            cur.execute(
                f"""SELECT id, payload FROM harness_jobs
                WHERE {' AND '.join(clauses)}
                ORDER BY available_at, created_at LIMIT ?""",
                [*params, int(limit)],
            )
            rows = cur.fetchall()
            leased: list[dict[str, Any]] = []
            for row in rows:
                job = _de(row["payload"])
                # A job can carry its own lease_seconds override (set at
                # enqueue time for kinds that drive a full agent loop and can
                # legitimately outlive the default lease) -- fall back to the
                # batch-wide default otherwise.
                row_lease_seconds = int(job.get("lease_seconds") or lease_seconds)
                expiry = (now + timedelta(seconds=row_lease_seconds)).isoformat()
                job["status"] = "leased"
                job["lease_owner"] = worker_id
                job["lease_expires_at"] = expiry
                job["attempts"] = int(job.get("attempts", 0)) + 1
                job["updated_at"] = now_s
                cur.execute(
                    """UPDATE harness_jobs SET status = 'leased', lease_owner = ?,
                    lease_expires_at = ?, attempts = ?, payload = ?, updated_at = ?
                    WHERE id = ?""",
                    (
                        worker_id,
                        expiry,
                        job["attempts"],
                        _ser(job),
                        now_s,
                        job["id"],
                    ),
                )
                leased.append(job)
            return leased

    def finish_harness_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        retry_at: str | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        with self.cursor(immediate=True) as cur:
            cur.execute("SELECT payload FROM harness_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            job = _de(row["payload"])
            if lease_owner is not None and job.get("lease_owner") != lease_owner:
                return None
            now = datetime.now(timezone.utc).isoformat()
            job["status"] = status
            job["updated_at"] = now
            job["lease_owner"] = None
            job["lease_expires_at"] = None
            if result is not None:
                job["result"] = result
            if error:
                job["error"] = error
            if retry_at:
                job["available_at"] = retry_at
            cur.execute(
                """UPDATE harness_jobs SET status = ?, available_at = ?,
                lease_owner = NULL, lease_expires_at = NULL, payload = ?, updated_at = ?
                WHERE id = ?""",
                (status, job["available_at"], _ser(job), now, job_id),
            )
        return job

    def harness_job_counts(self, organization_id: str) -> dict[str, int]:
        with self.cursor() as cur:
            cur.execute(
                """SELECT status, COUNT(*) AS count FROM harness_jobs
                WHERE organization_id = ? GROUP BY status""",
                (organization_id,),
            )
            return {row["status"]: int(row["count"]) for row in cur.fetchall()}

    def harness_job_queue_age_seconds(self, organization_id: str) -> dict[str, float]:
        """Oldest still-queued job's wait time per kind — the plan §9/§7
        observability requirement made concrete: a knowledge lane backing up
        should be visible in the same place a stuck control job would be,
        without having to guess from job counts alone."""
        with self.cursor() as cur:
            cur.execute(
                """SELECT kind, MIN(available_at) AS oldest FROM harness_jobs
                WHERE organization_id = ? AND status = 'queued' GROUP BY kind""",
                (organization_id,),
            )
            rows = cur.fetchall()
        now = datetime.now(timezone.utc)
        ages: dict[str, float] = {}
        for row in rows:
            try:
                oldest = datetime.fromisoformat(row["oldest"])
            except (TypeError, ValueError):
                continue
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            ages[row["kind"]] = max(0.0, (now - oldest).total_seconds())
        return ages

    def episode_size_stats(self, organization_id: str) -> dict[str, Any]:
        with self.cursor() as cur:
            cur.execute(
                """SELECT status, COUNT(*) AS n, AVG(event_count) AS avg_events, MAX(event_count) AS max_events
                FROM episodes WHERE organization_id = ? GROUP BY status""",
                (organization_id,),
            )
            return {
                row["status"]: {
                    "count": int(row["n"]),
                    "avg_events": round(float(row["avg_events"] or 0), 1),
                    "max_events": int(row["max_events"] or 0),
                }
                for row in cur.fetchall()
            }
