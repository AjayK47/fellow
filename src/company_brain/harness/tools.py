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
    reality: Any | None = None,
    artifacts: Any | None = None,
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

    if reality:
        def _query_facts(
            args: dict[str, Any], runtime: dict[str, Any]
        ) -> dict[str, Any]:
            decision = retrieval.authorize(
                runtime["organization_id"],
                runtime["principal_id"],
                runtime["destination"],
                runtime["purpose"],
            )
            facts = reality.current_facts(
                runtime["organization_id"],
                subject_id=args.get("subject_id"),
                predicate=args.get("predicate"),
                valid_at=args.get("valid_at"),
                known_at=args.get("known_at"),
                scope_ids=decision.allowed_scope_ids,
            )
            return {
                "facts": facts[: min(int(args.get("limit", 20)), 50)],
                "permission_decision_id": decision.id,
            }

        registry.register(
            ToolDefinition(
                name="brain_query_facts",
                description=(
                    "Query permission-safe bi-temporal company facts. Use "
                    "valid_at for what was true in the company then and known_at "
                    "for what Company Brain knew at that time."
                ),
                input_schema=_object_schema(
                    {
                        "subject_id": {"type": "string"},
                        "predicate": {"type": "string"},
                        "valid_at": {"type": "string"},
                        "known_at": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    [],
                ),
                risk="read",
            ),
            _query_facts,
        )

        def _list_cases(
            args: dict[str, Any], runtime: dict[str, Any]
        ) -> dict[str, Any]:
            decision = retrieval.authorize(
                runtime["organization_id"],
                runtime["principal_id"],
                runtime["destination"],
                runtime["purpose"],
            )
            statuses = {args["status"]} if args.get("status") else None
            cases = reality.list_cases(
                runtime["organization_id"],
                statuses=statuses,
                scope_ids=decision.allowed_scope_ids,
            )
            return {
                "cases": cases[: min(int(args.get("limit", 20)), 50)],
                "permission_decision_id": decision.id,
            }

        registry.register(
            ToolDefinition(
                name="brain_list_cases",
                description=(
                    "List permission-safe cross-tool company cases that correlate "
                    "events from Slack, tickets, code, CRM, and other sources."
                ),
                input_schema=_object_schema(
                    {
                        "status": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    [],
                ),
                risk="read",
            ),
            _list_cases,
        )

        registry.register(
            ToolDefinition(
                name="brain_update_case",
                description=(
                    "Update the lifecycle state or summary of an existing "
                    "company case. This is a governed write and requires approval."
                ),
                input_schema=_object_schema(
                    {
                        "case_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "open",
                                "investigating",
                                "blocked",
                                "mitigated",
                                "resolved",
                                "dismissed",
                            ],
                        },
                        "summary": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    ["case_id", "status"],
                ),
                risk="write",
                approval_required=True,
            ),
            lambda args, runtime: reality.update_case(
                args["case_id"],
                status=args["status"],
                updated_by=runtime["principal_id"],
                summary=args.get("summary"),
                title=args.get("title"),
            ),
        )

        registry.register(
            ToolDefinition(
                name="brain_judgment_guidance",
                description=(
                    "Read durable human corrections, approvals, rejections, and "
                    "preferences applicable to the acting principal."
                ),
                input_schema=_object_schema(
                    {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                        }
                    },
                    [],
                ),
                risk="read",
            ),
            lambda args, runtime: {
                "judgments": reality.judgment_guidance(
                    runtime["organization_id"],
                    runtime["principal_id"],
                    limit=min(int(args.get("limit", 10)), 20),
                )
            },
        )

    if artifacts:
        def _list_artifacts(
            args: dict[str, Any], runtime: dict[str, Any]
        ) -> dict[str, Any]:
            decision = retrieval.authorize(
                runtime["organization_id"],
                runtime["principal_id"],
                runtime["destination"],
                runtime["purpose"],
            )
            values = artifacts.list(
                runtime["organization_id"],
                scope_ids=decision.allowed_scope_ids,
                artifact_type=args.get("artifact_type"),
                run_id=args.get("run_id"),
            )
            return {
                "artifacts": values[: min(int(args.get("limit", 20)), 50)],
                "permission_decision_id": decision.id,
            }

        registry.register(
            ToolDefinition(
                name="brain_list_artifacts",
                description=(
                    "List durable reports, plans, briefs, tables, and specialist "
                    "results visible to the acting principal."
                ),
                input_schema=_object_schema(
                    {
                        "artifact_type": {"type": "string"},
                        "run_id": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    [],
                ),
                risk="read",
            ),
            _list_artifacts,
        )

        registry.register(
            ToolDefinition(
                name="brain_create_artifact",
                description=(
                    "Create a durable permission-scoped report, plan, brief, "
                    "table, or other structured artifact from completed work."
                ),
                input_schema=_object_schema(
                    {
                        "artifact_type": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {},
                        "scope_id": {"type": "string"},
                        "metadata": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    },
                    ["artifact_type", "title", "content", "scope_id"],
                ),
                risk="write",
                approval_required=True,
            ),
            lambda args, runtime: artifacts.create(
                organization_id=runtime["organization_id"],
                artifact_type=args["artifact_type"],
                title=args["title"],
                content=args["content"],
                scope_id=args["scope_id"],
                run_id=runtime["run_id"],
                created_by=runtime["principal_id"],
                metadata=args.get("metadata") or {},
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

    def _connected_accounts(runtime: dict[str, Any]) -> list[dict[str, Any]]:
        return store.list_connector_accounts(
            runtime["organization_id"], active_only=True
        )

    registry.register(
        ToolDefinition(
            name="composio_list_integrations",
            description=(
                "List the external applications connected to this organization. "
                "Use this before searching for external actions."
            ),
            input_schema=_object_schema({}, []),
            risk="read",
        ),
        lambda _args, runtime: {
            "configured": composio.configured,
            "integrations": [
                {
                    "toolkit": account["toolkit"],
                    "connected_account_id": account["connected_account_id"],
                    "status": account.get("status"),
                }
                for account in _connected_accounts(runtime)
            ],
        },
    )

    def _search_composio(
        args: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        accounts = _connected_accounts(runtime)
        connected_toolkits = sorted(
            {str(account["toolkit"]).lower() for account in accounts}
        )
        requested = (
            str(args.get("toolkit") or "").lower().strip() or None
        )
        if requested and requested not in connected_toolkits:
            return {
                "tools": [],
                "connected_toolkits": connected_toolkits,
                "error": f"{requested} is not connected for this organization",
            }
        toolkits = [requested] if requested else connected_toolkits
        limit = min(max(int(args.get("limit", 10)), 1), 25)
        found: dict[str, dict[str, Any]] = {}
        for toolkit in toolkits:
            response = composio.search_tools(
                query=args["query"], toolkit=toolkit, limit=limit
            )
            for item in response.get("items") or []:
                slug = item.get("slug")
                item_toolkit = (item.get("toolkit") or {}).get("slug")
                if (
                    slug
                    and str(item_toolkit or toolkit).lower()
                    in connected_toolkits
                ):
                    found[str(slug)] = {
                        "slug": slug,
                        "name": item.get("name"),
                        "description": item.get("description"),
                        "toolkit": item_toolkit or toolkit,
                    }
        return {
            "tools": list(found.values())[:limit],
            "connected_toolkits": connected_toolkits,
        }

    registry.register(
        ToolDefinition(
            name="composio_search",
            description=(
                "Search actions available only in this organization's connected "
                "external applications. Never guess an action slug."
            ),
            input_schema=_object_schema(
                {
                    "query": {"type": "string"},
                    "toolkit": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 25,
                    },
                },
                ["query"],
            ),
            risk="read",
        ),
        _search_composio,
    )

    def _describe_composio(
        args: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        accounts = _connected_accounts(runtime)
        connected_toolkits = {
            str(account["toolkit"]).lower() for account in accounts
        }
        result = composio.describe(args["tool_slug"])
        toolkit = str((result.get("toolkit") or {}).get("slug") or "").lower()
        if toolkit not in connected_toolkits:
            raise PermissionError(
                "Tool does not belong to an organization-connected toolkit"
            )
        return result

    registry.register(
        ToolDefinition(
            name="composio_describe",
            description=(
                "Fetch the exact input schema for a Composio action returned by "
                "composio_search."
            ),
            input_schema=_object_schema(
                {"tool_slug": {"type": "string"}}, ["tool_slug"]
            ),
            risk="read",
        ),
        _describe_composio,
    )

    def _execute_composio(
        args: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        accounts = _connected_accounts(runtime)
        described = composio.describe(args["tool_slug"])
        toolkit = str(
            (described.get("toolkit") or {}).get("slug") or ""
        ).lower()
        matching = [
            account
            for account in accounts
            if str(account["toolkit"]).lower() == toolkit
        ]
        selected_id = args.get("connected_account_id")
        if selected_id:
            matching = [
                account
                for account in matching
                if account["connected_account_id"] == selected_id
            ]
        if not matching:
            raise PermissionError(
                "No matching organization-connected account for this action"
            )
        if len(matching) > 1:
            raise ValueError(
                "Multiple connected accounts match; connected_account_id is required"
            )
        result = composio.execute(
            args["tool_slug"],
            args.get("arguments") or {},
            connected_account_id=matching[0]["connected_account_id"],
        )
        if result.get("successful") is False:
            raise RuntimeError(
                str(result.get("error") or "Composio action failed")
            )
        return result

    registry.register(
        ToolDefinition(
            name="composio_execute",
            description=(
                "Execute an exact action slug previously returned by "
                "composio_search and inspected with composio_describe. Dynamic "
                "external actions require human approval."
            ),
            input_schema=_object_schema(
                {
                    "tool_slug": {"type": "string"},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "connected_account_id": {"type": "string"},
                },
                ["tool_slug", "arguments"],
            ),
            risk="write",
            approval_required=True,
        ),
        _execute_composio,
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

    def __init__(
        self,
        store: BrainStore,
        registry: ToolRegistry,
        audit: Any,
        *,
        policy_engine: Any | None = None,
        reality: Any | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.audit = audit
        self.policy_engine = policy_engine
        self.reality = reality

    def allowed_definitions(self, run: dict[str, Any]) -> list[ToolDefinition]:
        org = run["organization_id"]
        metadata = run.get("metadata") or {}
        worker_allowed = (
            set(metadata.get("allowed_tools") or [])
            if metadata.get("specialist_worker")
            else None
        )
        definitions: list[ToolDefinition] = []
        for definition in self.registry.definitions():
            if worker_allowed is not None and definition.name not in worker_allowed:
                continue
            if metadata.get("specialist_worker") and definition.risk != "read":
                continue
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
        metadata = run.get("metadata") or {}
        if metadata.get("specialist_worker"):
            worker_allowed = set(metadata.get("allowed_tools") or [])
            if definition.name not in worker_allowed or definition.risk != "read":
                return ToolResult(
                    call_id=call.id,
                    tool_name=call.name,
                    status="disabled",
                    error="Tool is outside this specialist worker's read-only capability envelope.",
                )
        policy = self._policy(run["organization_id"], definition.name)
        if not policy.get("enabled", True) or definition.risk == "destructive":
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="disabled",
                error="Tool is disabled by organization policy.",
            )
        conditional = self._conditional_policy(run, definition, call.arguments)
        if conditional["decision"] == "deny":
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status="denied",
                error="Tool is denied by conditional organization policy.",
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
            or conditional.get("approval_required", False)
        )
        if requires_approval and not approved:
            approval = self._create_approval(run, record, definition)
            if self.policy_engine and conditional.get("preconditions"):
                runtime = self._runtime(run, call.arguments)
                captured = self.policy_engine.capture_preconditions(
                    organization_id=run["organization_id"],
                    approval_id=approval["id"],
                    specifications=conditional["preconditions"],
                    runtime=runtime,
                    arguments=call.arguments,
                )
                approval["precondition_ids"] = [item["id"] for item in captured]
                self.store.upsert_approval(approval)
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
        runtime = self._runtime(run, call.arguments)
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
        if self.reality:
            self.reality.record_judgment(
                organization_id=approval["organization_id"],
                judgment_type="tool_approval",
                principal_id=decided_by,
                subject={
                    "tool_name": approval["tool_name"],
                    "arguments": edited_arguments
                    if edited_arguments is not None
                    else approval["arguments"],
                    "approval_id": approval_id,
                },
                decision="approved" if approved else approval["status"],
                reason=(
                    "arguments_edited"
                    if edited_arguments is not None
                    else None
                ),
            )

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
        if self.policy_engine and approval.get("precondition_ids"):
            check = self.policy_engine.revalidate(
                approval_id=approval_id,
                runtime=self._runtime(run, call.arguments),
                arguments=call.arguments,
            )
            if not check["valid"]:
                approval["status"] = "stale"
                approval["precondition_check"] = check
                approval["updated_at"] = utcnow().isoformat()
                self.store.upsert_approval(approval)
                return approval, ToolResult(
                    call_id=approval["tool_call_id"],
                    tool_name=approval["tool_name"],
                    status="failed",
                    error=(
                        "External state changed after approval; the action was "
                        "not executed and must be replanned."
                    ),
                    approval_id=approval_id,
                )
        return approval, self.execute(run=run, call=call, approved=True)

    def _conditional_policy(
        self,
        run: dict[str, Any],
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.policy_engine:
            return {
                "decision": "legacy",
                "approval_required": False,
                "preconditions": [],
            }
        envelope = run["envelope"]
        return self.policy_engine.evaluate(
            organization_id=run["organization_id"],
            action=definition.name,
            context={
                "principal": {
                    "id": envelope["principal_id"],
                },
                "action": {
                    "name": definition.name,
                    "risk": definition.risk,
                    "arguments": arguments,
                },
                "destination": envelope["destination"],
                "purpose": envelope["purpose"],
                "run": {
                    "id": run["id"],
                    "trigger_kind": run["trigger_kind"],
                    "is_worker": bool(
                        (run.get("metadata") or {}).get("specialist_worker")
                    ),
                },
            },
        )

    def _runtime(
        self, run: dict[str, Any], arguments: dict[str, Any]
    ) -> dict[str, Any]:
        envelope = run["envelope"]
        account_id = arguments.get("connected_account_id")
        account = (
            self.store.connector_account_by_external_id(str(account_id))
            if account_id
            else None
        )
        return {
            "organization_id": run["organization_id"],
            "principal_id": envelope["principal_id"],
            "destination": envelope["destination"],
            "purpose": envelope["purpose"],
            "run_id": run["id"],
            "connected_account_id": account_id,
            "composio_user_id": (account or {}).get("composio_user_id"),
            "run_metadata": run.get("metadata") or {},
        }

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
        def validate(value: Any, node: dict[str, Any], path: str) -> str | None:
            expected = node.get("type")
            expected_types = (
                set(expected) if isinstance(expected, list) else {expected}
            )
            expected_types.discard(None)
            type_ok = {
                "object": lambda item: isinstance(item, dict),
                "array": lambda item: isinstance(item, list),
                "string": lambda item: isinstance(item, str),
                "integer": lambda item: isinstance(item, int)
                and not isinstance(item, bool),
                "number": lambda item: isinstance(item, (int, float))
                and not isinstance(item, bool),
                "boolean": lambda item: isinstance(item, bool),
                "null": lambda item: item is None,
            }
            if expected_types and not any(
                type_ok[kind](value)
                for kind in expected_types
                if kind in type_ok
            ):
                return (
                    f"{path} must be one of: "
                    f"{', '.join(sorted(expected_types))}"
                )
            if "enum" in node and value not in node["enum"]:
                return f"{path} must be one of: {', '.join(map(str, node['enum']))}"
            if isinstance(value, dict):
                properties = node.get("properties") or {}
                for required in node.get("required") or []:
                    if required not in value:
                        return f"Missing required tool argument: {path}.{required}"
                if node.get("additionalProperties") is False:
                    extras = set(value) - set(properties)
                    if extras:
                        return (
                            f"Unknown tool arguments at {path}: "
                            f"{', '.join(sorted(extras))}"
                        )
                for key, child in value.items():
                    child_schema = properties.get(key)
                    if isinstance(child_schema, dict):
                        error = validate(child, child_schema, f"{path}.{key}")
                        if error:
                            return error
            if isinstance(value, list):
                if len(value) < int(node.get("minItems", 0)):
                    return f"{path} contains too few items"
                if "maxItems" in node and len(value) > int(node["maxItems"]):
                    return f"{path} contains too many items"
                item_schema = node.get("items")
                if isinstance(item_schema, dict):
                    for index, child in enumerate(value):
                        error = validate(
                            child, item_schema, f"{path}[{index}]"
                        )
                        if error:
                            return error
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if "minimum" in node and value < node["minimum"]:
                    return f"{path} is below minimum {node['minimum']}"
                if "maximum" in node and value > node["maximum"]:
                    return f"{path} is above maximum {node['maximum']}"
            return None

        if not isinstance(arguments, dict):
            return "Tool arguments must be an object."
        return validate(arguments, schema, "arguments")

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
