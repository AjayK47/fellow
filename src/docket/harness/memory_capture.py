"""Asynchronous post-Task capture and ambient episode consolidation."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from docket.domain.models import new_id, utcnow
from docket.services.scope_resolution import resolve_scope

# Pattern-recurrence detection (self-improvement layer): tuned to be
# noticeable without nagging. 0.85 cosine is stricter than typical semantic
# search thresholds -- this is deciding "the same underlying ask", not
# "topically related". 3 occurrences before proposing mirrors Hermes's own
# "noticed a recurring ask" framing, and its MAX_PENDING-style discipline of
# not surfacing a proposal on the first sighting.
_PATTERN_SIMILARITY_THRESHOLD = 0.85
_PATTERN_PROPOSAL_THRESHOLD = 3
_PATTERN_TRACKED_TRIGGER_KINDS = {"reactive", "proactive"}


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
    ) -> None:
        self.store = store
        self.ingestion = ingestion
        self.audit = audit
        self.model_extractor = model_extractor
        self.embeddings = embeddings
        self.skills = skills

    def capture_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        # Task transcripts are scoped to the owning Session and carry the
        # Task identity on each message.
        messages = self.store.list_session_messages(task["session_id"], limit=200)
        own_messages = [m for m in messages if m.get("task_id") == task_id]
        transcript = "\n".join(
            f"{message['role'].upper()}: {message.get('content') or ''}"
            for message in own_messages
        )
        if not transcript.strip():
            return {"task_id": task_id, "captured": False, "reason": "empty_transcript"}
        envelope = task["envelope"]
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
        result = self.ingestion.ingest(
            organization_id=task["organization_id"],
            source_system="docket_agent",
            source_type="agent_task",
            external_id=task_id,
            content=(
                f"Goal: {task['goal']}\nStatus: {task['state']}\n"
                f"Final answer: {task.get('final_answer') or ''}\n\n{transcript}"
            ),
            scope_id=write_scope_id,
            author_entity_id=envelope["principal_id"],
            channel_id=envelope["destination"].get("id"),
            is_private_channel=bool(envelope["destination"].get("is_private")),
            entity_ids=list(task.get("metadata", {}).get("entity_ids") or []),
            metadata={
                "task_id": task_id,
                "trace_id": task["trace_id"],
                "trigger_kind": task["trigger_kind"],
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
            task["organization_id"],
            "memory.task_captured",
            {
                "task_id": task_id,
                "source_id": result["source"]["id"],
                "memory_count": len(memories),
            },
            principal_id=envelope["principal_id"],
            trace_id=task["trace_id"],
        )
        pattern_signal = self._track_pattern_and_maybe_propose(task)
        return {
            "task_id": task_id,
            "captured": True,
            "source_id": result["source"]["id"],
            "memory_ids": [memory["id"] for memory in memories],
            "pattern_signal": pattern_signal,
        }

    def _track_pattern_and_maybe_propose(
        self, task: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Notice when a Task's goal semantically repeats a recent one, and
        once it has repeated enough to be a real pattern (not a coincidence),
        draft a skill for a human to review.

        This is the "bigger picture" pass a single Task's own model call can
        never have: it compares THIS Task's goal against other completed Tasks
        completed earlier, something no in-task reasoning has visibility
        into. Detection is a pure embedding-similarity comparison — no LLM
        call — so this stays cheap and doesn't add to the very tool/model
        call volume it exists to eventually reduce; only the (much rarer)
        proposal step, gated behind a real repetition threshold, writes
        anything.
        """
        if not self.embeddings or not self.skills:
            return None
        if task.get("trigger_kind") not in _PATTERN_TRACKED_TRIGGER_KINDS:
            return None
        if (task.get("worker_type") or (task.get("metadata") or {}).get("worker_type")) in {"general", "pi"}:
            return None
        goal = (task.get("goal") or "").strip()
        if len(goal) < 12:
            return None

        org = task["organization_id"]
        vector = self.embeddings.embed([goal])[0]
        now = utcnow().isoformat()
        existing = self.store.list_harness_records(
            "task_pattern_signals", org, where={"status": "open"}
        )
        best: tuple[float, dict[str, Any]] | None = None
        for candidate in existing:
            score = self._cosine(vector, candidate.get("embedding") or [])
            if score >= _PATTERN_SIMILARITY_THRESHOLD and (
                best is None or score > best[0]
            ):
                best = (score, candidate)

        if best is None:
            signal = {
                "id": new_id("pattern"),
                "organization_id": org,
                "status": "open",
                "task_id": task["id"],
                "sample_goal": goal,
                "embedding": vector,
                "occurrence_count": 1,
                "task_ids": [task["id"]],
                "created_at": now,
                "updated_at": now,
            }
            self.store.upsert_harness_projection("task_pattern_signals", signal)
            return signal

        signal = best[1]
        signal["occurrence_count"] = int(signal.get("occurrence_count", 1)) + 1
        signal["task_ids"] = (list(signal.get("task_ids") or []) + [task["id"]])[-10:]
        signal["updated_at"] = now
        if signal["occurrence_count"] >= _PATTERN_PROPOSAL_THRESHOLD:
            self._propose_skill_from_pattern(signal)
        else:
            self.store.upsert_harness_projection("task_pattern_signals", signal)
        return signal

    def _propose_skill_from_pattern(self, signal: dict[str, Any]) -> None:
        task_ids = signal.get("task_ids") or []
        skill = self.skills.create(
            organization_id=signal["organization_id"],
            name=f"Recurring request: {signal['sample_goal'][:60]}",
            description=(
                f"Auto-detected: {signal['occurrence_count']} separate requests "
                f"since {signal.get('created_at', '')[:10]} looked like the same "
                f"underlying ask (Tasks: {', '.join(task_ids[-5:])}). Drafted for "
                "review — steps are not filled in yet."
            ),
            goal=signal["sample_goal"],
            steps=[],
            inputs=[],
            preconditions=[],
            warnings=[
                "Auto-drafted from a recurring pattern across separate Tasks, "
                "not authored by a human or reviewed in context. Verify the "
                "steps before approving.",
            ],
            owner_id=None,
            status="draft",
        )
        signal["status"] = "proposed"
        signal["proposed_skill_id"] = skill["id"]
        self.store.upsert_harness_projection("task_pattern_signals", signal)
        self.audit.record(
            signal["organization_id"],
            "skill.pattern_detected",
            {
                "pattern_signal_id": signal["id"],
                "occurrence_count": signal["occurrence_count"],
                "task_ids": task_ids,
                "proposed_skill_id": skill["id"],
            },
        )

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(
            sum(v * v for v in right)
        )
        return (
            sum(a * b for a, b in zip(left, right)) / denominator
            if denominator
            else 0.0
        )

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
