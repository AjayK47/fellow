"""Human-authored skill store and retrieval."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any
import re

from toda.domain.models import (
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
from toda.services.audit import AuditService
from toda.storage.db import BrainStore
from toda.services.skill_packages import SkillPackageCatalog

# Generic conversational/request vocabulary that carries no topical signal
# on its own ("can you look into...", "please make me..."). Without
# filtering these, a message's incidental overlap with them can outscore a
# skill's genuine, on-topic match -- this is exactly how "OCR and
# Documents"/"YouTube Content" out-ranked "PDF and Document Handling" for
# an economic-report request that never used any word specific to either.
_SKILL_SEARCH_STOP_WORDS = {
    "a", "an", "and", "as", "at", "before", "but", "by", "for", "from", "if",
    "in", "into", "is", "it", "its", "of", "on", "or", "so", "that", "the",
    "this", "to", "with", "without",
    "i", "me", "my", "we", "us", "our", "you", "your", "yours", "he", "him",
    "his", "she", "her", "they", "them", "their", "some", "any", "each",
    "every", "all", "one", "ones",
    "can", "could", "would", "should", "will", "shall", "do", "does", "did",
    "have", "has", "had", "be", "am", "are", "was", "were", "been", "being",
    "get", "got", "give", "gave", "make", "made", "look", "looking", "want",
    "wanted", "need", "needed", "please", "thanks", "thank", "cool", "ok",
    "okay", "just", "also", "like", "think", "know", "let", "lets", "help",
    "new", "good", "great", "nice", "hey", "hi", "hello",
}
# Fuzzy (typo-tolerant) matching only kicks in for longer tokens -- a short
# word like "war" or "you" is within edit-distance of plenty of unrelated
# short words, so fuzzy-matching it would reintroduce the same noise the
# stopword list just removed. A word long enough to need this ("rpeort",
# "econmic") carries enough real signal for the comparison to be safe.
_SKILL_SEARCH_FUZZY_MIN_TOKEN_LEN = 5
_SKILL_SEARCH_FUZZY_THRESHOLD = 0.8


def _skill_search_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _skill_query_tokens(query: str) -> list[str]:
    return [
        tok
        for tok in re.findall(r"[a-z0-9]+", query.lower())
        if tok not in _SKILL_SEARCH_STOP_WORDS and len(tok) > 2
    ]


def _skill_token_match_score(query_token: str, text_tokens: set[str]) -> float:
    """1.0 for an exact match; a fuzzy ratio for a likely typo in a long
    enough token; 0.0 otherwise (never a partial-credit substring match --
    that's what let "an" inside "many" or "art" inside "start" through
    before)."""
    if query_token in text_tokens:
        return 1.0
    if len(query_token) < _SKILL_SEARCH_FUZZY_MIN_TOKEN_LEN:
        return 0.0
    best = 0.0
    for candidate in text_tokens:
        if abs(len(candidate) - len(query_token)) > 2:
            continue
        ratio = SequenceMatcher(None, query_token, candidate).ratio()
        if ratio > best:
            best = ratio
    return best if best >= _SKILL_SEARCH_FUZZY_THRESHOLD else 0.0


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
        status: str = "active",
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
        required = required_capabilities or {}
        for group in ("all_of", "any_of"):
            values = required.get(group, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_.-]{1,120}", value)
                for value in values
            ):
                raise ValueError("required capabilities must be stable capability IDs")
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
            status=SkillStatus(status) if status in SkillStatus._value2member_map_ else SkillStatus.ACTIVE,
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
            package_now = utcnow().isoformat()
            self.store.upsert_harness_projection(
                "skill_packages",
                {
                    "id": f"package:{manifest.name}",
                    "organization_id": organization_id,
                    "status": manifest.security_verdict,
                    "package_path": package.package_path,
                    "name": manifest.name,
                    "version": manifest.version,
                    "kind": manifest.kind,
                    "content_hash": package.content_hash,
                    "origin": manifest.origin,
                    "license": manifest.license,
                    "security_findings": manifest.security_findings,
                    "created_at": package_now,
                    "updated_at": package_now,
                },
            )
            if manifest.security_verdict == "block":
                raise RuntimeError(f"immutable skill package is blocked: {manifest.name}")
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
        if slack.get("status") != SkillStatus.ACTIVE.value:
            raise RuntimeError("The mandatory Slack communication skill is not approved")
        return materialized

    def resolve_for_task(
        self,
        *,
        organization_id: str,
        task: str,
        destination: dict[str, Any],
        trigger_kind: str,
        purpose: str,
        allowed_scope_ids: list[str],
        optional_limit: int = 4,
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
            if skill.get("status") == SkillStatus.ACTIVE.value
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
        # Optional Skill bodies stay out of the base prompt. The compact
        # catalog is always present and the model loads a relevant body with
        # skill_read. Only mandatory operating constraints are preloaded.
        return [
            self._active_skill(skill, reason="destination_mandatory")
            for skill in mandatory
        ]

    def index_for_run(
        self,
        *,
        organization_id: str,
        task: str,
        allowed_scope_ids: list[str],
        available_capabilities: set[str] | None = None,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        """Return compact metadata (name/description only, not full
        content) for progressive-disclosure prompting -- ported from
        Hermes's build_skills_system_prompt(): show the *entire* compatible
        skill catalog as a lightweight index and let the model itself
        decide what's relevant and pull in the full content via
        skill_read/skill_list, rather than a server-side keyword scorer
        silently deciding what the model never even gets to see. A keyword
        matcher can't tell "PDF and Document Handling" is the right skill
        for a report request that never uses the word "PDF" and misspells
        "report" -- the model reading a real description can. `task` is
        unused now (kept for signature compatibility) since nothing here
        pre-filters by relevance anymore; resolve_for_task's small
        mandatory+optional auto-load selection still uses search() as a
        genuine optimization (skip a manual skill_read round-trip for the
        common case), it just isn't the only way a skill can be found."""
        self.ensure_platform_skills(organization_id)
        allowed = set(allowed_scope_ids)
        ready_caps = available_capabilities
        records: list[dict[str, Any]] = []
        catalog = sorted(
            self.list(organization_id),
            key=lambda s: (
                0 if s.get("kind") == "company" else 1,
                0 if s.get("immutable") else 1,
                str(s.get("name") or ""),
            ),
        )
        for skill in catalog:
            if len(records) >= limit:
                break
            if skill.get("status") != SkillStatus.ACTIVE.value:
                continue
            if not (
                skill.get("platform_key")
                or skill.get("immutable")
                or skill.get("kind") in {"builtin", "connector"}
                or not skill.get("scope_id")
                or skill.get("scope_id") in allowed
                or bool((self.store.get_scope(skill.get("scope_id")) or {}).get("is_public"))
            ):
                continue
            required = skill.get("required_capabilities") or {}
            all_of = {str(value) for value in required.get("all_of", [])}
            any_of = {str(value) for value in required.get("any_of", [])}
            ready = (
                ready_caps is None
                or (all_of <= ready_caps and (not any_of or bool(any_of & ready_caps)))
            )
            records.append(
                {
                    "id": skill.get("id"),
                    "name": skill.get("name"),
                    "description": skill.get("description"),
                    "version": skill.get("version", 1),
                    "kind": skill.get("kind", "company"),
                    "immutable": bool(skill.get("immutable")),
                    "activation_mode": skill.get("activation_mode", "optional"),
                    "activation": skill.get("activation") or {},
                    "required_capabilities": required,
                    "readiness": "ready" if ready else "setup_needed",
                    "content_hash": skill.get("content_hash", ""),
                    "origin": skill.get("origin") or {},
                }
            )
            if len(records) >= limit:
                break
        return records

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
        allowed_status = include_status or {SkillStatus.ACTIVE.value}
        skills = [
            s for s in self.store.list_skills(organization_id)
            if s.get("status") in allowed_status
        ]
        query_tokens = _skill_query_tokens(query)
        if not query_tokens:
            # No scoreable signal (empty query, or pure stopwords) --
            # preserve "browse everything" rather than returning nothing.
            scored = [(0.0, s) for s in skills]
        else:
            scored = []
            for s in skills:
                # name/description are the curated, authoritative relevance
                # signal; goal is a longer free-text field weighted lower so
                # it can't drown out a precise description match with
                # incidental word overlap.
                primary_tokens = _skill_search_tokens(
                    f"{s.get('name','')} {s.get('description','')}"
                )
                secondary_tokens = _skill_search_tokens(s.get("goal", ""))
                score = sum(
                    2.0 * _skill_token_match_score(tok, primary_tokens)
                    + 1.0 * _skill_token_match_score(tok, secondary_tokens)
                    for tok in query_tokens
                )
                if score > 0:
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
        if not skill or skill.get("status") != SkillStatus.ACTIVE.value:
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
        findings = SkillPackageCatalog.scan_text(text)
        if findings:
            errors.append(
                "skill text failed static security scan: "
                + ", ".join(item["rule"] for item in findings)
            )
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
                "instructions",
                "origin",
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
        approved["status"] = SkillStatus.ACTIVE.value
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
        skill["status"] = SkillStatus.DISABLED.value
        skill["updated_at"] = utcnow().isoformat()
        self.store.upsert_skill(skill)
        self.store.upsert_skill_version(skill)
        self.audit.record(skill["organization_id"], "skill.deprecated", {"skill_id": skill_id, "version": skill["version"]})
        return skill
