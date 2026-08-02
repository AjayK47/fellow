"""Asynchronous post-run capture and ambient episode consolidation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from docket.domain.models import new_id, utcnow
from docket.services.scope_resolution import resolve_scope


class HarnessMemoryCapture:
    def __init__(
        self,
        *,
        store: Any,
        ingestion: Any,
        audit: Any,
        model_extractor: Any | None = None,
    ) -> None:
        self.store = store
        self.ingestion = ingestion
        self.audit = audit
        self.model_extractor = model_extractor

    def capture_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_harness_record("agent_runs", run_id)
        if not run:
            raise KeyError(run_id)
        messages = self.store.session_messages(run["session_id"], limit=200)
        transcript = "\n".join(
            f"{message['role'].upper()}: {message.get('content') or ''}"
            for message in messages
            if message.get("run_id") == run_id
        )
        if not transcript.strip():
            return {"run_id": run_id, "captured": False, "reason": "empty_transcript"}
        envelope = run["envelope"]
        use_model = bool(self.model_extractor and self.model_extractor.live)
        # H-12: capture writes to the scope of the conversation this run
        # actually happened in, not an arbitrary pick from the authorization
        # decision's allowed_scope_ids (which can legitimately span more
        # than one room and was never a statement of provenance).
        write_scope_id = resolve_scope(
            store=self.store,
            organization_id=run["organization_id"],
            principal_id=envelope["principal_id"],
            destination=envelope["destination"],
        ).write_scope_id
        result = self.ingestion.ingest(
            organization_id=run["organization_id"],
            source_system="docket_agent",
            source_type="agent_run",
            external_id=run_id,
            content=(
                f"Goal: {run['goal']}\nStatus: {run['state']}\n"
                f"Final answer: {run.get('final_answer') or ''}\n\n{transcript}"
            ),
            scope_id=write_scope_id,
            author_entity_id=envelope["principal_id"],
            channel_id=envelope["destination"].get("id"),
            is_private_channel=bool(envelope["destination"].get("is_private")),
            entity_ids=list(run.get("metadata", {}).get("entity_ids") or []),
            metadata={
                "run_id": run_id,
                "trace_id": run["trace_id"],
                "trigger_kind": run["trigger_kind"],
                "conversation_id": envelope["conversation_id"],
            },
            learn=not use_model,
            processing_mode="reference" if use_model else "memory",
        )
        memories = result["memories"]
        if use_model:
            try:
                memories = self.model_extractor.extract_source(result["source"])
            except Exception:
                memories = self.ingestion.memory.learn_from_source(result["source"])
            if self.ingestion.reality:
                self.ingestion.reality.ingest_source(
                    result["source"], memories
                )
        self.audit.record(
            run["organization_id"],
            "memory.run_captured",
            {
                "run_id": run_id,
                "source_id": result["source"]["id"],
                "memory_count": len(memories),
            },
            principal_id=envelope["principal_id"],
            trace_id=run["trace_id"],
        )
        return {
            "run_id": run_id,
            "captured": True,
            "source_id": result["source"]["id"],
            "memory_ids": [memory["id"] for memory in memories],
        }

    def consolidate_conversation(
        self,
        organization_id: str,
        conversation_key: str,
        *,
        quiet_seconds: int = 0,
    ) -> dict[str, Any]:
        conversation = self.store.conversation_state(
            organization_id, conversation_key
        )
        if quiet_seconds and conversation and conversation.get("updated_at"):
            updated = datetime.fromisoformat(conversation["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - updated < timedelta(
                seconds=quiet_seconds
            ):
                return {
                    "conversation_key": conversation_key,
                    "consolidated": False,
                    "reason": "conversation_not_quiet",
                }
        sources = [
            source
            for source in self.store.list_sources(organization_id)
            if (
                (source.get("metadata") or {}).get("context_key")
                or (source.get("metadata") or {}).get("conversation_key")
            ) == conversation_key
            and source.get("source_system") == "slack"
        ]
        if not sources:
            return {
                "conversation_key": conversation_key,
                "consolidated": False,
                "reason": "no_sources",
            }
        sources.sort(key=lambda source: source.get("occurred_at") or "")
        episode_id = new_id("episode")
        transcript = "\n".join(
            f"{source.get('author_entity_id') or 'unknown'}: {source.get('content') or ''}"
            for source in sources
        )
        latest = sources[-1]
        now = utcnow().isoformat()
        episode = {
            "id": episode_id,
            "organization_id": organization_id,
            "conversation_key": conversation_key,
            "status": "consolidated",
            "source_ids": [source["id"] for source in sources],
            "message_count": len(sources),
            "created_at": now,
            "updated_at": now,
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO episodes
                (id, organization_id, conversation_key, status, payload,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    episode_id,
                    organization_id,
                    conversation_key,
                    "consolidated",
                    __import__("json").dumps(episode),
                    now,
                    now,
                ),
            )
        use_model = bool(self.model_extractor and self.model_extractor.live)
        result = self.ingestion.ingest(
            organization_id=organization_id,
            source_system="docket_consolidation",
            source_type="slack_episode",
            external_id=episode_id,
            content=transcript,
            scope_id=latest["scope_id"],
            author_entity_id=None,
            channel_id=latest.get("channel_id"),
            is_private_channel=bool(latest.get("is_private_channel")),
            entity_ids=sorted(
                {
                    entity_id
                    for source in sources
                    for entity_id in source.get("entity_ids") or []
                }
            ),
            metadata={
                "conversation_key": conversation_key,
                "episode_id": episode_id,
                "source_ids": episode["source_ids"],
            },
            learn=not use_model,
            processing_mode="reference" if use_model else "memory",
        )
        memories = result["memories"]
        if use_model:
            try:
                memories = self.model_extractor.extract_source(result["source"])
            except Exception:
                memories = self.ingestion.memory.learn_from_source(result["source"])
            if self.ingestion.reality:
                self.ingestion.reality.ingest_source(
                    result["source"], memories
                )
        return {
            "conversation_key": conversation_key,
            "consolidated": True,
            "episode_id": episode_id,
            "memory_ids": [memory["id"] for memory in memories],
        }
