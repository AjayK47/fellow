"""Human-authored skill store and retrieval."""

from __future__ import annotations

from typing import Any

from company_brain.domain.models import Skill, SkillStatus, SkillStep, new_id, utcnow
from company_brain.services.audit import AuditService
from company_brain.storage.db import BrainStore


class SkillsService:
    def __init__(self, store: BrainStore, audit: AuditService) -> None:
        self.store = store
        self.audit = audit

    def create(
        self,
        *,
        organization_id: str,
        name: str,
        description: str,
        goal: str,
        steps: list[dict[str, Any]] | None = None,
        owner_id: str | None = None,
        inputs: list[str] | None = None,
        preconditions: list[str] | None = None,
        warnings: list[str] | None = None,
        scope_id: str = "scope_org_public",
        status: str = "approved",
    ) -> dict[str, Any]:
        step_models = [
            SkillStep(
                order=s.get("order", i + 1),
                name=s["name"],
                description=s.get("description", ""),
                required=s.get("required", True),
                approvals=s.get("approvals") or [],
            )
            for i, s in enumerate(steps or [])
        ]
        skill = Skill(
            id=new_id("skill"),
            organization_id=organization_id,
            name=name,
            description=description,
            goal=goal,
            version=1,
            status=SkillStatus(status) if status in SkillStatus._value2member_map_ else SkillStatus.APPROVED,
            inputs=inputs or [],
            preconditions=preconditions or [],
            steps=step_models,
            warnings=warnings or [],
            owner_id=owner_id,
            scope_id=scope_id,
        )
        data = skill.model_dump(mode="json")
        self.store.upsert_skill(data)
        self.store.upsert_skill_version(data)
        self.audit.record(
            organization_id=organization_id,
            event_type="skill.created",
            payload={"skill_id": data["id"], "version": data["version"], "name": name},
        )
        return data

    def get(self, skill_id: str) -> dict[str, Any] | None:
        return self.store.get_skill(skill_id)

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.store.list_skills(organization_id)

    def search(self, organization_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = query.lower()
        skills = self.store.list_skills(organization_id)
        scored = []
        for s in skills:
            text = f"{s.get('name','')} {s.get('description','')} {s.get('goal','')}".lower()
            score = sum(1 for tok in q.split() if tok in text)
            if score or not q.strip():
                scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:limit]]

    def prepare(self, skill_id: str, context_warnings: list[str] | None = None) -> dict[str, Any] | None:
        skill = self.get(skill_id)
        if not skill:
            return None
        return {
            "skill": skill,
            "procedure_steps": skill.get("steps") or [],
            "warnings": list(skill.get("warnings") or []) + list(context_warnings or []),
            "status": skill.get("status"),
            "version": skill.get("version"),
        }

    def propose_update(
        self, skill_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        skill = self.get(skill_id)
        if not skill:
            return None
        new_version = dict(skill)
        new_version["version"] = int(skill.get("version") or 1) + 1
        new_version["status"] = SkillStatus.DRAFT.value
        new_version["updated_at"] = utcnow().isoformat()
        for k, v in changes.items():
            if k in {"name", "description", "goal", "steps", "warnings", "inputs", "preconditions"}:
                new_version[k] = v
        # A proposed version is immutable history; it does not replace the approved current skill.
        self.store.upsert_skill_version(new_version)
        self.audit.record(
            organization_id=skill["organization_id"],
            event_type="skill.propose_update",
            payload={"skill_id": skill_id, "version": new_version["version"]},
        )
        return new_version

    def versions(self, skill_id: str) -> list[dict[str, Any]]:
        return self.store.list_skill_versions(skill_id)

    def approve(self, skill_id: str, version: int) -> dict[str, Any] | None:
        proposed = self.store.get_skill_version(skill_id, version)
        if not proposed:
            return None
        approved = dict(proposed)
        approved["status"] = SkillStatus.APPROVED.value
        approved["updated_at"] = utcnow().isoformat()
        self.store.upsert_skill_version(approved)
        self.store.upsert_skill(approved)
        self.audit.record(approved["organization_id"], "skill.approved", {"skill_id": skill_id, "version": version})
        return approved

    def deprecate(self, skill_id: str) -> dict[str, Any] | None:
        skill = self.get(skill_id)
        if not skill:
            return None
        skill["status"] = SkillStatus.DEPRECATED.value
        skill["updated_at"] = utcnow().isoformat()
        self.store.upsert_skill(skill)
        self.store.upsert_skill_version(skill)
        self.audit.record(skill["organization_id"], "skill.deprecated", {"skill_id": skill_id, "version": skill["version"]})
        return skill
