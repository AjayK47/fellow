"""Restricted asynchronous review of completed root Tasks for reusable Skills."""

from __future__ import annotations

import json
from typing import Any

from toda.domain.models import utcnow


_CURATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["NO_CHANGE", "NEW_SKILL_DRAFT", "SKILL_UPDATE_DRAFT", "EXISTING_SKILL_COVERS_IT"],
        },
        "reason": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "instructions": {"type": ["string", "null"]},
        "existing_skill_id": {"type": ["string", "null"]},
    },
    "required": ["outcome", "reason", "name", "description", "instructions", "existing_skill_id"],
    "additionalProperties": False,
}


class SkillCurator:
    def __init__(self, *, store: Any, skills: Any, model: Any, audit: Any) -> None:
        self.store = store
        self.skills = skills
        self.model = model
        self.audit = audit

    def review(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task or task.get("parent_task_id") is not None or task.get("state") != "completed":
            return {"task_id": task_id, "outcome": "NO_CHANGE", "reason": "not_a_completed_root_task"}
        if int(task.get("tool_call_count") or 0) == 0 and not self.store.list_tasks(
            task["organization_id"], parent_task_id=task_id, limit=1
        ):
            return {"task_id": task_id, "outcome": "NO_CHANGE", "reason": "no_reusable_procedure"}
        existing = [
            item
            for item in self.skills.list(task["organization_id"])
            if item.get("status") in {"active", "draft"}
        ]
        related_tasks = [
            {
                "id": item["id"],
                "goal": item.get("goal"),
                "summary": item.get("completion_summary"),
            }
            for item in self.store.search_root_tasks(
                task["organization_id"], query=str(task.get("goal") or ""), limit=6
            )
            if item["id"] != task_id
        ][:5]
        packet = {
            "task": {
                "id": task["id"],
                "goal": task.get("goal"),
                "summary": task.get("completion_summary"),
            },
            "tools": [
                {"name": item.get("tool_name"), "status": item.get("status"), "error": item.get("error")}
                for item in self.store.list_harness_records(
                    "harness_tool_calls", task["organization_id"], where={"task_id": task_id}, limit=30
                )
            ],
            "skills": [
                {"id": item["id"], "name": item.get("name"), "description": item.get("description"), "status": item.get("status")}
                for item in existing[:30]
            ],
            "related_tasks": related_tasks,
        }
        try:
            response = self.model.generate(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Review this completed Task only for reusable procedure. Prefer NO_CHANGE. "
                            "Do not invent steps absent from evidence. Existing Skills should be reused. "
                            "You can only propose a draft; never activate anything."
                        ),
                    },
                    {"role": "user", "content": json.dumps(packet, sort_keys=True)},
                ],
                response_schema=_CURATOR_SCHEMA,
                temperature=0.0,
                max_tokens=1200,
            )
            decision = json.loads(response.content)
            if decision.get("outcome") not in {
                "NO_CHANGE", "NEW_SKILL_DRAFT", "SKILL_UPDATE_DRAFT", "EXISTING_SKILL_COVERS_IT"
            }:
                raise ValueError("invalid curator outcome")
        except Exception as exc:
            decision = {
                "outcome": "NO_CHANGE",
                "reason": f"curator_unavailable:{type(exc).__name__}",
                "name": None,
                "description": None,
                "instructions": None,
                "existing_skill_id": None,
            }
        outcome = decision["outcome"]
        result: dict[str, Any] = {"task_id": task_id, **decision}
        if outcome == "NEW_SKILL_DRAFT":
            skill = self.skills.create(
                organization_id=task["organization_id"],
                name=str(decision.get("name") or task.get("goal") or "Learned procedure")[:120],
                description=str(decision.get("description") or decision["reason"]),
                goal=str(task.get("goal") or ""),
                steps=[{"name": "Procedure", "description": str(decision.get("instructions") or "")}],
                warnings=[f"Draft learned from Task {task_id}; review before activation."],
                status="draft",
                scope_id=task.get("origin_scope_id") or "scope_org_public",
                instructions=str(decision.get("instructions") or ""),
                origin={"type": "learned", "source_task_ids": [task_id]},
            )
            result["skill_id"] = skill["id"]
        elif outcome == "SKILL_UPDATE_DRAFT" and decision.get("existing_skill_id"):
            proposed = self.skills.propose_update(
                str(decision["existing_skill_id"]),
                {
                    "instructions": str(decision.get("instructions") or ""),
                    "origin": {"type": "learned_update", "source_task_ids": [task_id]},
                },
            )
            if proposed:
                result["skill_id"] = proposed["id"]
        self.audit.record(
            task["organization_id"],
            "skill.curator.reviewed",
            {**result, "reviewed_at": utcnow().isoformat()},
        )
        return result
