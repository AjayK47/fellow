"""Human-authored skill store and retrieval."""

from __future__ import annotations

from typing import Any

from docket.domain.models import (
    Skill,
    SkillActivation,
    SkillActivationMode,
    SkillResponseValidation,
    SkillStatus,
    SkillStep,
    new_id,
    utcnow,
)
from docket.services.audit import AuditService
from docket.storage.db import BrainStore


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
        activation_mode: str = "optional",
        activation: dict[str, Any] | None = None,
        response_validation: dict[str, Any] | None = None,
        platform_key: str | None = None,
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
            activation_mode=SkillActivationMode(activation_mode),
            activation=SkillActivation(**(activation or {})),
            response_validation=SkillResponseValidation(
                **(response_validation or {})
            ),
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
        """Install immutable platform skills required by the harness.

        Platform skills are capabilities of Docket itself, not company-authored
        procedures.  They must remain available even when a tenant has no
        organization-public memory scope yet.
        """
        existing = {
            skill.get("platform_key"): skill
            for skill in self.list(organization_id)
            if skill.get("platform_key")
        }
        slack_key = "destination.slack.communication"
        slack_name = "Write for Slack"
        if slack_key not in existing:
            existing[slack_key] = self.create(
                organization_id=organization_id,
                name=slack_name,
                description=(
                    "Compose user-facing responses for Slack public channels, "
                    "private channels, and DMs using native Slack conventions."
                ),
                goal=(
                    "Produce concise, readable Slack messages with valid mrkdwn, "
                    "links, citations, and channel-appropriate structure."
                ),
                steps=[
                    {
                        "order": 1,
                        "name": "Use Slack mrkdwn",
                        "description": (
                            "Use *bold*, _italic_, ~strike~, `inline code`, and "
                            "triple-backtick code blocks. Never emit Markdown "
                            "double-asterisk bold or Markdown headings."
                        ),
                    },
                    {
                        "order": 2,
                        "name": "Format links for Slack",
                        "description": (
                            "Write labeled links as <https://example.com|label>. "
                            "Never write Markdown links in [label](URL) form."
                        ),
                    },
                    {
                        "order": 3,
                        "name": "Optimize for chat",
                        "description": (
                            "Lead with the answer, keep paragraphs short, use "
                            "bullets for scanability, and avoid Markdown tables."
                        ),
                    },
                    {
                        "order": 4,
                        "name": "Present evidence clearly",
                        "description": (
                            "Place source links next to the claims they support. "
                            "Do not imply that a link supports a claim unless the "
                            "retrieved evidence actually does."
                        ),
                    },
                ],
                warnings=[
                    "Do not expose private evidence into a public channel.",
                    "Do not use standard Markdown link or bold syntax in Slack.",
                ],
                activation_mode="mandatory",
                activation={
                    "destination_types": [
                        "slack_channel",
                        "private_channel",
                        "dm",
                    ]
                },
                response_validation={
                    "enabled": True,
                    "instructions": (
                        "Confirm the response follows the Slack skill, especially "
                        "native mrkdwn and <URL|label> links; contains no Markdown "
                        "headings, tables, **bold**, or [label](URL) links; remains "
                        "concise and preserves every factual claim and citation."
                    ),
                },
                platform_key=slack_key,
                status="approved",
            )
        skill = existing[slack_key]
        if skill.get("status") != SkillStatus.APPROVED.value:
            raise RuntimeError(
                "The mandatory Slack communication skill is not approved."
            )
        # These are the small, tool-surface-safe procedures adapted from the
        # Hermes/Enclaw skill set.  They are intentionally concise because the
        # full procedure is injected only when relevant.
        builtin_specs = [
            (
                "builtin.hermes.systematic_debugging",
                "Investigate Before Fixing",
                "Investigate an issue from evidence before recommending a fix.",
                "Establish the symptom, gather evidence, identify the root cause, and propose the smallest verified next step.",
                ["Separate observed facts from assumptions.", "Check related history and evidence before concluding.", "State confidence and remaining unknowns."],
            ),
            (
                "builtin.hermes.deep_research",
                "Deep Research",
                "Research a question using multiple authoritative sources.",
                "Produce a concise, source-grounded research answer with uncertainty and citations.",
                ["Search broadly, then fetch primary sources.", "Cross-check important claims.", "Put source links next to the claims they support."],
            ),
            (
                "builtin.hermes.pdf_documents",
                "PDF and Document Handling",
                "Read, extract, summarize, and create document artifacts safely.",
                "Use the available document tools for PDFs and office files and preserve source fidelity.",
                ["Inspect the source before transforming it.", "Keep layout and citations intact where possible.", "Report the generated artifact and any limitations."],
            ),
            (
                "builtin.hermes.decision_communication",
                "Decision Communication",
                "Communicate decisions with clear options and tradeoffs.",
                "Recommend one option, explain the tradeoff, and give concrete next steps.",
                ["Use alternatives only when they are materially different.", "Make a direct recommendation.", "Mark actions that require human approval."],
            ),
        ]
        for key, name, description, goal, steps in builtin_specs:
            if key in existing:
                continue
            existing[key] = self.create(
                organization_id=organization_id,
                name=name,
                description=description,
                goal=goal,
                steps=[{"order": i + 1, "name": f"Step {i + 1}", "description": step} for i, step in enumerate(steps)],
                activation_mode="optional",
                platform_key=key,
                status="approved",
            )
        return [skill] + [existing[key] for key, *_ in builtin_specs]

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
    ) -> list[dict[str, Any]]:
        """Resolve and pin approved mandatory and relevant optional skills."""
        self.ensure_platform_skills(organization_id)
        destination_type = str(destination.get("type") or "")
        allowed = set(allowed_scope_ids)
        def skill_is_visible(skill: dict[str, Any]) -> bool:
            # Platform/default skills are harness capabilities, not tenant
            # memory.  Never hide them because a reset or a new tenant has not
            # materialized its organization-public scope yet.
            if skill.get("platform_key"):
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
        q = query.lower()
        skills = [
            s for s in self.store.list_skills(organization_id)
            if s.get("status") in allowed_status
        ]
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
        if not skill or skill.get("status") != SkillStatus.APPROVED.value:
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
        if skill.get("platform_key"):
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
        if skill.get("platform_key"):
            raise ValueError("platform skills cannot be deprecated")
        skill["status"] = SkillStatus.DEPRECATED.value
        skill["updated_at"] = utcnow().isoformat()
        self.store.upsert_skill(skill)
        self.store.upsert_skill_version(skill)
        self.audit.record(skill["organization_id"], "skill.deprecated", {"skill_id": skill_id, "version": skill["version"]})
        return skill
