"""Optional MCP tool surface mirroring reactive Brain operations.

MCP is an interoperability adapter, not a replacement for REST.
Wire these handlers into an MCP server process when needed.
"""

from __future__ import annotations

from typing import Any, Callable

from docket.services.container import DocketApp


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "brain_search",
            "description": "Permission-aware organizational memory search",
            "input_schema": {
                "type": "object",
                "required": ["organization_id", "query", "acting_principal_id"],
                "properties": {
                    "organization_id": {"type": "string"},
                    "query": {"type": "string"},
                    "acting_principal_id": {"type": "string"},
                    "destination": {"type": "object"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "brain_prepare_context",
            "description": "Build a structured, permission-safe context package",
            "input_schema": {
                "type": "object",
                "required": ["organization_id", "task", "acting_principal_id"],
                "properties": {
                    "organization_id": {"type": "string"},
                    "task": {"type": "string"},
                    "acting_principal_id": {"type": "string"},
                    "entity_ids": {"type": "array", "items": {"type": "string"}},
                    "token_budget": {"type": "integer"},
                    "destination": {"type": "object"},
                },
            },
        },
        {
            "name": "brain_resolve_owner",
            "description": "Resolve current owner/on-call with evidence",
            "input_schema": {
                "type": "object",
                "required": ["organization_id", "issue_text", "acting_principal_id"],
                "properties": {
                    "organization_id": {"type": "string"},
                    "issue_text": {"type": "string"},
                    "acting_principal_id": {"type": "string"},
                    "entity_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "brain_relationship_context",
            "description": "Shared decisions/commitments and graph paths between two entities",
            "input_schema": {
                "type": "object",
                "required": [
                    "organization_id",
                    "from_entity_id",
                    "to_entity_id",
                    "acting_principal_id",
                ],
                "properties": {
                    "organization_id": {"type": "string"},
                    "from_entity_id": {"type": "string"},
                    "to_entity_id": {"type": "string"},
                    "acting_principal_id": {"type": "string"},
                },
            },
        },
        {
            "name": "brain_project_context",
            "description": "Project members, owners, and current knowledge",
            "input_schema": {
                "type": "object",
                "required": ["organization_id", "project_id", "acting_principal_id"],
                "properties": {
                    "organization_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "acting_principal_id": {"type": "string"},
                },
            },
        },
        {
            "name": "brain_search_skills",
            "description": "Search approved organizational procedures",
            "input_schema": {"type": "object", "required": ["organization_id", "query"], "properties": {"organization_id": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}}},
        },
        {
            "name": "brain_prepare_skill",
            "description": "Prepare a versioned skill for a harness",
            "input_schema": {"type": "object", "required": ["skill_id"], "properties": {"skill_id": {"type": "string"}, "context_warnings": {"type": "array", "items": {"type": "string"}}}},
        },
        {
            "name": "brain_record_task_event",
            "description": "Record structured execution telemetry for an existing Task",
            "input_schema": {"type": "object", "required": ["task_id", "event_type"], "properties": {"task_id": {"type": "string"}, "event_type": {"type": "string"}, "action": {"type": "string"}, "result": {"type": "string"}}},
        },
        {
            "name": "brain_evaluate_intervention",
            "description": "Evaluate a permission-safe proactive intervention recommendation",
            "input_schema": {"type": "object", "required": ["organization_id", "trigger", "acting_principal_id"], "properties": {"organization_id": {"type": "string"}, "trigger": {"type": "object"}, "acting_principal_id": {"type": "string"}, "destination": {"type": "object"}}},
        },
    ]


def bind_mcp_handlers(brain: DocketApp) -> dict[str, Callable[..., Any]]:
    def search(**kwargs: Any) -> dict[str, Any]:
        return brain.retrieval.search(
            organization_id=kwargs["organization_id"],
            query=kwargs["query"],
            acting_principal_id=kwargs["acting_principal_id"],
            destination=kwargs.get("destination"),
            limit=int(kwargs.get("limit") or 20),
            entity_ids=kwargs.get("entity_ids"),
        )

    def prepare_context(**kwargs: Any) -> dict[str, Any]:
        pkg = brain.context.prepare_context(
            organization_id=kwargs["organization_id"],
            task=kwargs["task"],
            acting_principal_id=kwargs["acting_principal_id"],
            destination=kwargs.get("destination"),
            entity_ids=kwargs.get("entity_ids"),
            token_budget=int(kwargs.get("token_budget") or 8000),
        )
        return pkg.model_dump(mode="json")

    def resolve_owner(**kwargs: Any) -> dict[str, Any]:
        return brain.context.resolve_owner(
            organization_id=kwargs["organization_id"],
            issue_text=kwargs["issue_text"],
            acting_principal_id=kwargs["acting_principal_id"],
            entity_ids=kwargs.get("entity_ids"),
            destination=kwargs.get("destination"),
        )

    def relationship_context(**kwargs: Any) -> dict[str, Any]:
        return brain.context.relationship_context(
            organization_id=kwargs["organization_id"],
            from_entity_id=kwargs["from_entity_id"],
            to_entity_id=kwargs["to_entity_id"],
            acting_principal_id=kwargs["acting_principal_id"],
            destination=kwargs.get("destination"),
        )

    def project_context(**kwargs: Any) -> dict[str, Any]:
        return brain.context.project_context(
            organization_id=kwargs["organization_id"],
            project_id=kwargs["project_id"],
            acting_principal_id=kwargs["acting_principal_id"],
            destination=kwargs.get("destination"),
        )

    def search_skills(**kwargs: Any) -> dict[str, Any]:
        return {"results": brain.skills.search(kwargs["organization_id"], kwargs["query"], int(kwargs.get("limit") or 20))}

    def prepare_skill(**kwargs: Any) -> dict[str, Any]:
        result = brain.skills.prepare(kwargs["skill_id"], kwargs.get("context_warnings"))
        if not result:
            raise KeyError("skill not found")
        return result

    def record_task_event(**kwargs: Any) -> dict[str, Any]:
        task = brain.store.get_task(kwargs["task_id"])
        if not task:
            raise KeyError("task not found")
        event = {key: value for key, value in kwargs.items() if key != "task_id"}
        task.setdefault("events", []).append(event)
        brain.store.update_task(task)
        return task

    def evaluate_intervention(**kwargs: Any) -> dict[str, Any]:
        return brain.proactive.evaluate(
            organization_id=kwargs["organization_id"], trigger=kwargs["trigger"],
            acting_principal_id=kwargs["acting_principal_id"], destination=kwargs.get("destination"),
            mode=kwargs.get("mode", "dry_run"), candidate=kwargs.get("candidate"),
        )

    return {
        "brain_search": search,
        "brain_prepare_context": prepare_context,
        "brain_resolve_owner": resolve_owner,
        "brain_relationship_context": relationship_context,
        "brain_project_context": project_context,
        "brain_search_skills": search_skills,
        "brain_prepare_skill": prepare_skill,
        "brain_record_task_event": record_task_event,
        "brain_evaluate_intervention": evaluate_intervention,
    }
