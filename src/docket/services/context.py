"""Structured context package construction with token budgeting."""

from __future__ import annotations

from typing import Any

from docket.domain.models import ContextPackage, Destination, utcnow
from docket.services.audit import AuditService
from docket.services.graph import GraphService
from docket.services.retrieval import RetrievalService
from docket.storage.db import BrainStore


def _estimate_tokens(text: str) -> int:
    # rough heuristic ~4 chars/token
    return max(1, len(text) // 4) if text else 0


class ContextService:
    def __init__(
        self,
        store: BrainStore,
        retrieval: RetrievalService,
        graph: GraphService,
        audit: AuditService,
    ) -> None:
        self.store = store
        self.retrieval = retrieval
        self.graph = graph
        self.audit = audit

    def prepare_context(
        self,
        *,
        organization_id: str,
        task: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
        entity_ids: list[str] | None = None,
        token_budget: int = 8000,
        agent_id: str | None = None,
        conversation: dict[str, Any] | None = None,
    ) -> ContextPackage:
        search = self.retrieval.search(
            organization_id=organization_id,
            query=task,
            acting_principal_id=acting_principal_id,
            destination=destination,
            purpose=purpose,
            entity_ids=entity_ids,
            limit=50,
        )
        decision_id = search["permission_decision_id"]
        results = search["results"]

        current_facts: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        commitments: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        contradictions: list[dict[str, Any]] = []
        entities_out: list[dict[str, Any]] = []
        people: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []

        for fact in search.get("reality_facts") or []:
            current_facts.append(
                {
                    "id": fact["id"],
                    "content": (
                        f"{fact['subject_id']} {fact['predicate']} "
                        f"{fact['object_value']}"
                    ),
                    "subject_id": fact["subject_id"],
                    "predicate": fact["predicate"],
                    "object_value": fact["object_value"],
                    "confidence": fact.get("confidence"),
                    "authority": fact.get("authority"),
                    "source_ids": fact.get("source_ids"),
                    "valid_from": fact.get("valid_from"),
                    "valid_to": fact.get("valid_to"),
                    "system_from": fact.get("system_from"),
                    "status": fact.get("status"),
                    "score": fact.get("score"),
                }
            )
        if self.retrieval.reality:
            for conflict in self.retrieval.reality.list_conflicts(
                organization_id,
                scope_ids=search.get("allowed_scope_ids") or [],
            )[:20]:
                contradictions.append(
                    {
                        "id": conflict["id"],
                        "subject_id": conflict["subject_id"],
                        "predicate": conflict["predicate"],
                        "objects": conflict.get("objects") or [],
                        "status": conflict["status"],
                        "kind": conflict["kind"],
                        "resolution": conflict.get("resolution"),
                    }
                )

        seen_source: set[str] = set()
        for r in results:
            mem = r["memory"]
            item = {
                "id": mem["id"],
                "content": mem["content"],
                "confidence": mem.get("confidence"),
                "authority": mem.get("authority"),
                "source_ids": mem.get("source_ids"),
                "entity_ids": mem.get("entity_ids"),
                "valid_from": mem.get("valid_from"),
                "score": r["score"],
            }
            mtype = mem.get("memory_type")
            if mtype == "fact":
                current_facts.append(item)
            elif mtype == "decision":
                decisions.append(item)
            elif mtype == "commitment":
                commitments.append(item)
            elif mtype == "issue":
                issues.append(item)
            elif mtype == "outcome":
                outcomes.append(item)
            else:
                current_facts.append(item)

            for s in r.get("sources") or []:
                if s["id"] not in seen_source:
                    seen_source.add(s["id"])
                    sources.append(s)

            # contradictions: superseded claims linked by UPDATES
            for older in self.graph.memory_history(organization_id, mem["id"]):
                if older["id"] != mem["id"] and not older.get("is_latest"):
                    contradictions.append(
                        {
                            "current_claim": mem.get("content"),
                            "superseded_claim": older.get("content"),
                            "valid_until": older.get("valid_until") or older.get("superseded_at"),
                        }
                    )

        # entities
        eids = list(entity_ids or [])
        for r in results:
            for eid in r["memory"].get("entity_ids") or []:
                if eid not in eids:
                    eids.append(eid)
        for eid in eids[:20]:
            ent = self.store.get_entity(eid)
            if ent:
                entities_out.append(
                    {
                        "id": ent["id"],
                        "type": ent.get("entity_type"),
                        "name": ent.get("name"),
                    }
                )
                if ent.get("entity_type") == "person":
                    people.append({"id": ent["id"], "name": ent.get("name")})
            for edge in self.graph.neighbors(organization_id, eid)[:10]:
                if edge not in relationships:
                    relationships.append(edge)

        raw_search = self.retrieval.search_evidence(
            organization_id=organization_id,
            query=task,
            acting_principal_id=acting_principal_id,
            destination=destination,
            purpose=purpose,
            limit=12,
        )
        related_messages = raw_search["results"]

        task_tokens = {
            token.lower()
            for token in task.replace("_", " ").split()
            if len(token) > 2
        }
        relevant_skills = []
        for skill in self.store.list_skills(organization_id):
            if skill.get("status") != "approved":
                continue
            haystack = " ".join(
                [
                    skill.get("name") or "",
                    skill.get("description") or "",
                    skill.get("goal") or "",
                ]
            ).lower()
            score = sum(1 for token in task_tokens if token in haystack)
            if score:
                relevant_skills.append(
                    {
                        "id": skill["id"],
                        "name": skill.get("name"),
                        "goal": skill.get("goal"),
                        "version": skill.get("version"),
                        "score": score,
                    }
                )
        relevant_skills.sort(key=lambda skill: skill["score"], reverse=True)

        active_runs: list[dict[str, Any]] = []
        session_id = (conversation or {}).get("session_id")
        if session_id:
            active = self.store.active_run_for_session(session_id)
            if active:
                active_runs.append(
                    {
                        "id": active["id"],
                        "state": active["state"],
                        "goal": active["goal"],
                        "trigger_kind": active["trigger_kind"],
                    }
                )

        active_cases: list[dict[str, Any]] = []
        judgment_guidance: list[dict[str, Any]] = []
        if self.retrieval.reality:
            relevant_entity_ids = set(eids)
            for case in self.retrieval.reality.list_cases(
                organization_id,
                statuses={"open", "investigating"},
                scope_ids=search.get("allowed_scope_ids") or [],
            ):
                if not relevant_entity_ids or relevant_entity_ids.intersection(
                    case.get("entity_ids") or []
                ):
                    active_cases.append(
                        {
                            "id": case["id"],
                            "title": case["title"],
                            "status": case["status"],
                            "case_type": case["case_type"],
                            "entity_ids": case.get("entity_ids") or [],
                            "source_ids": case.get("source_ids") or [],
                        }
                    )
            judgment_guidance = self.retrieval.reality.judgment_guidance(
                organization_id, acting_principal_id, query=task
            )

        # Budget by priority (spec §8.11)
        ordered_sections: list[tuple[str, list[dict[str, Any]]]] = [
            ("current_facts", current_facts),
            ("issues", issues),
            ("decisions", decisions),
            ("open_commitments", commitments),
            ("outcomes", outcomes),
            ("sources", sources),
            ("contradictions", contradictions),
            ("entities", entities_out),
            ("related_messages", related_messages),
            ("relationships", relationships),
            ("skills", relevant_skills),
        ]

        budgeted: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in ordered_sections}
        used = 0
        summary_bits: list[str] = []
        for key, items in ordered_sections:
            for item in items:
                cost = _estimate_tokens(str(item.get("content") or item.get("name") or item))
                if used + cost > token_budget:
                    continue
                budgeted[key].append(item)
                used += cost
                if key in {"current_facts", "issues", "decisions"} and item.get("content"):
                    summary_bits.append(str(item["content"])[:120])

        package = ContextPackage(
            permission_decision_id=decision_id,
            viewer={
                "person_id": acting_principal_id,
                "organization_id": organization_id,
            },
            destination=(
                destination.model_dump(mode="json")
                if isinstance(destination, Destination)
                else destination or {}
            ),
            task={
                "goal": task,
                "purpose": purpose,
                "agent_id": agent_id,
            },
            summary="; ".join(summary_bits[:3]) if summary_bits else "No permission-safe evidence available in this context.",
            entities=budgeted["entities"],
            people=people[:15],
            relationships=budgeted["relationships"][:20],
            current_facts=budgeted["current_facts"],
            decisions=budgeted["decisions"],
            open_commitments=budgeted["open_commitments"],
            issues=budgeted["issues"],
            outcomes=budgeted["outcomes"],
            skills=budgeted["skills"][:10],
            recent_events=budgeted["related_messages"][:8],
            related_messages=budgeted["related_messages"][:12],
            active_runs=active_runs,
            active_cases=active_cases[:10],
            judgment_guidance=judgment_guidance[:10],
            warnings=[],
            contradictions=budgeted["contradictions"][:10],
            sources=budgeted["sources"],
            uncertainties=[] if summary_bits else ["No authorized matching knowledge found."],
            freshness={"as_of": utcnow().isoformat()},
            token_estimate=used,
            token_budget=token_budget,
        )

        self.audit.record(
            organization_id=organization_id,
            event_type="context.prepared",
            principal_id=acting_principal_id,
            payload={
                "request_id": package.request_id,
                "permission_decision_id": decision_id,
                "task": task,
                "agent_id": agent_id,
                "entity_ids": entity_ids,
                "token_estimate": used,
                "token_budget": token_budget,
                "fact_count": len(package.current_facts),
                "issue_count": len(package.issues),
                "summary": package.summary,
            },
        )
        return package

    def relationship_context(
        self,
        *,
        organization_id: str,
        from_entity_id: str,
        to_entity_id: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        decision = self.retrieval.authorize(
            organization_id, acting_principal_id, destination, purpose
        )
        paths = self.graph.paths_between(organization_id, from_entity_id, to_entity_id)
        shared_entity_ids = {from_entity_id, to_entity_id}
        for path in paths:
            for e in path:
                shared_entity_ids.add(e["from_id"])
                shared_entity_ids.add(e["to_id"])

        mems = self.graph.current_memories(organization_id)
        mems = self.retrieval.authz.filter_memories(decision, mems)

        shared_decisions = []
        open_commitments = []
        for m in mems:
            participants = set(m.get("participant_ids") or [])
            entities = set(m.get("entity_ids") or [])
            involves_both = (
                {from_entity_id, to_entity_id}.issubset(participants)
                or from_entity_id in participants
                and to_entity_id in entities
                or to_entity_id in participants
                and from_entity_id in entities
                or bool(entities & shared_entity_ids)
                and (from_entity_id in entities or to_entity_id in entities)
            )
            # shared project paths
            shared_projects = any(
                e.get("edge_type") == "WORKS_ON" for path in paths for e in path
            )
            if m.get("memory_type") == "decision" and (involves_both or shared_projects):
                # both work on something related
                if from_entity_id in (m.get("participant_ids") or []) or to_entity_id in (
                    m.get("participant_ids") or []
                ) or entities & shared_entity_ids:
                    shared_decisions.append(m)
            if m.get("memory_type") == "commitment" and involves_both:
                open_commitments.append(m)

        # also include decisions about shared projects
        project_ids = set()
        for path in paths:
            for e in path:
                if e.get("edge_type") == "WORKS_ON":
                    project_ids.add(e["to_id"] if not e.get("_reversed") else e["from_id"])
        for m in mems:
            if m.get("memory_type") == "decision" and project_ids.intersection(m.get("entity_ids") or []):
                if m not in shared_decisions:
                    shared_decisions.append(m)
            if m.get("memory_type") == "commitment" and (
                from_entity_id in (m.get("participant_ids") or [])
                or to_entity_id in (m.get("participant_ids") or [])
            ):
                if m not in open_commitments:
                    open_commitments.append(m)

        payload = {
            "from_entity_id": from_entity_id,
            "to_entity_id": to_entity_id,
            "graph_paths": paths,
            "shared_decisions": [
                {"id": m["id"], "content": m["content"], "source_ids": m.get("source_ids")}
                for m in shared_decisions[:20]
            ],
            "open_commitments": [
                {"id": m["id"], "content": m["content"], "source_ids": m.get("source_ids")}
                for m in open_commitments[:20]
            ],
            "permission_decision_id": decision.id,
        }
        self.audit.record(
            organization_id=organization_id,
            event_type="relationship.queried",
            principal_id=acting_principal_id,
            payload={
                "from_entity_id": from_entity_id,
                "to_entity_id": to_entity_id,
                "permission_decision_id": decision.id,
                "decision_count": len(shared_decisions),
                "commitment_count": len(open_commitments),
            },
        )
        return payload

    def resolve_owner(
        self,
        *,
        organization_id: str,
        issue_text: str,
        acting_principal_id: str,
        entity_ids: list[str] | None = None,
        include_on_call: bool = True,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        decision = self.retrieval.authorize(
            organization_id, acting_principal_id, destination, purpose
        )
        entity_ids = list(entity_ids or [])

        # expand via search if no entities
        if not entity_ids:
            search = self.retrieval.search(
                organization_id=organization_id,
                query=issue_text,
                acting_principal_id=acting_principal_id,
                destination=destination,
                purpose=purpose,
                limit=10,
            )
            for r in search["results"]:
                for eid in r["memory"].get("entity_ids") or []:
                    if eid not in entity_ids:
                        entity_ids.append(eid)

        owners = self.graph.resolve_owners(organization_id, entity_ids)
        evidence: list[dict[str, Any]] = []
        primary = None
        team = None
        on_call = None

        for o in owners:
            edge = o.get("edge") or {}
            owner = o.get("owner") or {}
            if not primary and owner:
                primary = {
                    "entity_id": owner.get("id"),
                    "reason": f"Current owner via {edge.get('edge_type')}",
                    "confidence": 0.95,
                    "name": owner.get("name"),
                }
            evidence.append(
                {
                    "type": "org_edge",
                    "edge_type": edge.get("edge_type"),
                    "from_id": edge.get("from_id"),
                    "to_id": edge.get("to_id"),
                    "source_ids": edge.get("source_ids"),
                    "entity": o.get("entity_id"),
                }
            )

        # team via MEMBER_OF of owner or OWNS team
        if primary:
            for e in self.graph.list_edges(
                organization_id, edge_type="MEMBER_OF", from_id=primary["entity_id"]
            ):
                ent = self.store.get_entity(e["to_id"])
                if ent and ent.get("entity_type") == "team":
                    team = ent["id"]
                    break

        if include_on_call:
            for eid in entity_ids:
                oc = self.graph.on_call_for(organization_id, eid)
                if oc:
                    person = oc[0].get("person") or {}
                    on_call = {
                        "entity_id": person.get("id"),
                        "name": person.get("name"),
                        "valid_until": (oc[0].get("edge") or {}).get("valid_until"),
                    }
                    evidence.append(
                        {
                            "type": "on_call",
                            "edge": oc[0].get("edge"),
                        }
                    )
                    break

        # related issue memories as evidence
        mems = self.retrieval.authz.filter_memories(
            decision, self.graph.current_memories(organization_id, entity_ids=entity_ids)
        )
        for m in mems:
            if m.get("memory_type") == "issue":
                evidence.append(
                    {
                        "type": "memory",
                        "memory_id": m["id"],
                        "content": m["content"],
                        "source_ids": m.get("source_ids"),
                    }
                )

        result = {
            "primary_owner": primary,
            "on_call": on_call,
            "team": team,
            "evidence": evidence,
            "fallback_path": [],
            "permission_decision_id": decision.id,
        }
        self.audit.record(
            organization_id=organization_id,
            event_type="owner.resolved",
            principal_id=acting_principal_id,
            payload=result,
        )
        return result

    def project_context(
        self,
        *,
        organization_id: str,
        project_id: str,
        acting_principal_id: str,
        destination: Destination | dict[str, Any] | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        decision = self.retrieval.authorize(
            organization_id, acting_principal_id, destination, purpose
        )
        project = self.store.get_entity(project_id)
        mems = self.retrieval.authz.filter_memories(
            decision,
            self.graph.current_memories(organization_id, entity_ids=[project_id]),
        )
        members = []
        for e in self.graph.list_edges(organization_id, edge_type="WORKS_ON", to_id=project_id):
            ent = self.store.get_entity(e["from_id"])
            if ent:
                members.append({"id": ent["id"], "name": ent.get("name"), "type": ent.get("entity_type")})
        owners = self.graph.resolve_owners(organization_id, [project_id])
        payload = {
            "project": project,
            "members": members,
            "owners": owners,
            "current_facts": [m for m in mems if m.get("memory_type") == "fact"],
            "decisions": [m for m in mems if m.get("memory_type") == "decision"],
            "issues": [m for m in mems if m.get("memory_type") == "issue"],
            "commitments": [m for m in mems if m.get("memory_type") == "commitment"],
            "permission_decision_id": decision.id,
        }
        self.audit.record(
            organization_id=organization_id,
            event_type="project.context",
            principal_id=acting_principal_id,
            payload={"project_id": project_id, "permission_decision_id": decision.id},
        )
        return payload
