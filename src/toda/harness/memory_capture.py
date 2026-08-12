"""Asynchronous post-Task capture and ambient episode consolidation."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.harness.prompt_library import load
from toda.services.scope_resolution import resolve_scope

# Response contract for skill synthesis (memory ingestion/retrieval plan
# §10): steps grounded in what the supporting Tasks actually did, each
# tagged with which Task(s) it came from so a draft is auditable back to
# real evidence, not just a plausible-sounding model guess.
_SKILL_SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "source_task_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "source_task_ids"],
            },
        },
        "inputs": {"type": "array", "items": {"type": "string"}},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "expected_outputs": {"type": "array", "items": {"type": "string"}},
        "failure_handling": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps", "inputs", "preconditions", "expected_outputs", "failure_handling"],
}
# Two Tasks' worth of evidence is barely a pattern; without a floor here a
# synthesis call could run on a signal that's about to hit the proposal
# threshold with only its first couple of occurrences gathered so far.
_SKILL_SYNTHESIS_MIN_TASKS = 2
_SKILL_SYNTHESIS_MAX_TOOL_CALLS = 20
_SKILL_SYNTHESIS_TRANSCRIPT_CHARS = 4000

# Pattern-recurrence detection (self-improvement layer): tuned to be
# noticeable without nagging. 0.85 cosine is stricter than typical semantic
# search thresholds -- this is deciding "the same underlying ask", not
# "topically related". 3 occurrences before proposing mirrors Hermes's own
# "noticed a recurring ask" framing, and its MAX_PENDING-style discipline of
# not surfacing a proposal on the first sighting.
_PATTERN_SIMILARITY_THRESHOLD = 0.85
_PATTERN_PROPOSAL_THRESHOLD = 3
_PATTERN_TRACKED_TRIGGER_KINDS = {"reactive", "proactive"}

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
            source_system="toda_agent",
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

    def _gather_task_evidence(self, task_id: str) -> dict[str, Any] | None:
        """The same evidence a human reviewer would want: what was asked,
        what the Task actually did (tool calls with real arguments and
        outcomes, not just a transcript), and how it ended."""
        task = self.store.get_task(task_id)
        if not task:
            return None
        messages = self.store.list_session_messages(task["session_id"], limit=200)
        own_messages = [m for m in messages if m.get("task_id") == task_id]
        transcript = "\n".join(
            f"{m['role'].upper()}: {m.get('content') or ''}" for m in own_messages
        )
        tool_calls = self.store.list_harness_records(
            "harness_tool_calls", task["organization_id"], where={"task_id": task_id}
        )
        return {
            "task_id": task_id,
            "goal": task.get("goal"),
            "status": task.get("state"),
            "final_answer": task.get("final_answer"),
            "transcript": transcript[:_SKILL_SYNTHESIS_TRANSCRIPT_CHARS],
            "tool_calls": [
                {
                    "tool_name": call.get("tool_name"),
                    "arguments": call.get("arguments"),
                    "status": call.get("status"),
                    "error": call.get("error"),
                }
                for call in tool_calls[:_SKILL_SYNTHESIS_MAX_TOOL_CALLS]
            ],
        }

    def _synthesize_skill_from_tasks(self, task_ids: list[str]) -> dict[str, Any] | None:
        """Ask the model for concrete steps/inputs/preconditions grounded in
        what the supporting Tasks actually did. Returns None (never raises)
        on anything short of a clean, schema-valid response — the caller
        falls back to an empty-steps draft shell exactly like before this
        existed, so a bad model response degrades gracefully rather than
        losing the pattern-detection signal itself."""
        if not self.model_extractor or not self.model_extractor.live:
            return None
        evidence = [e for e in (self._gather_task_evidence(tid) for tid in task_ids) if e]
        if len(evidence) < _SKILL_SYNTHESIS_MIN_TASKS:
            return None
        try:
            response = self.model_extractor.model.generate(
                messages=[
                    {"role": "system", "content": load("skill_synthesis")},
                    {
                        "role": "user",
                        "content": json.dumps({"tasks": evidence}, sort_keys=True, default=str),
                    },
                ],
                response_schema=_SKILL_SYNTHESIS_SCHEMA,
                temperature=0.0,
                max_tokens=1800,
            )
            return json.loads(response.content)
        except Exception:
            return None

    def _propose_skill_from_pattern(self, signal: dict[str, Any]) -> None:
        task_ids = signal.get("task_ids") or []
        synthesized = self._synthesize_skill_from_tasks(task_ids) or {}
        steps = synthesized.get("steps") or []
        failure_handling = synthesized.get("failure_handling") or []

        warnings = [
            "Auto-drafted from a recurring pattern across separate Tasks, "
            "not authored by a human or reviewed in context. Verify the "
            "steps before approving.",
        ]
        warnings.extend(f"Failure handling (from supporting Tasks): {item}" for item in failure_handling)
        if not steps:
            warnings.append(
                "Model synthesis produced no steps (or the extraction model "
                "isn't live) -- steps left empty for manual authoring."
            )

        skill = self.skills.create(
            organization_id=signal["organization_id"],
            name=f"Recurring request: {signal['sample_goal'][:60]}",
            description=(
                f"Auto-detected: {signal['occurrence_count']} separate requests "
                f"since {signal.get('created_at', '')[:10]} looked like the same "
                f"underlying ask (Tasks: {', '.join(task_ids[-5:])}). Drafted for "
                "review — verify before approving."
            ),
            goal=signal["sample_goal"],
            steps=steps,
            inputs=synthesized.get("inputs") or [],
            preconditions=synthesized.get("preconditions") or [],
            warnings=warnings,
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

        # A rolling window, not a fresh model summarization pass every
        # tick -- consolidation must stay O(new events); a dedicated
        # summarizer call can replace this later without changing the
        # episode contract (id, summary, summary_embedding stay the same).
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
            "memory.episode_consolidated",
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
