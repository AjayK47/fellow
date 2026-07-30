"""Trusted tool registry, policy enforcement, approvals, and execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from company_brain.domain.models import new_id, utcnow
from company_brain.harness.composio import ComposioClient
from company_brain.harness.models import ModelToolCall, ToolDefinition, ToolResult
from company_brain.storage.db import BrainStore


ToolHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool: {definition.name}")
        self._tools[definition.name] = RegisteredTool(definition, handler)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [registered.definition for registered in self._tools.values()]

    def model_schemas(self) -> list[dict[str, Any]]:
        return [definition.openai_schema() for definition in self.definitions()]

    def replace_definition(self, definition: ToolDefinition) -> None:
        registered = self._tools.get(definition.name)
        if not registered:
            raise KeyError(definition.name)
        self._tools[definition.name] = RegisteredTool(
            definition=definition, handler=registered.handler
        )


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


COMPOSIO_TOOLS: dict[str, tuple[str, str, str]] = {
    "linear_search_issues": (
        "LINEAR_SEARCH_ISSUES",
        "Search Linear issues visible to the organization account.",
        "read",
    ),
    "linear_get_issue": (
        "LINEAR_GET_ISSUE",
        "Read a Linear issue by identifier.",
        "read",
    ),
    "linear_create_issue": (
        "LINEAR_CREATE_ISSUE",
        "Create a Linear issue after human approval.",
        "write",
    ),
    "linear_add_comment": (
        "LINEAR_CREATE_COMMENT",
        "Add a comment to a Linear issue after human approval.",
        "write",
    ),
    "linear_update_issue": (
        "LINEAR_UPDATE_ISSUE",
        "Update a Linear issue after human approval.",
        "write",
    ),
    "github_search_issues": (
        "GITHUB_SEARCH_ISSUES",
        "Search GitHub issues and pull requests.",
        "read",
    ),
    "github_get_pull_request": (
        "GITHUB_GET_A_PULL_REQUEST",
        "Read a GitHub pull request.",
        "read",
    ),
    "github_list_commits": (
        "GITHUB_LIST_COMMITS",
        "List commits in a GitHub repository.",
        "read",
    ),
    "github_create_issue": (
        "GITHUB_CREATE_ISSUE",
        "Create a GitHub issue after human approval.",
        "write",
    ),
    "github_add_comment": (
        "GITHUB_CREATE_AN_ISSUE_COMMENT",
        "Add a GitHub issue/PR comment after human approval.",
        "write",
    ),
}


def build_tool_registry(
    *,
    store: BrainStore,
    retrieval: Any,
    context: Any,
    skills: Any,
    composio: ComposioClient,
) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolDefinition(
            name="request_human_input",
            description=(
                "Pause the current run and ask the Slack user for information "
                "that is genuinely required to continue."
            ),
            input_schema=_object_schema(
                {
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                },
                ["question", "reason"],
            ),
            risk="read",
        ),
        lambda args, runtime: {
            "waiting_for_human": True,
            "question": args["question"],
            "reason": args["reason"],
            "run_id": runtime["run_id"],
        },
    )

    registry.register(
        ToolDefinition(
            name="brain_search",
            description=(
                "Search permission-safe organizational memories and evidence. "
                "Use this when the supplied context is insufficient."
            ),
            input_schema=_object_schema(
                {
                    "query": {"type": "string"},
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query"],
            ),
            risk="read",
        ),
        lambda args, runtime: retrieval.search(
            organization_id=runtime["organization_id"],
            query=args["query"],
            acting_principal_id=runtime["principal_id"],
            destination=runtime["destination"],
            purpose=runtime["purpose"],
            entity_ids=args.get("entity_ids") or None,
            limit=min(int(args.get("limit", 10)), 20),
        ),
    )

    registry.register(
        ToolDefinition(
            name="brain_resolve_owner",
            description="Resolve the current owner and on-call person with evidence.",
            input_schema=_object_schema(
                {
                    "issue_text": {"type": "string"},
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                ["issue_text"],
            ),
            risk="read",
        ),
        lambda args, runtime: context.resolve_owner(
            organization_id=runtime["organization_id"],
            issue_text=args["issue_text"],
            acting_principal_id=runtime["principal_id"],
            entity_ids=args.get("entity_ids") or None,
            include_on_call=True,
            destination=runtime["destination"],
            purpose=runtime["purpose"],
        ),
    )

    registry.register(
        ToolDefinition(
            name="brain_find_skills",
            description="Find approved organizational procedures relevant to a task.",
            input_schema=_object_schema(
                {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                ["query"],
            ),
            risk="read",
        ),
        lambda args, runtime: {
            "skills": skills.search(
                runtime["organization_id"],
                args["query"],
                limit=min(int(args.get("limit", 5)), 10),
            )
        },
    )

    external_schema = _object_schema(
        {
            "arguments": {
                "type": "object",
                "description": "Arguments matching the selected Composio tool schema.",
                "additionalProperties": True,
            }
        },
        ["arguments"],
    )
    for name, (slug, description, risk) in COMPOSIO_TOOLS.items():
        toolkit = "linear" if name.startswith("linear_") else "github"

        def _handler(
            args: dict[str, Any],
            runtime: dict[str, Any],
            *,
            tool_slug: str = slug,
            toolkit_slug: str = toolkit,
        ) -> dict[str, Any]:
            arguments = (
                args.get("arguments")
                if set(args) == {"arguments"} and isinstance(args.get("arguments"), dict)
                else args
            )
            account = store.connector_account(
                runtime["organization_id"], toolkit_slug
            )
            return composio.execute(
                tool_slug,
                arguments or {},
                connected_account_id=(
                    account.get("connected_account_id") if account else None
                ),
            )

        registry.register(
            ToolDefinition(
                name=name,
                description=description,
                input_schema=external_schema,
                risk="write" if risk == "write" else "read",
                approval_required=risk == "write",
                toolkits=[toolkit],
            ),
            _handler,
        )
    return registry


def refresh_composio_tool_schemas(
    registry: ToolRegistry, composio: ComposioClient
) -> dict[str, Any]:
    """Replace generic fallback schemas with the current Composio schemas."""
    refreshed: list[str] = []
    failed: dict[str, str] = {}
    for name, (slug, description, risk) in COMPOSIO_TOOLS.items():
        try:
            payload = composio.describe(slug)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            schema = (
                data.get("input_schema")
                or data.get("input_parameters")
                or data.get("parameters")
            )
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("Composio response did not contain an object input schema")
            existing = registry.get(name)
            registry.replace_definition(
                ToolDefinition(
                    name=name,
                    description=str(data.get("description") or description),
                    input_schema=schema,
                    risk="write" if risk == "write" else "read",
                    approval_required=risk == "write",
                    toolkits=existing.definition.toolkits if existing else [],
                )
            )
            refreshed.append(name)
        except Exception as exc:
            failed[name] = str(exc)
    return {"refreshed": refreshed, "failed": failed}


class ToolExecutor:
    """The model proposes; trusted code authorizes, validates, and executes."""

    def __init__(self, store: BrainStore, registry: ToolRegistry, audit: Any) -> None:
        self.store = store
        self.registry = registry
        self.audit = audit

    def allowed_definitions(self, run: dict[str, Any]) -> list[ToolDefinition]:
        org = run["organization_id"]
        definitions: list[ToolDefinition] = []
        for definition in self.registry.definitions():
            policy = self._policy(org, definition.name)
            if policy.get("enabled", True) and definition.risk != "destructive":
                definitions.append(definition)
        return definitions

    def execute(
        self,
        *,
        run: dict[str, Any],
        call: ModelToolCall,
        approved: bool = False,
    ) -> ToolResult:
        registered = self.registry.get(call.name)
        if not registered:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="disabled",
                error="Unknown or unavailable tool.",
            )
        definition = registered.definition
        policy = self._policy(run["organization_id"], definition.name)
        if not policy.get("enabled", True) or definition.risk == "destructive":
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="disabled",
                error="Tool is disabled by organization policy.",
            )

        validation_error = self._validate_arguments(
            call.arguments, definition.input_schema
        )
        if validation_error:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="failed",
                error=validation_error,
            )

        idempotency_key = f"{run['id']}:{call.id}"
        existing = self.store.tool_call_by_idempotency(
            run["organization_id"], idempotency_key
        )
        if existing and existing.get("status") == "completed":
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="completed",
                output=existing.get("output") or {},
                evidence_source_ids=existing.get("evidence_source_ids") or [],
            )

        now = utcnow().isoformat()
        record = existing or {
            "id": call.id,
            "organization_id": run["organization_id"],
            "run_id": run["id"],
            "tool_name": call.name,
            "arguments": call.arguments,
            "risk": definition.risk,
            "idempotency_key": idempotency_key,
            "status": "proposed",
            "created_at": now,
            "updated_at": now,
        }

        requires_approval = (
            definition.approval_required
            or definition.risk == "write"
            or policy.get("approval_required", False)
        )
        if requires_approval and not approved:
            approval = self._create_approval(run, record, definition)
            record["status"] = "waiting_for_approval"
            record["approval_id"] = approval["id"]
            record["updated_at"] = utcnow().isoformat()
            self.store.upsert_harness_tool_call(record)
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="waiting_for_approval",
                approval_id=approval["id"],
                output={"preview": approval["preview"]},
            )

        envelope = run["envelope"]
        runtime = {
            "organization_id": run["organization_id"],
            "principal_id": envelope["principal_id"],
            "destination": envelope["destination"],
            "purpose": envelope["purpose"],
            "run_id": run["id"],
        }
        record["status"] = "running"
        record["updated_at"] = utcnow().isoformat()
        self.store.upsert_harness_tool_call(record)
        try:
            raw_output = registered.handler(call.arguments, runtime)
            output = self._bounded_output(raw_output)
            record["status"] = "completed"
            record["output"] = output
            record["updated_at"] = utcnow().isoformat()
            self.store.upsert_harness_tool_call(record)
            self.audit.record(
                run["organization_id"],
                "agent.tool.completed",
                {
                    "run_id": run["id"],
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "risk": definition.risk,
                },
                principal_id=envelope["principal_id"],
                trace_id=run["trace_id"],
            )
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="completed",
                output=output,
            )
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            record["updated_at"] = utcnow().isoformat()
            self.store.upsert_harness_tool_call(record)
            self.audit.record(
                run["organization_id"],
                "agent.tool.failed",
                {
                    "run_id": run["id"],
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "error": str(exc),
                },
                principal_id=envelope["principal_id"],
                trace_id=run["trace_id"],
            )
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="failed",
                error=str(exc),
            )

    def resolve_approval(
        self,
        *,
        approval_id: str,
        approved: bool,
        decided_by: str,
        edited_arguments: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], ToolResult | None]:
        approval = self.store.get_harness_record("approvals", approval_id)
        if not approval:
            raise KeyError(f"approval not found: {approval_id}")
        if approval["status"] != "pending":
            raise ValueError("approval has already been resolved")
        allowed_approvers = set(
            approval.get("allowed_approver_ids")
            or [approval.get("requested_by")]
        )
        if decided_by not in allowed_approvers:
            raise PermissionError(
                "This principal is not authorized to decide the approval."
            )
        if approval.get("expires_at") and approval["expires_at"] < utcnow().isoformat():
            approved = False
            approval["status"] = "expired"
        else:
            approval["status"] = "approved" if approved else "denied"
        approval["decided_by"] = decided_by
        approval["decided_at"] = utcnow().isoformat()
        approval["updated_at"] = approval["decided_at"]
        if edited_arguments is not None:
            approval["edited_arguments"] = edited_arguments
        self.store.upsert_approval(approval)

        if not approved:
            return approval, ToolResult(
                call_id=approval["tool_call_id"],
                tool_name=approval["tool_name"],
                status="denied",
                error="Human approval denied.",
                approval_id=approval_id,
            )

        run = self.store.get_harness_record("agent_runs", approval["run_id"])
        if not run:
            raise KeyError(f"run not found: {approval['run_id']}")
        call = ModelToolCall(
            id=approval["tool_call_id"],
            name=approval["tool_name"],
            arguments=edited_arguments
            if edited_arguments is not None
            else approval["arguments"],
        )
        return approval, self.execute(run=run, call=call, approved=True)

    def _policy(self, organization_id: str, tool_name: str) -> dict[str, Any]:
        records = self.store.list_harness_records(
            "tool_policies",
            organization_id,
            where={"tool_name": tool_name},
            order_by="updated_at",
            limit=1,
        )
        return records[0] if records else {"enabled": True}

    def _create_approval(
        self,
        run: dict[str, Any],
        call_record: dict[str, Any],
        definition: ToolDefinition,
    ) -> dict[str, Any]:
        now = utcnow()
        approval = {
            "id": new_id("approval"),
            "organization_id": run["organization_id"],
            "run_id": run["id"],
            "tool_call_id": call_record["id"],
            "tool_name": definition.name,
            "arguments": call_record["arguments"],
            "status": "pending",
            "requested_by": run["envelope"]["principal_id"],
            "allowed_approver_ids": [run["envelope"]["principal_id"]],
            "preview": {
                "action": definition.name,
                "description": definition.description,
                "arguments": call_record["arguments"],
                "destination": run["envelope"]["destination"],
            },
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self.store.upsert_approval(approval)
        self.audit.record(
            run["organization_id"],
            "agent.approval.requested",
            {
                "run_id": run["id"],
                "approval_id": approval["id"],
                "tool_name": definition.name,
                "preview": approval["preview"],
            },
            principal_id=run["envelope"]["principal_id"],
            trace_id=run["trace_id"],
        )
        return approval

    @staticmethod
    def _validate_arguments(
        arguments: dict[str, Any], schema: dict[str, Any]
    ) -> str | None:
        if not isinstance(arguments, dict):
            return "Tool arguments must be an object."
        for required in schema.get("required") or []:
            if required not in arguments:
                return f"Missing required tool argument: {required}"
        if schema.get("additionalProperties") is False:
            allowed = set((schema.get("properties") or {}).keys())
            extras = set(arguments) - allowed
            if extras:
                return f"Unknown tool arguments: {', '.join(sorted(extras))}"
        return None

    @staticmethod
    def _bounded_output(output: Any) -> dict[str, Any]:
        if isinstance(output, dict):
            value = output
        else:
            value = {"result": output}
        serialized = json.dumps(value, default=str)
        if len(serialized) <= 20_000:
            return value
        return {
            "truncated": True,
            "content": serialized[:20_000],
            "original_characters": len(serialized),
        }
