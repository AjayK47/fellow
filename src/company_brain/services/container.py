"""Service container / composition root."""

from __future__ import annotations

from pathlib import Path

from company_brain.config import settings
from company_brain.services.audit import AuditService
from company_brain.services.authorization import AuthorizationService
from company_brain.services.context import ContextService
from company_brain.services.events import EventService
from company_brain.services.embeddings import build_embedding_provider
from company_brain.services.graph import GraphService
from company_brain.services.ingestion import IngestionService
from company_brain.services.indexing import IndexingService
from company_brain.services.memory import MemoryService
from company_brain.services.proactive import ProactiveService
from company_brain.services.profiles import ProfilesService
from company_brain.services.retrieval import RetrievalService
from company_brain.services.retention import RetentionService
from company_brain.services.runs import RunsService
from company_brain.services.skills import SkillsService
from company_brain.services.signals import SignalsService
from company_brain.storage.db import BrainStore
from company_brain.harness.attention import AttentionPipeline
from company_brain.harness.automations import AutomationService
from company_brain.harness.composio import ComposioClient
from company_brain.harness.delivery import DeliveryService
from company_brain.harness.extraction import ModelMemoryExtractor
from company_brain.harness.evaluation import HarnessEvaluationService
from company_brain.harness.jobs import JobQueue
from company_brain.harness.memory_capture import HarnessMemoryCapture
from company_brain.harness.model_client import build_model_clients
from company_brain.harness.router import EventRouter
from company_brain.harness.run_store import AgentRunStore
from company_brain.harness.runtime import CompanyAgent
from company_brain.harness.slack import SlackGateway, SlackSender
from company_brain.harness.tools import ToolExecutor, build_tool_registry
from company_brain.harness.worker import HarnessWorker


class BrainApp:
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
        self.ingestion = IngestionService(self.store, self.memory, self.audit, indexing=self.indexing)
        self.events = EventService(self.store, self.ingestion, self.audit)
        self.retrieval = RetrievalService(self.store, self.authz, self.graph, embeddings=self.embeddings)
        self.context = ContextService(self.store, self.retrieval, self.graph, self.audit)
        self.profiles = ProfilesService(self.store, self.retrieval, self.graph, self.audit)
        self.skills = SkillsService(self.store, self.audit)
        self.runs = RunsService(self.store, self.graph, self.audit)
        self.signals = SignalsService(self.store, self.graph, self.audit)
        self.proactive = ProactiveService(
            self.store, self.authz, self.retrieval, self.graph, self.audit, self.signals
        )

        # Managed company-wide harness. These are deliberately composed from
        # small first-party primitives rather than an agent framework.
        runtime_settings = type(settings)()
        self.main_model, self.attention_model, self.extraction_model = (
            build_model_clients(runtime_settings)
        )
        self.harness_jobs = JobQueue(
            self.store, lease_seconds=runtime_settings.worker_lease_seconds
        )
        self.agent_runs = AgentRunStore(self.store)
        self.composio = ComposioClient(
            api_key=runtime_settings.composio_api_key,
            base_url=runtime_settings.composio_base_url,
            connected_account_id=runtime_settings.composio_connected_account_id,
            toolkit_versions=runtime_settings.composio_toolkit_versions,
        )
        self.tool_registry = build_tool_registry(
            store=self.store,
            retrieval=self.retrieval,
            context=self.context,
            skills=self.skills,
            composio=self.composio,
        )
        self.tool_executor = ToolExecutor(
            self.store, self.tool_registry, self.audit
        )
        self.company_agent = CompanyAgent(
            runs=self.agent_runs,
            context_service=self.context,
            model=self.main_model,
            tools=self.tool_executor,
            audit=self.audit,
            enqueue_job=self.harness_jobs.enqueue,
            graph=self.graph,
            max_iterations=runtime_settings.agent_max_iterations,
            max_tool_calls=runtime_settings.agent_max_tool_calls,
            context_token_budget=runtime_settings.agent_context_token_budget,
            max_total_tokens=runtime_settings.agent_max_total_tokens,
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
        self.slack_sender = SlackSender(bot_token=runtime_settings.slack_bot_token)
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
        self.harness_worker.register(
            "automations.tick",
            lambda payload: self.automations.tick(payload.get("organization_id")),
        )
        self.harness_evaluation = HarnessEvaluationService(
            store=self.store, audit=self.audit
        )
        self.retention = RetentionService(store=self.store, audit=self.audit)

    def close(self) -> None:
        self.store.close()


_app: BrainApp | None = None


def get_app(db_path: Path | str | None = None) -> BrainApp:
    global _app
    if _app is None:
        _app = BrainApp(db_path=db_path)
    return _app


def reset_app(db_path: Path | str | None = None) -> BrainApp:
    global _app
    if _app is not None:
        _app.close()
    _app = BrainApp(db_path=db_path)
    return _app
