"""Asynchronous post-Task capture and ambient episode consolidation."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.services.scope_resolution import resolve_scope

_CITED_ID_PATTERN = re.compile(r"\b(?:src|mem)_[0-9a-f]{16}\b")


def _outcome_text(tool_name: str, arguments: dict[str, Any]) -> str:
    args = arguments or {}
    if tool_name == "slack_send_message":
        channel = args.get("channel_id") or args.get("channel") or "the conversation"
        return f"Posted a message to {channel}"
    if tool_name in {"artifact_create", "pdf_create", "docx_create", "html_create"}:
        title = args.get("title") or args.get("name") or args.get("filename") or "a file"
        return f"Created artifact '{title}'"
    if tool_name == "slack_send_artifact":
        title = args.get("title") or args.get("artifact_id") or "an artifact"
        return f"Shared artifact '{title}'"
    if tool_name == "integration_execute":
        slug = str(args.get("tool_slug") or args.get("action") or "").replace("_", " ").lower()
        return f"Ran connected-app action {slug or tool_name}".strip()
    if tool_name in {"write_file", "edit_file", "move_file", "delete_file"}:
        path = args.get("path") or args.get("destination") or "a file"
        verb = {"write_file": "Wrote", "edit_file": "Edited", "move_file": "Moved", "delete_file": "Deleted"}[tool_name]
        return f"{verb} file {path}"
    if tool_name.startswith("automation_"):
        return f"Updated scheduled automation ({tool_name})"
    return f"Ran {tool_name}"

# Episode boundaries (memory ingestion/retrieval plan §5 Phase C): a quiet
# window alone isn't enough for a high-volume group (a busy channel, a
# noisy connector) -- without a hard cap it would grow one episode forever
# and every consolidation tick would re-embed and re-extract more text than
# the last. Once either limit is hit the episode is closed ("complete");
# the next new source under the same group_key opens a fresh one.
_EPISODE_MAX_EVENTS = 200
_EPISODE_MAX_SUMMARY_CHARS = 4000


class HarnessMemoryCapture:
    def __init__(
        self,
        *,
        store: Any,
        ingestion: Any,
        audit: Any,
        model_extractor: Any | None = None,
        embeddings: Any | None = None,
        skills: Any | None = None,
        history_compactor: Any | None = None,
    ) -> None:
        self.store = store
        self.ingestion = ingestion
        self.audit = audit
        self.model_extractor = model_extractor
        self.embeddings = embeddings
        self.skills = skills
        self.history_compactor = history_compactor

    def capture_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        envelope = task["envelope"]
        tool_calls = self.store.list_harness_records(
            "harness_tool_calls", task["organization_id"], where={"task_id": task_id}
        )
        completed_writes = [
            c
            for c in tool_calls
            if (c.get("status") or (c.get("payload") or {}).get("status")) == "completed"
            and (c.get("risk") or (c.get("payload") or {}).get("risk", "read")) != "read"
        ]
        # Keep the existing rule: an automation firing with no tool calls
        # writes nothing.
        if task.get("trigger_kind") == "automation" and not tool_calls:
            return {"task_id": task_id, "captured": False, "reason": "empty_automation"}
        # Task transcripts are scoped to the owning Session and carry the
        # Task identity on each message.
        messages = self.store.list_session_messages(task["session_id"], limit=200)
        own_messages = [m for m in messages if m.get("task_id") == task_id]
        user_messages = [
            m for m in own_messages if m.get("role") == "user" and str(m.get("content") or "").strip()
        ]
        if not user_messages and not completed_writes and not str(task.get("final_answer") or "").strip():
            return {"task_id": task_id, "captured": False, "reason": "empty_transcript"}
        use_model = bool(self.model_extractor and self.model_extractor.live)
        # H-12: capture writes to the scope of the conversation this Task
        # actually happened in, not an arbitrary pick from the authorization
        # decision's allowed_scope_ids (which can legitimately span more
        # than one room and was never a statement of provenance).
        write_scope_id = resolve_scope(
            store=self.store,
            organization_id=task["organization_id"],
            principal_id=envelope["principal_id"],
            destination=envelope["destination"],
        ).write_scope_id
        memories: list[dict[str, Any]] = []
        source_ids: list[str] = []
        # 1. What people said, attributed to the speaker, through extraction.
        for msg in user_messages:
            meta = msg.get("metadata") or {}
            author = (
                meta.get("author_entity_id")
                or meta.get("principal_id")
                or envelope["principal_id"]
            )
            result = self.ingestion.ingest(
                organization_id=task["organization_id"],
                source_system="toda_task_user",
                source_type="task_user_message",
                external_id=f"{task_id}:{msg.get('id')}",
                content=str(msg.get("content") or ""),
                scope_id=write_scope_id,
                author_entity_id=author,
                channel_id=envelope["destination"].get("id"),
                is_private_channel=bool(envelope["destination"].get("is_private")),
                entity_ids=list((msg.get("metadata") or {}).get("entity_ids") or []),
                metadata={"task_id": task_id, "trigger_kind": task["trigger_kind"]},
                learn=not use_model,
                processing_mode="reference" if use_model else "memory",
            )
            source_ids.append(result["source"]["id"])
            extracted = result["memories"]
            if use_model:
                try:
                    extracted = self.model_extractor.extract_source(result["source"])
                except Exception:
                    extracted = self.ingestion.memory.learn_from_source(result["source"])
                if self.ingestion.reality:
                    self.ingestion.reality.ingest_source(result["source"], extracted)
            memories.extend(extracted)
        # 2. What Toda did: one deterministic outcome memory per completed
        # write tool call. No model call, never materialized into facts.
        now = utcnow().isoformat()
        outcomes: list[dict[str, Any]] = []
        for call in completed_writes:
            payload = call.get("payload") if isinstance(call.get("payload"), dict) else None
            tool_name = call.get("tool_name") or (payload or {}).get("tool_name") or "tool"
            arguments = call.get("arguments") or (payload or {}).get("arguments") or {}
            outcomes.append(
                {
                    "id": new_id("mem"),
                    "organization_id": task["organization_id"],
                    "memory_type": "outcome",
                    "content": _outcome_text(str(tool_name), dict(arguments)),
                    "status": "active",
                    "is_latest": True,
                    "confidence": 0.9,
                    "authority": 0.5,
                    "scope_id": write_scope_id,
                    "source_ids": [],
                    "entity_ids": [],
                    "observed_at": now,
                    "valid_from": now,
                    "author_entity_id": None,
                }
            )
        for mem in outcomes:
            self.store.upsert_memory(mem)
        if outcomes and self.ingestion.indexing is not None:
            try:
                self.ingestion.indexing.index_memories(outcomes)
            except Exception:
                pass
        memories.extend(outcomes)
        # 3. Toda's answer as low-authority derived memory, never a fact.
        final_answer = str(task.get("final_answer") or "").strip()
        if final_answer:
            cited = [
                cid
                for cid in _CITED_ID_PATTERN.findall(final_answer)
                if cid.startswith("src_") and self.store.get_source(cid) is not None
            ]
            derived = {
                "id": new_id("mem"),
                "organization_id": task["organization_id"],
                "memory_type": "derived",
                "content": final_answer[:4000],
                "status": "active",
                "is_latest": True,
                "confidence": 0.5,
                "authority": 0.3,
                "scope_id": write_scope_id,
                "source_ids": cited,
                "entity_ids": [],
                "observed_at": now,
                "valid_from": now,
                "author_entity_id": None,
            }
            self.store.upsert_memory(derived)
            if self.ingestion.indexing is not None:
                try:
                    self.ingestion.indexing.index_memories([derived])
                except Exception:
                    pass
            memories.append(derived)
        self.audit.record(
            task["organization_id"],
            "memory.capture",
            {
                "task_id": task_id,
                "source_id": source_ids[0] if source_ids else None,
                "memory_count": len(memories),
            },
            principal_id=envelope["principal_id"],
        )
        return {
            "task_id": task_id,
            "captured": True,
            "source_id": source_ids[0] if source_ids else None,
            "memory_ids": [memory["id"] for memory in memories],
        }

    def consolidate_episode(
        self,
        organization_id: str,
        *,
        group_key: str,
        group_kind: str,
        source_system: str,
        scope_id: str,
        quiet_seconds: int = 0,
    ) -> dict[str, Any]:
        """Incrementally fold new evidence into one episode per group_key
        (memory ingestion/retrieval plan §5 Phase C). Only sources newer
        than the episode's own watermark (`ended_at`) are ever read — a
        second, third, Nth consolidation of a long-running thread or issue
        costs O(new events), not O(whole history), and generalizes past
        Slack: any connector that can name a stable group_key (GitHub
        issue/PR, Linear issue, ...) gets the same incremental treatment.
        """
        episode = self.store.get_episode_by_group_key(organization_id, group_key)
        carry_forward_watermark = None
        if episode and episode["status"] == "complete":
            # This group_key's current episode hit a size boundary and was
            # closed; it stays as searchable history and a fresh one opens
            # -- but still picks up strictly after the closed episode's
            # watermark, not from the beginning of the group's history.
            carry_forward_watermark = episode["ended_at"]
            episode = None

        now = utcnow().isoformat()
        new_sources = self.store.list_sources_by_group(
            organization_id,
            group_key,
            after_occurred_at=episode["ended_at"] if episode else carry_forward_watermark,
            limit=_EPISODE_MAX_EVENTS,
        )
        if not new_sources:
            return {"group_key": group_key, "consolidated": False, "reason": "no_new_sources"}

        latest_occurred_at = new_sources[-1].get("occurred_at") or now
        if quiet_seconds:
            updated = datetime.fromisoformat(latest_occurred_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - updated < timedelta(seconds=quiet_seconds):
                return {"group_key": group_key, "consolidated": False, "reason": "group_not_quiet"}

        if episode is None:
            episode = {
                "id": new_id("episode"),
                "organization_id": organization_id,
                "group_key": group_key,
                "group_kind": group_kind,
                "source_system": source_system,
                "started_at": new_sources[0].get("occurred_at") or now,
                "ended_at": latest_occurred_at,
                "last_source_id": new_sources[-1]["id"],
                "event_count": 0,
                "status": "consolidating",
                "summary": None,
                "summary_embedding": None,
                "extraction_revision": 0,
                "scope_id": scope_id,
                "privacy_class": "internal",
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
        else:
            episode["status"] = "consolidating"
            episode["ended_at"] = latest_occurred_at
            episode["last_source_id"] = new_sources[-1]["id"]
            episode["updated_at"] = now

        start_ordinal = episode["event_count"]
        self.store.add_episode_sources(episode["id"], new_sources, start_ordinal=start_ordinal)
        episode["event_count"] = start_ordinal + len(new_sources)

        transcript = "\n".join(
            f"{source.get('author_entity_id') or 'unknown'}: {source.get('content') or ''}"
            for source in new_sources
        )
        prior_summary = episode.get("summary") or ""
        use_model = bool(self.model_extractor and self.model_extractor.live)
        try:
            result = self.ingestion.ingest(
                organization_id=organization_id,
                source_system="toda_consolidation",
                source_type=group_kind,
                # Same external_id across ticks -> the store versions each
                # consolidation of this episode rather than treating it as
                # unrelated evidence, mirroring how a Source's own edits
                # version instead of overwrite.
                external_id=episode["id"],
                content=(
                    (f"Prior summary: {prior_summary}\n\n" if prior_summary else "")
                    + transcript
                ),
                scope_id=scope_id,
                author_entity_id=None,
                channel_id=new_sources[-1].get("channel_id"),
                is_private_channel=bool(new_sources[-1].get("is_private_channel")),
                entity_ids=sorted(
                    {eid for source in new_sources for eid in source.get("entity_ids") or []}
                ),
                metadata={
                    # Deliberately NOT "group_key": this synthesized
                    # consolidation source is derived FROM the group's raw
                    # evidence, not a new member of it -- stamping the same
                    # group_key here would make list_sources_by_group feed
                    # each consolidation's own output back in as if it were
                    # a new raw event on the next tick.
                    "source_group_key": group_key,
                    "group_kind": group_kind,
                    "episode_id": episode["id"],
                    "source_ids": [s["id"] for s in new_sources],
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
                    self.ingestion.reality.ingest_source(result["source"], memories)
        except Exception as exc:
            episode["status"] = "failed"
            episode["error"] = str(exc)
            self.store.upsert_episode(episode)
            raise

        # Rolling summary via the shared compactor when live; today's
        # truncation stays as the offline fallback. Extraction above keeps
        # running on the new transcript, not the summary.
        if self.history_compactor is not None:
            try:
                episode["summary"] = self.history_compactor.compact(
                    previous_summary=prior_summary,
                    new_messages=[
                        {
                            "role": "user",
                            "content": f"{source.get('author_entity_id') or 'unknown'}: {source.get('content') or ''}",
                            "id": source["id"],
                        }
                        for source in new_sources
                    ],
                )
            except Exception:
                combined = f"{prior_summary}\n{transcript}".strip() if prior_summary else transcript
                episode["summary"] = combined[-_EPISODE_MAX_SUMMARY_CHARS:]
        else:
            combined = f"{prior_summary}\n{transcript}".strip() if prior_summary else transcript
            episode["summary"] = combined[-_EPISODE_MAX_SUMMARY_CHARS:]
        episode["summary_embedding"] = (
            self.embeddings.embed([episode["summary"]])[0] if self.embeddings and episode["summary"].strip() else None
        )
        episode["extraction_revision"] = int(episode.get("extraction_revision", 0)) + 1
        episode["status"] = "complete" if episode["event_count"] >= _EPISODE_MAX_EVENTS else "quiet"
        self.store.upsert_episode(episode)

        self.audit.record(
            organization_id,
            "memory.capture",
            {
                "episode_id": episode["id"],
                "group_key": group_key,
                "new_source_count": len(new_sources),
                "event_count": episode["event_count"],
                "status": episode["status"],
            },
        )
        return {
            "group_key": group_key,
            "consolidated": True,
            "episode_id": episode["id"],
            "status": episode["status"],
            "memory_ids": [memory["id"] for memory in memories],
        }
