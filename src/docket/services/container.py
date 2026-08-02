"""Service container / composition root."""

from __future__ import annotations

from pathlib import Path
import json

from docket.config import settings
from docket.services.audit import AuditService
from docket.services.authorization import AuthorizationService
from docket.services.context import ContextService
from docket.services.events import EventService
from docket.services.embeddings import build_embedding_provider
from docket.services.graph import GraphService
from docket.services.ingestion import IngestionService
from docket.services.indexing import IndexingService
from docket.services.memory import MemoryService
from docket.services.proactive import ProactiveService
from docket.services.profiles import ProfilesService
from docket.services.retrieval import RetrievalService
from docket.services.retention import RetentionService
from docket.services.web_tools import WebTools
from docket.services.runs import RunsService
from docket.services.skills import SkillsService
from docket.services.skill_packages import SkillPackageCatalog
from docket.services.workspaces import WorkspaceService
from docket.services.signals import SignalsService
from docket.services.reality import RealityEngine
from docket.services.policy import PolicyEngine
from docket.services.artifacts import ArtifactService
from docket.storage.db import BrainStore
from docket.harness.attention import AttentionPipeline
from docket.harness.automations import AutomationService
from docket.harness.composio import ComposioClient
from docket.harness.delivery import DeliveryService
from docket.harness.extraction import ModelMemoryExtractor
from docket.harness.history_compaction import HistoryCompactor
from docket.harness.evaluation import HarnessEvaluationService
from docket.harness.jobs import JobQueue
from docket.harness.memory_capture import HarnessMemoryCapture
from docket.harness.model_client import build_model_clients
from docket.harness.router import EventRouter
from docket.harness.run_store import AgentRunStore
from docket.harness.runtime import CompanyAgent
from docket.harness.slack import SlackGateway, SlackSender
from docket.harness.skill_validation import SkillResponseValidator
from docket.harness.tool_output_summary import ToolOutputSummarizer
from docket.harness.tools import ToolExecutor, ToolDefinition, build_tool_registry
from docket.harness.typed_tools import BrowserTools, DocumentTools, VisionMediaTools
from docket.harness.worker import HarnessWorker
from docket.harness.specialists import SpecialistWorkerService


class DocketApp:
    def __init__(self, db_path: Path | str | None = None) -> None:
        path = Path(db_path) if db_path else settings.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.store = BrainStore(path)
        self.audit = AuditService(self.store)
        self.graph = GraphService(self.store)
        self.authz = AuthorizationService(self.store, self.audit)
        self.embeddings = build_embedding_provider()
        self.indexing = IndexingService(self.store, self.embeddings)
        self.memory = MemoryService(self.store, self.graph, self.audit, indexing=self.indexing)
        self.reality = RealityEngine(self.store, self.graph, self.audit)
        self.artifacts = ArtifactService(self.store, self.audit)
        self.ingestion = IngestionService(
            self.store,
            self.memory,
            self.audit,
            indexing=self.indexing,
            reality=self.reality,
        )
        self.events = EventService(self.store, self.ingestion, self.audit)
        self.retrieval = RetrievalService(
            self.store,
            self.authz,
            self.graph,
            embeddings=self.embeddings,
            reality=self.reality,
        )
        self.context = ContextService(self.store, self.retrieval, self.graph, self.audit)
        self.profiles = ProfilesService(self.store, self.retrieval, self.graph, self.audit)
        runtime_settings = type(settings)()
        self.skill_packages = SkillPackageCatalog()
        self.workspaces = WorkspaceService(
            self.store, self.audit, runtime_settings.workspace_root
        )
        self.skills = SkillsService(
            self.store,
            self.audit,
            package_catalog=self.skill_packages,
        )
        self.runs = RunsService(self.store, self.graph, self.audit)
        self.signals = SignalsService(self.store, self.graph, self.audit)
        self.proactive = ProactiveService(
            self.store, self.authz, self.retrieval, self.graph, self.audit, self.signals
        )

        # Managed company-wide harness. These are deliberately composed from
        # small first-party primitives rather than an agent framework.
        (
            self.main_model,
            self.attention_model,
            self.extraction_model,
            self.tool_output_summary_model,
        ) = build_model_clients(runtime_settings)
        self.harness_jobs = JobQueue(
            self.store, lease_seconds=runtime_settings.worker_lease_seconds
        )
        self.agent_runs = AgentRunStore(self.store)
        self.composio = ComposioClient(
            api_key=runtime_settings.composio_api_key,
            base_url=runtime_settings.composio_base_url,
            connected_account_id=runtime_settings.composio_connected_account_id,
            toolkit_versions=runtime_settings.composio_toolkit_versions,
            webhook_secret=runtime_settings.composio_webhook_secret,
            webhook_tolerance_seconds=(
                runtime_settings.composio_webhook_tolerance_seconds
            ),
        )
        self.web_tools = WebTools(
            brave_api_key=runtime_settings.brave_search_api_key,
            firecrawl_api_key=runtime_settings.firecrawl_api_key,
            firecrawl_base_url=runtime_settings.firecrawl_base_url,
            fetch_enabled=runtime_settings.web_fetch_enabled,
            max_chars=runtime_settings.web_fetch_max_chars,
        )
        self.document_tools = DocumentTools(self.artifacts)
        self.browser_tools = BrowserTools(
            self.artifacts,
            enabled=runtime_settings.browser_enabled,
            allowed_domains=[
                item.strip().lower()
                for item in runtime_settings.browser_allowed_domains.split(",")
                if item.strip()
            ],
        )
        self.vision_tools = VisionMediaTools(
            self.artifacts,
            model=self.main_model,
            vision_enabled=runtime_settings.vision_enabled,
            image_generation_enabled=runtime_settings.image_generation_enabled,
        )
        self.policy = PolicyEngine(
            self.store,
            self.audit,
            external_reader=lambda **kwargs: self.composio.execute(
                kwargs["action_slug"],
                kwargs["arguments"],
                connected_account_id=kwargs.get("connected_account_id"),
            ),
        )
        self.slack_sender = SlackSender(bot_token=runtime_settings.slack_bot_token)
        self.tool_registry = build_tool_registry(
            store=self.store,
            retrieval=self.retrieval,
            context=self.context,
            skills=self.skills,
            composio=self.composio,
            reality=self.reality,
            artifacts=self.artifacts,
            web_tools=self.web_tools,
            slack_sender=self.slack_sender,
            run_code_enabled=runtime_settings.run_code_enabled,
            run_code_timeout_seconds=runtime_settings.run_code_timeout_seconds,
            run_code_max_tool_calls=runtime_settings.run_code_max_tool_calls,
            workspace_root=runtime_settings.workspace_root,
            workspace_service=self.workspaces,
            document_tools=self.document_tools,
            browser_tools=self.browser_tools,
            vision_tools=self.vision_tools,
        )
        self.tool_output_summarizer = ToolOutputSummarizer(
            model=self.tool_output_summary_model
        )
        self.tool_executor = ToolExecutor(
            self.store,
            self.tool_registry,
            self.audit,
            policy_engine=self.policy,
            reality=self.reality,
            artifacts=self.artifacts,
            output_summarizer=self.tool_output_summarizer,
        )
        self.history_compactor = HistoryCompactor(model=self.extraction_model)
        self.skill_response_validator = SkillResponseValidator(
            model=self.extraction_model
        )
        self.company_agent = CompanyAgent(
            runs=self.agent_runs,
            context_service=self.context,
            model=self.main_model,
            tools=self.tool_executor,
            skills=self.skills,
            skill_validator=self.skill_response_validator,
            audit=self.audit,
            enqueue_job=self.harness_jobs.enqueue,
            graph=self.graph,
            history_compactor=self.history_compactor,
            workspaces=self.workspaces,
            max_iterations=runtime_settings.agent_max_iterations,
            max_tool_calls=runtime_settings.agent_max_tool_calls,
            context_token_budget=runtime_settings.agent_context_token_budget,
            max_total_tokens=runtime_settings.agent_max_total_tokens,
        )
        self.specialists = SpecialistWorkerService(
            agent=self.company_agent,
            artifacts=self.artifacts,
            audit=self.audit,
            max_parallel=runtime_settings.specialist_max_parallel,
            max_depth=runtime_settings.specialist_max_depth,
        )
        self.tool_registry.register(
            self.specialists.tool_definition(), self.specialists.execute
        )
        self.tool_registry.register(
            self.specialists.tool_definition().model_copy(
                update={
                    "name": "delegate_task",
                    "description": "Delegate bounded specialist tasks with explicit scopes, capabilities, budgets, and handoff contracts.",
                }
            ),
            self.specialists.execute,
        )
        self.attention = AttentionPipeline(
            store=self.store,
            retrieval=self.retrieval,
            model=self.attention_model,
            agent=self.company_agent,
            queue=self.harness_jobs,
            audit=self.audit,
            config=runtime_settings,
        )
        self.event_router = EventRouter(
            store=self.store,
            ingestion=self.ingestion,
            retrieval=self.retrieval,
            agent=self.company_agent,
            attention=self.attention,
            queue=self.harness_jobs,
            audit=self.audit,
            graph=self.graph,
            config=runtime_settings,
        )
        self.slack_gateway = SlackGateway(
            store=self.store,
            queue=self.harness_jobs,
            graph=self.graph,
            config=runtime_settings,
            audit=self.audit,
        )
        self.model_memory_extractor = ModelMemoryExtractor(
            model=self.extraction_model,
            memory_service=self.memory,
            store=self.store,
            audit=self.audit,
        )
        self.harness_memory = HarnessMemoryCapture(
            store=self.store,
            ingestion=self.ingestion,
            audit=self.audit,
            model_extractor=self.model_memory_extractor,
        )
        self.delivery = DeliveryService(
            store=self.store,
            authz=self.authz,
            slack_sender=self.slack_sender,
            audit=self.audit,
        )
        self.harness_worker = HarnessWorker(
            queue=self.harness_jobs,
            router=self.event_router,
            attention=self.attention,
            agent=self.company_agent,
            memory_capture=self.harness_memory,
            delivery=self.delivery,
            indexing=self.indexing,
            profiles=self.profiles,
            batch_size=runtime_settings.worker_batch_size,
            concurrency=runtime_settings.worker_concurrency,
        )
        self.automations = AutomationService(
            store=self.store,
            authz=self.authz,
            agent=self.company_agent,
            queue=self.harness_jobs,
            audit=self.audit,
        )
        def _automation_owned(automation_id: str, organization_id: str) -> dict[str, object]:
            automation = self.store.get_harness_record("automations", automation_id)
            if not automation or automation.get("organization_id") != organization_id:
                raise KeyError("automation not found")
            return automation

        self.tool_registry.register(
            ToolDefinition(
                name="automation_list",
                description="List scheduled automations for the organization.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                risk="read",
            ),
            lambda _args, runtime: {"automations": self.automations.list(runtime["organization_id"])},
        )
        self.tool_registry.register(
            ToolDefinition(
                name="automation_create",
                description="Create a durable scheduled agent run after approval.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "goal": {"type": "string"},
                        "conversation_id": {"type": "string"}, "destination": {"type": "object", "additionalProperties": True},
                        "interval_seconds": {"type": "integer", "minimum": 60}, "cron_expression": {"type": "string"}, "timezone": {"type": "string"},
                    },
                    "required": ["name", "goal", "conversation_id", "destination"],
                    "additionalProperties": False,
                },
                risk="write",
                approval_required=True,
            ),
            lambda args, runtime: {"automation": self.automations.create(
                organization_id=runtime["organization_id"], name=args["name"], goal=args["goal"],
                principal_id=runtime["principal_id"], conversation_id=args["conversation_id"], destination=args["destination"],
                interval_seconds=args.get("interval_seconds", 3600), cron_expression=args.get("cron_expression"), timezone=args.get("timezone", "UTC"),
            )},
        )
        self.tool_registry.register(
            ToolDefinition(
                name="automation_set_enabled",
                description="Enable or pause one organization automation after approval.",
                input_schema={"type": "object", "properties": {"automation_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["automation_id", "enabled"], "additionalProperties": False},
                risk="write", approval_required=True,
            ),
            lambda args, runtime: self.automations.set_enabled(
                _automation_owned(args["automation_id"], runtime["organization_id"])["id"], bool(args["enabled"])
            ),
        )
        self.tool_registry.register(
            ToolDefinition(
                name="automation_run_now",
                description="Fire one enabled automation immediately after approval.",
                input_schema={"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"], "additionalProperties": False},
                risk="write", approval_required=True,
            ),
            lambda args, runtime: self._run_automation_now(
                _automation_owned(args["automation_id"], runtime["organization_id"])
            ),
        )
        self.tool_registry.register(
            ToolDefinition(
                name="automation_delete",
                description="Delete one organization automation after approval.",
                input_schema={"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"], "additionalProperties": False},
                risk="write", approval_required=True,
            ),
            lambda args, runtime: self.automations.delete(
                _automation_owned(args["automation_id"], runtime["organization_id"])["id"]
            ),
        )
        self.harness_worker.register(
            "automations.tick",
            lambda payload: self.automations.tick(payload.get("organization_id")),
        )
        self.harness_worker.register(
            "connector.backfill", self._process_connector_backfill
        )
        self.harness_evaluation = HarnessEvaluationService(
            store=self.store, audit=self.audit, reality=self.reality
        )
        self.retention = RetentionService(store=self.store, audit=self.audit)
        # Materialize the immutable package/capability indexes for the default
        # tenant.  These are projections only; package files and registry
        # definitions remain authoritative and can recreate them after reset.
        try:
            self.skills.ensure_platform_skills(runtime_settings.default_org_id)
        except RuntimeError:
            # A developer may intentionally run a stripped package checkout;
            # readiness endpoints will report the missing mandatory package.
            pass
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        seen_capabilities: set[str] = set()
        for definition in self.tool_registry.definitions():
            capability = definition.capability_id
            if not capability or capability in seen_capabilities:
                continue
            seen_capabilities.add(capability)
            self.store.upsert_harness_projection(
                "capabilities",
                {
                    "id": f"capability:{capability}",
                    "organization_id": runtime_settings.default_org_id,
                    "status": "registered",
                    "capability_id": capability,
                    "tool_names": [
                        item.name
                        for item in self.tool_registry.definitions()
                        if item.capability_id == capability
                    ],
                    "runtime_ready": any(
                        item.capability_id == capability and item.runtime_ready
                        for item in self.tool_registry.definitions()
                    ),
                    "setup_needed": sorted({
                        need
                        for item in self.tool_registry.definitions()
                        if item.capability_id == capability
                        for need in item.setup_needed
                    }),
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def _run_automation_now(self, automation: dict[str, object]) -> dict[str, object]:
        """Queue one automation immediately while retaining normal tick semantics."""
        from docket.domain.models import utcnow

        automation = dict(automation)
        automation["next_run_at"] = utcnow().isoformat()
        automation["status"] = "enabled"
        automation["updated_at"] = utcnow().isoformat()
        self.automations._upsert(automation)
        return self.automations.tick(str(automation["organization_id"]))

    def _process_connector_backfill(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        run_id = str(payload["sync_run_id"])
        run = self.reality.get_sync_run(run_id)
        if not run:
            raise KeyError(run_id)
        account = self.store.get_harness_record(
            "connector_accounts", run["connector_account_id"]
        )
        if not account:
            raise KeyError(run["connector_account_id"])
        records = list(payload.get("records") or [])
        try:
            ingested = []
            pages = 0
            next_cursor = payload.get("next_cursor")
            while True:
                if not records and payload.get("action_slug"):
                    arguments = dict(payload.get("action_arguments") or {})
                    if next_cursor is not None:
                        arguments[str(payload.get("cursor_argument") or "cursor")] = (
                            next_cursor
                        )
                    remote = self.composio.execute(
                        str(payload["action_slug"]),
                        arguments,
                        connected_account_id=account["connected_account_id"],
                    )
                    records = list(
                        self._nested_value(
                            remote, str(payload.get("items_path") or "data.items")
                        )
                        or remote.get("items")
                        or []
                    )
                    next_cursor = self._nested_value(
                        remote,
                        str(
                            payload.get("next_cursor_path")
                            or "data.next_cursor"
                        ),
                    )
                for item in records:
                    if not isinstance(item, dict):
                        raise ValueError("backfill records must be objects")
                    external_id = item.get("id") or item.get("external_id")
                    if external_id is None:
                        raise ValueError(
                            "every backfill record requires id or external_id"
                        )
                    result = self.ingestion.ingest(
                        organization_id=run["organization_id"],
                        source_system=str(payload["provider"]),
                        source_type=run["object_type"],
                        external_id=str(external_id),
                        content=str(
                            item.get("content")
                            or item.get("text")
                            or item.get("title")
                            or json.dumps(item, default=str)
                        ),
                        scope_id=account.get("scope_id")
                        or "scope_org_public",
                        author_entity_id=account.get("principal_id"),
                        entity_ids=[],
                        metadata={
                            "object_type": run["object_type"],
                            "object_id": str(external_id),
                            "raw_data": item,
                            "connected_account_id": account[
                                "connected_account_id"
                            ],
                            "backfill_sync_run_id": run_id,
                        },
                        learn=bool(account.get("learn", True)),
                        processing_mode="memory"
                        if account.get("learn", True)
                        else "reference",
                        occurred_at=(
                            item.get("occurred_at")
                            or item.get("updated_at")
                            or item.get("created_at")
                        ),
                    )
                    ingested.append(result["source"]["id"])
                self.reality.apply_sync_batch(
                    run_id,
                    provider=str(payload["provider"]),
                    records=records,
                    next_cursor=(
                        str(next_cursor) if next_cursor is not None else None
                    ),
                    scope_id=account.get("scope_id") or "scope_org_public",
                )
                pages += 1
                if (
                    not payload.get("action_slug")
                    or next_cursor is None
                    or pages >= int(payload.get("max_pages") or 100)
                ):
                    break
                records = []
            completed = self.reality.complete_sync(run_id)
            return {
                "sync_run": completed,
                "source_ids": ingested,
                "pages": pages,
            }
        except Exception as exc:
            self.reality.complete_sync(run_id, error=str(exc))
            raise

    @staticmethod
    def _nested_value(value: object, path: str) -> object | None:
        current = value
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def close(self) -> None:
        self.store.close()


_app: DocketApp | None = None


def get_app(db_path: Path | str | None = None) -> DocketApp:
    global _app
    if _app is None:
        _app = DocketApp(db_path=db_path)
    return _app


def reset_app(db_path: Path | str | None = None) -> DocketApp:
    global _app
    if _app is not None:
        _app.close()
    _app = DocketApp(db_path=db_path)
    return _app
