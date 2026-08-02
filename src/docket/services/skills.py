"""Human-authored skill store and retrieval."""

from __future__ import annotations

from typing import Any

from docket.domain.models import (
    Skill,
    SkillActivation,
    SkillActivationMode,
    SkillKind,
    SkillResponseValidation,
    SkillStatus,
    SkillStep,
    new_id,
    utcnow,
)
from docket.services.audit import AuditService
from docket.storage.db import BrainStore
from docket.services.skill_packages import SkillPackageCatalog


class SkillsService:
    def __init__(
        self,
        store: BrainStore,
        audit: AuditService,
        package_catalog: SkillPackageCatalog | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.package_catalog = package_catalog or SkillPackageCatalog()

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
        activation_mode: str = "optional",
        activation: dict[str, Any] | None = None,
        response_validation: dict[str, Any] | None = None,
        platform_key: str | None = None,
        scope_id: str = "scope_org_public",
        status: str = "approved",
        instructions: str = "",
        kind: str = "company",
        immutable: bool = False,
        content_hash: str = "",
        package_path: str = "",
        license: str = "unspecified",
        origin: dict[str, Any] | None = None,
        required_capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind != SkillKind.COMPANY.value or immutable:
            raise ValueError("only mutable company skills may be created through this API")
        validation = self.validate_definition(
            {
                "name": name,
                "description": description,
                "goal": goal,
                "steps": steps or [],
                "warnings": warnings or [],
                "instructions": instructions,
            }
        )
        if not validation["valid"]:
            raise ValueError("invalid skill definition: " + "; ".join(validation["errors"]))
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
            activation_mode=SkillActivationMode(activation_mode),
            activation=SkillActivation(**(activation or {})),
            response_validation=SkillResponseValidation(
                **(response_validation or {})
            ),
            instructions=instructions,
            kind=SkillKind(kind) if kind in SkillKind._value2member_map_ else SkillKind.COMPANY,
            immutable=immutable,
            content_hash=content_hash,
            package_path=package_path,
            license=license,
            origin=origin or {},
            required_capabilities=required_capabilities or {},
            platform_key=platform_key,
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

    def ensure_platform_skills(self, organization_id: str) -> list[dict[str, Any]]:
        """Materialize immutable package skills into the tenant index.

        The package files are authoritative.  SQLite only holds a searchable,
        tenant-aware projection so reset/deletion of company data cannot remove
        the harness procedures.
        """
        existing = {
            skill.get("platform_key"): skill
            for skill in self.list(organization_id)
            if skill.get("platform_key")
        }
        materialized: list[dict[str, Any]] = []
        for package in self.package_catalog.list():
            manifest = package.manifest
            platform_key = manifest.platform_key or f"{manifest.kind}.{manifest.name}"
            old = existing.get(platform_key)
            skill_id = old.get("id") if old else new_id("skill_builtin")
            data = package.as_record(organization_id=organization_id, skill_id=skill_id)
            if old and old.get("created_at"):
                data["created_at"] = old["created_at"]
            else:
                data["created_at"] = utcnow().isoformat()
            data["updated_at"] = utcnow().isoformat()
            if not old or old.get("content_hash") != data.get("content_hash"):
                self.audit.record(
                    organization_id,
                    "skill.package.materialized",
                    {
                        "name": manifest.name,
                        "skill_id": skill_id,
                        "content_hash": data.get("content_hash"),
                        "kind": manifest.kind,
                    },
                )
            self.store.upsert_skill(data)
            self.store.upsert_skill_version(data)
            materialized.append(data)
        slack = next(
            (skill for skill in materialized if skill.get("platform_key") == "destination.slack.communication"),
            None,
        )
        if slack is None:
            raise RuntimeError("The mandatory Slack communication package is missing")
        if slack.get("status") != SkillStatus.APPROVED.value:
            raise RuntimeError("The mandatory Slack communication skill is not approved")
        return materialized

    def resolve_for_run(
        self,
        *,
        organization_id: str,
        task: str,
        destination: dict[str, Any],
        trigger_kind: str,
        purpose: str,
        allowed_scope_ids: list[str],
        optional_limit: int = 3,
        available_capabilities: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve and pin approved mandatory and relevant optional skills."""
        self.ensure_platform_skills(organization_id)
        destination_type = str(destination.get("type") or "")
        allowed = set(allowed_scope_ids)
        def skill_is_visible(skill: dict[str, Any]) -> bool:
            # Platform/default skills are harness capabilities, not tenant
            # memory.  Never hide them because a reset or a new tenant has not
            # materialized its organization-public scope yet.
            if skill.get("platform_key") or skill.get("immutable") or skill.get("kind") in {"builtin", "connector"}:
                return True
            scope_id = skill.get("scope_id")
            if not scope_id or scope_id in allowed:
                return True
            scope = self.store.get_scope(scope_id)
            return bool(scope and scope.get("is_public"))

        approved = [
            skill
            for skill in self.list(organization_id)
            if skill.get("status") == SkillStatus.APPROVED.value
            and skill_is_visible(skill)
        ]

        capabilities = available_capabilities

        def ready(skill: dict[str, Any]) -> bool:
            if capabilities is None:
                return True
            required = skill.get("required_capabilities") or {}
            all_of = {str(item) for item in required.get("all_of", [])}
            any_of = {str(item) for item in required.get("any_of", [])}
            missing = all_of - capabilities
            has_any = not any_of or bool(any_of & capabilities)
            if missing or not has_any:
                if skill.get("activation_mode") == SkillActivationMode.MANDATORY.value:
                    raise RuntimeError(
                        f"Mandatory skill {skill.get('name')} is not ready: "
                        f"missing {sorted(missing or any_of)}"
                    )
                return False
            return True

        def applies(skill: dict[str, Any]) -> bool:
            activation = skill.get("activation") or {}
            destinations = set(activation.get("destination_types") or [])
            triggers = set(activation.get("trigger_kinds") or [])
            purposes = set(activation.get("purposes") or [])
            return (
                (not destinations or destination_type in destinations)
                and (not triggers or trigger_kind in triggers)
                and (not purposes or purpose in purposes)
            )

        mandatory = [
            skill
            for skill in approved
            if skill.get("activation_mode") == SkillActivationMode.MANDATORY.value
            and applies(skill)
            and ready(skill)
        ]
        mandatory_ids = {skill["id"] for skill in mandatory}
        relevant = self.search(
            organization_id,
            task,
            limit=max(optional_limit * 3, 10),
        )
        approved_by_id = {skill["id"]: skill for skill in approved}
        optional = [
            approved_by_id[skill["id"]]
            for skill in relevant
            if skill["id"] in approved_by_id
            and skill["id"] not in mandatory_ids
            and skill.get("activation_mode", "optional") == "optional"
            and applies(approved_by_id[skill["id"]])
            and ready(approved_by_id[skill["id"]])
        ][:optional_limit]
        return [
            self._active_skill(skill, reason="destination_mandatory")
            for skill in mandatory
        ] + [
            self._active_skill(skill, reason="task_relevant")
            for skill in optional
        ]

    @staticmethod
    def _active_skill(skill: dict[str, Any], *, reason: str) -> dict[str, Any]:
        return {
            "id": skill["id"],
            "version": skill.get("version", 1),
            "name": skill.get("name"),
            "description": skill.get("description"),
            "goal": skill.get("goal"),
            "steps": skill.get("steps") or [],
            "warnings": skill.get("warnings") or [],
            "activation_mode": skill.get("activation_mode", "optional"),
            "activation_reason": reason,
            "response_validation": skill.get("response_validation") or {},
            "platform_key": skill.get("platform_key"),
            "instructions": skill.get("instructions") or "",
            "kind": skill.get("kind", "company"),
            "immutable": bool(skill.get("immutable")),
            "content_hash": skill.get("content_hash", ""),
            "required_capabilities": skill.get("required_capabilities") or {},
            "origin": skill.get("origin") or {},
        }

    def get(self, skill_id: str) -> dict[str, Any] | None:
        return self.store.get_skill(skill_id)

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.store.list_skills(organization_id)

    def search(
        self,
        organization_id: str,
        query: str,
        limit: int = 20,
        *,
        include_status: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find skills relevant to a query.

        Defaults to approved skills only — draft and deprecated versions are
        proposals or retired procedures, not something a run should treat as
        authoritative guidance. Pass include_status explicitly (e.g. for the
        admin console) to see other statuses.
        """
        allowed_status = include_status or {SkillStatus.APPROVED.value}
        stop_words = {"a", "an", "and", "before", "for", "in", "is", "it", "of", "on", "or", "the", "to"}
        q = query.lower()
        skills = [
            s for s in self.store.list_skills(organization_id)
            if s.get("status") in allowed_status
        ]
        scored = []
        for s in skills:
            text = f"{s.get('name','')} {s.get('description','')} {s.get('goal','')}".lower()
            score = sum(1 for tok in q.split() if tok not in stop_words and len(tok) > 1 and tok in text)
            if score or not q.strip():
                scored.append((score, s))
        scored.sort(
            key=lambda x: (
                x[0],
                1 if x[1].get("kind") == "company" else 0,
                1 if x[1].get("immutable") else 0,
            ),
            reverse=True,
        )
        return [s for _, s in scored[:limit]]

    def prepare(self, skill_id: str, context_warnings: list[str] | None = None) -> dict[str, Any] | None:
        skill = self.get(skill_id)
        if not skill or skill.get("status") != SkillStatus.APPROVED.value:
            return None
        return {
            "skill": skill,
            "procedure_steps": skill.get("steps") or [],
            "warnings": list(skill.get("warnings") or []) + list(context_warnings or []),
            "status": skill.get("status"),
            "version": skill.get("version"),
        }

    @staticmethod
    def validate_definition(definition: dict[str, Any]) -> dict[str, Any]:
        """Deterministically preview a company skill before creating a draft."""
        errors: list[str] = []
        warnings: list[str] = []
        for field in ("name", "description", "goal"):
            if not str(definition.get(field) or "").strip():
                errors.append(f"{field} is required")
        if definition.get("activation_mode") not in {None, "mandatory", "optional"}:
            errors.append("activation_mode must be mandatory or optional")
        for index, step in enumerate(definition.get("steps") or [], 1):
            if not step.get("name") or not step.get("description"):
                errors.append(f"steps[{index}] requires name and description")
        text = " ".join(str(definition.get(key) or "") for key in ("description", "goal", "instructions"))
        if any(marker in text.lower() for marker in ("ignore previous instructions", "system override", "reveal the system prompt")):
            errors.append("skill text contains a prompt-injection marker")
        if not definition.get("warnings"):
            warnings.append("add explicit warnings for privacy, approvals, and failure handling")
        if not definition.get("response_validation"):
            warnings.append("consider a response validation contract for user-facing output")
        return {"valid": not errors, "errors": errors, "warnings": warnings}

    def propose_update(
        self, skill_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        skill = self.get(skill_id)
        if not skill:
            return None
        if skill.get("platform_key") or skill.get("immutable") or skill.get("kind") in {"builtin", "connector"}:
            raise ValueError("platform skills are immutable")
        new_version = dict(skill)
        new_version["version"] = int(skill.get("version") or 1) + 1
        new_version["status"] = SkillStatus.DRAFT.value
        new_version["updated_at"] = utcnow().isoformat()
        for k, v in changes.items():
            if k in {
                "name",
                "description",
                "goal",
                "steps",
                "warnings",
                "inputs",
                "preconditions",
                "activation_mode",
                "activation",
                "response_validation",
            }:
                new_version[k] = v
        validation = self.validate_definition(new_version)
        if not validation["valid"]:
            raise ValueError("invalid skill update: " + "; ".join(validation["errors"]))
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
        current = self.get(skill_id)
        if current and (current.get("immutable") or current.get("kind") in {"builtin", "connector"}):
            raise ValueError("platform skills are immutable")
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
        if skill.get("platform_key") or skill.get("immutable") or skill.get("kind") in {"builtin", "connector"}:
            raise ValueError("platform skills cannot be deprecated")
        skill["status"] = SkillStatus.DEPRECATED.value
        skill["updated_at"] = utcnow().isoformat()
        self.store.upsert_skill(skill)
        self.store.upsert_skill_version(skill)
        self.audit.record(skill["organization_id"], "skill.deprecated", {"skill_id": skill_id, "version": skill["version"]})
        return skill
