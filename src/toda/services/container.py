"""Service container / composition root."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from toda.config import Settings, settings as _module_settings, StorageConfig
from toda.domain.models import new_id
from toda.services.audit import AuditService
from toda.services.authorization import AuthorizationService
from toda.services.context import ContextService
from toda.services.events import EventService
from toda.services.embeddings import build_embedding_provider, instrument_embedding
from toda.services.graph import GraphService
from toda.services.ingestion import IngestionService
from toda.services.indexing import IndexingService
from toda.services.memory import MemoryService
from toda.services.mcp_client import MCPClientService
from toda.services.proactive import ProactiveService
from toda.services.profiles import ProfilesService
from toda.services.retrieval import RetrievalService
from toda.services.retention import RetentionService
from toda.services.web_tools import build_web_tools
from toda.services.skills import SkillsService
from toda.services.skill_packages import SkillPackageCatalog
from toda.services.workspaces import WorkspaceService
from toda.services.signals import SignalsService
from toda.services.reality import RealityEngine
from toda.services.policy import PolicyEngine
from toda.services.artifacts import ArtifactService
from toda.storage.db import BrainStore
from toda.harness.attention import AttentionPipeline
from toda.harness.automations import AutomationService
from toda.harness.composio import ComposioClient
from toda.harness.delivery import DeliveryService
from toda.harness.extraction import ModelMemoryExtractor
from toda.harness.history_compaction import HistoryCompactor
from toda.harness.evaluation import HarnessEvaluationService
from toda.harness.jobs import JobQueue
from toda.harness.memory_capture import HarnessMemoryCapture
from toda.harness.model_client import build_model_clients, build_vision_model, instrument_model
from toda.harness.media_providers import (
    build_image_provider,
    build_speech_provider,
    build_transcription_provider,
)
from toda.harness.media_ingestion import SlackMediaIngestion
from toda.harness.router import EventRouter
from toda.harness.runtime import CompanyAgent
from toda.harness.slack import SlackGateway, SlackSender
from toda.harness.skill_validation import SkillResponseValidator
from toda.harness.tool_output_summary import ToolOutputSummarizer
from toda.harness.action_review import ActionReviewer
from toda.harness.tools import ToolExecutor, ToolDefinition, build_tool_registry
from toda.harness.typed_tools import BrowserTools, DocumentTools, VisionMediaTools
from toda.harness.visual_tools import VisualTools
from toda.harness.worker import HarnessWorker
from toda.harness.child_tasks import ChildTaskService
from toda.harness.child_task_executor import ChildTaskExecutor
from toda.harness.pi_task_executor import PiTaskExecutor
from toda.harness.agent_runner import AgentRunner
from toda.harness.session_manager import SessionManagerScheduler
from toda.harness.conversation import ConversationAgent
from toda.harness.skill_curator import SkillCurator
from toda.harness.recovery import RecoveryHeartbeat
from toda.harness.sessions import SessionService
from toda.harness.task_run_store import TaskRunStore
from toda.services.models_catalog import ModelsCatalogService
from toda.services.run_inspection import RunInspectionService


class TodaApp:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        """`settings` overrides the loaded config.json for this container.

        Added so a caller can pin which providers are in play without
        touching the environment or monkeypatching the module. Determinism
        here is decided by whether ``models.assignments`` resolves, so
        "run this offline" previously had no expressible form: the eval
        harness and the 1,000-member benchmark both set TODA_* variables
        that nothing reads, and made real billable embedding calls while
        claiming not to.
        """
        runtime_settings = settings or _module_settings
        path = Path(db_path) if db_path else runtime_settings.storage.db_path
        if db_path is not None and settings is None:
            # A caller supplying an isolated database is asking for an
            # isolated app container as well.  Keeping the process config's
            # workspace roots here makes private task worktrees escape into
            # the operator's real data directory (and fails in sandboxes).
            # Explicit Settings remain authoritative for deployments that
            # intentionally pair a custom database with fixed roots.
            isolated_storage = runtime_settings.storage.model_copy(
                update={
                    "data_dir": path.parent,
                    "workspace_root": path.parent / "workspaces",
                    "agent_workspace_root": path.parent / "agent-workspaces",
                }
            )
            runtime_settings = runtime_settings.model_copy(
                update={"storage": StorageConfig(**isolated_storage.model_dump())}
            )
        self.settings = runtime_settings
        path.parent.mkdir(parents=True, exist_ok=True)
        self.store = BrainStore(path)
        self.store.configure_telemetry(runtime_settings.observability)
        self.audit = AuditService(self.store)
        self.graph = GraphService(self.store)
        self.authz = AuthorizationService(self.store, self.audit)
        self.embeddings = instrument_embedding(
            build_embedding_provider(runtime_settings), telemetry=self.store.telemetry
        )
        self.indexing = IndexingService(self.store, self.embeddings)
        self.memory = MemoryService(self.store, self.graph, self.audit, indexing=self.indexing)
        self.reality = RealityEngine(
            self.store, self.graph, self.audit, embeddings=self.embeddings
        )
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
        # `runtime_settings` is bound once at the top of __init__ -- either
        # the loaded config.json or an explicit override. It used to
        # construct a second Settings here so it would re-read the
        # environment after the setup wizard rewrote .env mid-process;
        # config.json is read once at import and never written at runtime,
        # and rebuilding would silently discard every configured value.
        self.skill_packages = SkillPackageCatalog()
        self.workspaces = WorkspaceService(
            self.store,
            self.audit,
            runtime_settings.storage.workspaces,
            agent_root=runtime_settings.storage.agent_workspaces,
        )
        self.skills = SkillsService(
            self.store,
            self.audit,
            package_catalog=self.skill_packages,
        )
        self.signals = SignalsService(self.store, self.graph, self.audit)
        self.proactive = ProactiveService(
            self.store, self.authz, self.retrieval, self.graph, self.audit, self.signals
        )

        # Managed company-wide harness. These are deliberately composed from
        # small first-party primitives rather than an agent framework.
        self.models_catalog = ModelsCatalogService(
            cache_path=runtime_settings.storage.data_dir / "models-catalog.json",
            url=runtime_settings.models.catalog_url,
            refresh_interval_seconds=runtime_settings.models.catalog_refresh_interval_seconds,
            timeout_seconds=runtime_settings.models.catalog_timeout_seconds,
            enabled=runtime_settings.models.catalog_enabled,
        )
        if runtime_settings.models.catalog_enabled:
            self.models_catalog.refresh_in_background(
                force=bool(self.models_catalog.status().get("fallback"))
            )
        self.run_inspection = RunInspectionService(
            store=self.store,
            settings=runtime_settings,
            catalog=self.models_catalog,
        )
        self.store.run_inspection = self.run_inspection
        # The provider profiles retain transport/setup facts; the picker and
        # inference eligibility use this pinned provider-model catalog.
        from toda.providers import providers
        providers.set_catalog(self.models_catalog)
        (
            self.main_model,
            self.attention_model,
            self.extraction_model,
            self.tool_output_summary_model,
        ) = build_model_clients(runtime_settings)
        self.main_model = instrument_model(
            self.main_model, telemetry=self.store.telemetry, role="main", catalog=self.models_catalog
        )
        self.attention_model = instrument_model(
            self.attention_model, telemetry=self.store.telemetry, role="attention", catalog=self.models_catalog
        )
        self.extraction_model = instrument_model(
            self.extraction_model, telemetry=self.store.telemetry, role="extraction", catalog=self.models_catalog
        )
        self.tool_output_summary_model = instrument_model(
            self.tool_output_summary_model,
            telemetry=self.store.telemetry,
            role="tool_summary",
            catalog=self.models_catalog,
        )
        self.vision_model = build_vision_model(runtime_settings, self.main_model)
        if self.vision_model is not None:
            self.vision_model = instrument_model(
                self.vision_model, telemetry=self.store.telemetry, role="vision", catalog=self.models_catalog
            )
        self.harness_jobs = JobQueue(
            self.store, lease_seconds=runtime_settings.worker.lease_seconds
        )
        self.composio = ComposioClient(
            api_key=runtime_settings.composio.api_key,
            base_url=runtime_settings.composio.base_url,
            connected_account_id=runtime_settings.composio.connected_account_id,
            toolkit_versions=runtime_settings.composio.toolkit_versions,
            webhook_secret=runtime_settings.composio.webhook_secret,
            webhook_tolerance_seconds=(
                runtime_settings.composio.webhook_tolerance_seconds
            ),
            telemetry=self.store.telemetry,
        )
        self.web_tools = build_web_tools(runtime_settings)
        self.document_tools = DocumentTools(self.artifacts)
        self.vision_tools = VisionMediaTools(
            self.artifacts,
            vision_model=self.vision_model,
            image_provider=build_image_provider(runtime_settings),
            transcription_provider=build_transcription_provider(runtime_settings),
            speech_provider=build_speech_provider(runtime_settings),
        )
        self.browser_tools = BrowserTools(
            self.artifacts,
            enabled=runtime_settings.tools.browser_enabled,
            allowed_domains=[
                item.strip().lower()
                for item in runtime_settings.tools.browser_allowed_domains
                if item.strip()
            ],
            vision_tools=self.vision_tools,
            cdp_enabled=runtime_settings.tools.browser_cdp_enabled,
        )
        self.visual_tools = VisualTools(self.artifacts)
        code_runner = AgentRunner(
            execution_user=runtime_settings.agent.pi_execution_user,
            execution_home=runtime_settings.agent.pi_home,
            cpu_seconds=runtime_settings.agent.pi_cpu_seconds,
            max_processes=runtime_settings.agent.pi_max_processes,
            max_file_bytes=runtime_settings.agent.pi_max_file_bytes,
            memory_bytes=runtime_settings.agent.pi_memory_bytes,
        )
        self.sessions = SessionService(
            store=self.store, audit=self.audit, queue=self.harness_jobs
        )
        self.policy = PolicyEngine(
            self.store,
            self.audit,
            external_reader=self._composio_precondition_reader,
        )
        self.slack_sender = SlackSender(bot_token=runtime_settings.slack.bot_token, store=self.store)
        self.slack_media = SlackMediaIngestion(
            store=self.store,
            artifacts=self.artifacts,
            bot_token=runtime_settings.slack.bot_token,
            transcription_provider=self.vision_tools.transcription_provider,
            max_bytes=runtime_settings.media.max_bytes,
        )
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
            run_code_enabled=runtime_settings.tools.run_code_enabled,
            run_code_timeout_seconds=runtime_settings.tools.run_code_timeout_seconds,
            run_code_max_tool_calls=runtime_settings.tools.run_code_max_tool_calls,
            run_code_runner=code_runner,
            run_code_isolation_required=runtime_settings.agent.pi_isolation_required,
            workspace_root=runtime_settings.storage.workspaces,
            workspace_service=self.workspaces,
            document_tools=self.document_tools,
            browser_tools=self.browser_tools,
            vision_tools=self.vision_tools,
            visual_tools=self.visual_tools,
            audit=self.audit,
            session_notifier=self.sessions.notify,
        )
        self.mcp = MCPClientService(
            store=self.store,
            registry=self.tool_registry,
            audit=self.audit,
        )
        self.tool_output_summarizer = ToolOutputSummarizer(
            model=self.tool_output_summary_model
        )
        # Reuses the same small/cheap model client as tool-output
        # summarization rather than provisioning a new one -- both are
        # low-stakes, structured, single-purpose auxiliary calls.
        self.action_reviewer = ActionReviewer(model=self.tool_output_summary_model)
        self.tool_executor = ToolExecutor(
            self.store,
            self.tool_registry,
            self.audit,
            policy_engine=self.policy,
            reality=self.reality,
            artifacts=self.artifacts,
            output_summarizer=self.tool_output_summarizer,
            action_reviewer=self.action_reviewer,
        )
        self.history_compactor = HistoryCompactor(model=self.extraction_model)
        self.skill_response_validator = SkillResponseValidator(
            model=self.extraction_model
        )
        # Every executor gets a fresh TaskRunStore bound to its own lease
        # owner. The public agent is only a dependency bundle; Session Manager
        # and child execution create their own bound instance below.
        def _task_agent_factory(runs: TaskRunStore) -> CompanyAgent:
            return CompanyAgent(
                runs=runs,
                context_service=self.context,
                model=getattr(getattr(self, "company_agent", None), "model", self.main_model),
                tools=self.tool_executor,
                skills=self.skills,
                skill_validator=self.skill_response_validator,
                audit=self.audit,
                enqueue_job=self.harness_jobs.enqueue,
                graph=self.graph,
                history_compactor=self.history_compactor,
                workspaces=self.workspaces,
                max_iterations=runtime_settings.agent.max_iterations,
                max_tool_calls=runtime_settings.agent.max_tool_calls,
                max_model_calls=runtime_settings.agent.max_model_calls,
                max_cost_usd=runtime_settings.agent.max_cost_usd,
                max_duration_seconds=runtime_settings.agent.max_duration_seconds,
                context_token_budget=runtime_settings.agent.context_token_budget,
                max_total_tokens=runtime_settings.agent.max_total_tokens,
                completion_max_tokens=runtime_settings.agent.completion_max_tokens,
                embeddings=self.embeddings,
                run_inspection=self.run_inspection,
                readiness_target=self,
                persona_name=runtime_settings.agent.persona_name,
            )

        self.company_agent = _task_agent_factory(
            TaskRunStore(
                self.store,
                owner=new_id("agent"),
                lease_seconds=runtime_settings.worker.lease_seconds,
            )
        )
        # Repair terminal runs created before automatic lifecycle
        # reconciliation was installed. This is bounded by the organization
        # query and is safe to repeat because completeness is an upsert.
        self.run_inspection.reconcile_terminal_tasks(runtime_settings.organization.id)

        self.conversation_agent = ConversationAgent(
            store=self.store,
            model=None,
            tools=self.tool_executor,
            context_service=self.context,
            task_agent_factory=_task_agent_factory,
            delivery_enqueue=self.harness_jobs.enqueue,
            readiness_target=self,
            persona_name=runtime_settings.agent.persona_name,
            history_compactor=self.history_compactor,
        )
        self.session_manager = SessionManagerScheduler(
            store=self.store,
            authz=self.authz,
            sessions=self.sessions,
            agent_factory=_task_agent_factory,
            conversation_agent=self.conversation_agent,
            lease_seconds=runtime_settings.agent.session_manager_lease_seconds,
            task_lease_seconds=runtime_settings.agent.task_execution_lease_seconds,
        )
        self.child_tasks = ChildTaskService(
            store=self.store,
            queue=self.harness_jobs,
            audit=self.audit,
            artifacts=self.artifacts,
            workspaces=self.workspaces,
            max_parallel=runtime_settings.agent.child_max_parallel,
            max_depth=runtime_settings.agent.child_max_depth,
            max_iterations=runtime_settings.agent.child_max_iterations,
            max_tool_calls=runtime_settings.agent.child_max_tool_calls,
            max_model_calls=runtime_settings.agent.max_model_calls,
            max_cost_usd=runtime_settings.agent.max_cost_usd,
            max_duration_seconds=runtime_settings.agent.max_duration_seconds,
            max_total_tokens=runtime_settings.agent.max_total_tokens,
        )
        self.tool_registry.register(
            self.child_tasks.tool_definition(), self.child_tasks.execute
        )
        self.tool_registry.register(
            self.child_tasks.tool_definition_manage(), self.child_tasks.manage
        )
        self.tool_registry.register(
            self.child_tasks.tool_definition_wait(), self.child_tasks.wait
        )
        self.tool_registry.register(
            self.child_tasks.tool_definition_start_root(), self.child_tasks.start_root
        )
        self.pi_task_executor = PiTaskExecutor(
            store=self.store,
            sessions=self.sessions,
            workspaces=self.workspaces,
            artifacts=self.artifacts,
            audit=self.audit,
            provider=runtime_settings.agent.pi_provider,
            model=runtime_settings.agent.pi_model,
            credential_env=runtime_settings.agent.pi_credential_env,
            execution_user=runtime_settings.agent.pi_execution_user,
            isolation_required=runtime_settings.agent.pi_isolation_required,
            execution_home=runtime_settings.agent.pi_home,
            cpu_seconds=runtime_settings.agent.pi_cpu_seconds,
            max_processes=runtime_settings.agent.pi_max_processes,
            max_file_bytes=runtime_settings.agent.pi_max_file_bytes,
            memory_bytes=runtime_settings.agent.pi_memory_bytes,
            credential=getattr(
                runtime_settings.models.providers.get(
                    runtime_settings.agent.pi_credential_connection
                ),
                "api_key",
                None,
            ),
            checkpoint_interval_seconds=runtime_settings.agent.pi_checkpoint_interval_seconds,
            stale_heartbeat_seconds=runtime_settings.agent.pi_stale_heartbeat_seconds,
            poll_interval_seconds=runtime_settings.agent.pi_poll_interval_seconds,
            cancel_grace_seconds=runtime_settings.agent.pi_cancel_grace_seconds,
            action_reviewer=self.action_reviewer,
        )
        self.child_task_executor = ChildTaskExecutor(
            store=self.store,
            queue=self.harness_jobs,
            sessions=self.sessions,
            agent_factory=_task_agent_factory,
            workspaces=self.workspaces,
            # Only wire Pi when the actual `pi` CLI is expected to be present.
            # worker_type="pi" children fail closed with a clear error
            # otherwise (ChildTaskExecutor.run), never silently no-op.
            pi_executor=self.pi_task_executor if runtime_settings.agent.pi_coding_enabled else None,
        )
        # Back-references for code built before pi_task_executor existed --
        # same circular-ish ordering already worked around for
        # company_agent/child tasks above.
        self.sessions.pi_executor = self.child_task_executor.pi_executor
        self.child_tasks.pi_executor = self.child_task_executor.pi_executor

        self.attention = AttentionPipeline(
            store=self.store,
            retrieval=self.retrieval,
            model=self.attention_model,
            queue=self.harness_jobs,
            audit=self.audit,
            config=runtime_settings,
            sessions=self.sessions,
        )
        self.event_router = EventRouter(
            store=self.store,
            ingestion=self.ingestion,
            attention=self.attention,
            queue=self.harness_jobs,
            audit=self.audit,
            graph=self.graph,
            config=runtime_settings,
            sessions=self.sessions,
            slack_sender=self.slack_sender,
            slack_media=self.slack_media,
        )
        self.slack_gateway = SlackGateway(
            store=self.store,
            queue=self.harness_jobs,
            graph=self.graph,
            config=runtime_settings,
            audit=self.audit,
            sessions=self.sessions,
            reality=self.reality,
        )
        self.model_memory_extractor = ModelMemoryExtractor(
            model=self.extraction_model,
            memory_service=self.memory,
            store=self.store,
            audit=self.audit,
            reality=self.reality,
        )
        self.harness_memory = HarnessMemoryCapture(
            store=self.store,
            ingestion=self.ingestion,
            audit=self.audit,
            model_extractor=self.model_memory_extractor,
            embeddings=self.embeddings,
            skills=self.skills,
            history_compactor=self.history_compactor,
        )
        self.skill_curator = SkillCurator(
            store=self.store,
            skills=self.skills,
            model=self.extraction_model,
            audit=self.audit,
        )
        self.delivery = DeliveryService(
            store=self.store,
            authz=self.authz,
            slack_sender=self.slack_sender,
            audit=self.audit,
            # The small auxiliary model writes the Task acknowledgement --
            # it has to land while the main model is still on its first
            # turn, so it cannot be the main model itself.
            model=self.extraction_model,
            persona_name=runtime_settings.agent.persona_name,
        )
        self.harness_worker = HarnessWorker(
            queue=self.harness_jobs,
            router=self.event_router,
            attention=self.attention,
            memory_capture=self.harness_memory,
            delivery=self.delivery,
            ingestion=self.ingestion,
            indexing=self.indexing,
            profiles=self.profiles,
            batch_size=runtime_settings.worker.batch_size,
            concurrency=runtime_settings.worker.concurrency,
            knowledge_concurrency=runtime_settings.worker.knowledge_concurrency,
            store=self.store,
            task_agent_factory=_task_agent_factory,
            session_manager=self.session_manager,
            sessions=self.sessions,
            run_inspection=self.run_inspection,
        )
        self.harness_worker.register("task.run_child", self.child_task_executor.run)
        self.harness_worker.register("task.pi_poll", self.child_task_executor.poll_pi)
        self.harness_worker.register(
            "skill.curate_task",
            lambda payload: self.skill_curator.review(payload["task_id"]),
        )
        self.recovery_heartbeat = RecoveryHeartbeat(
            store=self.store,
            sessions=self.sessions,
            queue=self.harness_jobs,
            workspaces=self.workspaces,
            workspace_retention_hours=runtime_settings.agent.pi_workspace_retention_hours,
        )
        self.harness_worker.register(
            "system.recover",
            lambda payload: self.recovery_heartbeat.tick(payload["organization_id"]),
        )
        self.harness_worker.register(
            "proactive.heartbeat",
            lambda payload: self.proactive.heartbeat(payload["organization_id"]),
        )
        self.automations = AutomationService(
            store=self.store,
            sessions=self.sessions,
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
                description=(
                    "Create a durable scheduled automation that wakes this agent "
                    "on an interval or cron schedule. Executes immediately."
                ),
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
                # Native allow -- no reviewer call, no human pause, in every
                # policy mode. See NATIVE_ALLOW_TOOL_NAMES in harness/tools.py
                # for why scheduling is treated as an internal control surface
                # rather than an external effect.
                approval_required=False,
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
                description="Enable or pause one organization automation.",
                input_schema={"type": "object", "properties": {"automation_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["automation_id", "enabled"], "additionalProperties": False},
                risk="write", approval_required=False,
            ),
            lambda args, runtime: self.automations.set_enabled(
                _automation_owned(args["automation_id"], runtime["organization_id"])["id"], bool(args["enabled"])
            ),
        )
        self.tool_registry.register(
            ToolDefinition(
                name="automation_run_now",
                description="Fire one enabled automation immediately.",
                input_schema={"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"], "additionalProperties": False},
                risk="write", approval_required=False,
            ),
            lambda args, runtime: self._run_automation_now(
                _automation_owned(args["automation_id"], runtime["organization_id"])
            ),
        )
        self.tool_registry.register(
            ToolDefinition(
                name="automation_delete",
                description="Delete one organization automation.",
                input_schema={"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"], "additionalProperties": False},
                risk="write", approval_required=False,
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
        self.harness_worker.register(
            "connector.subscribe_triggers", self._subscribe_connector_triggers
        )
        self.harness_worker.register(
            "connector.unsubscribe_triggers", self._unsubscribe_connector_triggers
        )
        self.harness_evaluation = HarnessEvaluationService(
            store=self.store, audit=self.audit, reality=self.reality
        )
        self.retention = RetentionService(store=self.store, audit=self.audit)
        # Materialize the immutable package/capability indexes for the default
        # tenant.  These are projections only; package files and registry
        # definitions remain authoritative and can recreate them after reset.
        try:
            self.skills.ensure_platform_skills(runtime_settings.organization.id)
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
                    "organization_id": runtime_settings.organization.id,
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

    def _composio_precondition_reader(self, **kwargs: object) -> dict[str, object]:
        connected_account_id = kwargs.get("connected_account_id")
        account = (
            self.store.connector_account_by_external_id(str(connected_account_id))
            if connected_account_id
            else None
        )
        return self.composio.execute(
            str(kwargs["action_slug"]),
            dict(kwargs["arguments"]),  # type: ignore[arg-type]
            connected_account_id=str(connected_account_id) if connected_account_id else None,
            user_id=(account or {}).get("composio_user_id"),
        )

    def _run_automation_now(self, automation: dict[str, object]) -> dict[str, object]:
        """Queue one automation immediately while retaining normal tick semantics."""
        return self.automations.run_now(
            str(automation["id"]), str(automation["organization_id"])
        )

    def _subscribe_connector_triggers(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Subscribe a newly connected account to its toolkit's triggers.

        Runs as a job rather than inline in the connect request: GitHub
        alone offers 46 trigger types, so doing this synchronously would
        hold the HTTP response open for the whole round trip and fail the
        connection if any one of them errored.
        """
        # Registering a connector account must not acquire a network
        # dependency: an installation without Composio configured (a fresh
        # checkout, the test suite) would otherwise enqueue a job that fails,
        # retries and dead-letters on every connection.
        if not self.composio.configured:
            return {"skipped": "composio_not_configured"}
        account = self.store.connector_account_by_external_id(
            str(payload["connected_account_id"])
        )
        if not account:
            return {"skipped": "account_not_active"}
        result = self.composio.subscribe_all_triggers(
            toolkit=str(account["toolkit"]),
            connected_account_id=str(account["connected_account_id"]),
            user_id=account.get("composio_user_id"),
        )
        self.audit.record(
            account["organization_id"],
            "connector.triggers_subscribed",
            {
                "toolkit": result["toolkit"],
                "subscribed": result["subscribed"],
                "needs_config": [item["slug"] for item in result["needs_config"]],
            },
            principal_id=account.get("principal_id"),
        )
        return result

    def _unsubscribe_connector_triggers(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Tear down a disconnected account's triggers.

        Without this, disconnecting leaves the triggers live on Composio's
        side, still firing for a connection the organization believes it
        removed.
        """
        if not self.composio.configured:
            return {"skipped": "composio_not_configured"}
        connected_account_id = str(payload["connected_account_id"])
        result = self.composio.unsubscribe_all_triggers(
            connected_account_id=connected_account_id
        )
        self.audit.record(
            str(payload["organization_id"]),
            "connector.triggers_unsubscribed",
            {
                "connected_account_id": connected_account_id,
                "removed": len(result["removed"]),
                "failed": [item["slug"] for item in result["failed"]],
            },
        )
        return result

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
                        user_id=account.get("composio_user_id"),
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
        # Drain the worker's shared thread pool before closing the database
        # underneath it -- an in-flight job still needs the store.
        self.harness_worker.close()
        self.store.close()


_app: TodaApp | None = None


def get_app(db_path: Path | str | None = None) -> TodaApp:
    global _app
    if _app is None:
        _app = TodaApp(db_path=db_path)
    return _app


def reset_app(db_path: Path | str | None = None) -> TodaApp:
    global _app
    if _app is not None:
        _app.close()
    _app = TodaApp(db_path=db_path)
    return _app
