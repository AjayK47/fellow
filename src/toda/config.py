"""Runtime configuration.

Toda reads exactly one file: ``config.json``. There is no ``.env`` and no
environment-variable layer. An administrator may update validated model
assignments through the dashboard; the gateway writes the same file atomically
and requires a restart. The previous split (a
hand-edited ``.env`` plus a setup wizard writing back into that same file)
kept two sources of truth that drifted: roughly half the keys in a typical
``.env`` were no longer ``Settings`` fields at all and were silently
dropped by ``extra="ignore"``, so operators were editing keys that did
nothing.

Resolution order for the file:

1. ``$TODA_CONFIG`` — explicit path, wins over everything.
2. ``<repo>/config.json`` — the directory containing ``pyproject.toml``,
   for a source checkout.
3. ``<data_dir>/config.json`` — the installed location, alongside the
   database and workspaces.

The one thing still read from the environment is ``TODA_CONFIG`` itself,
because a config file cannot tell you where it lives.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from toda.providers import ModelAssignment, ProviderConnection, ToolConnection


class _Section(BaseModel):
    """Reject unknown keys.

    The old .env silently dropped anything that was not a field, so roughly
    half the keys in a typical operator's file did nothing and there was no
    way to find out. A misspelled key in config.json must fail loudly.
    """

    model_config = ConfigDict(extra="forbid")


def _default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Toda"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Toda"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "toda"


def _repo_root() -> Path | None:
    """The directory holding pyproject.toml, walking up from this module.

    Deliberately anchored to the module's own location rather than the
    process working directory. The old loader walked up from ``cwd``, which
    meant running Toda from inside any unrelated Python project picked up
    that project's config file instead.
    """
    for directory in Path(__file__).resolve().parents:
        if (directory / "pyproject.toml").is_file():
            return directory
    return None


def config_path() -> Path:
    configured = os.environ.get("TODA_CONFIG")
    if configured:
        return Path(configured).expanduser()
    root = _repo_root()
    if root is not None and (root / "config.json").is_file():
        return root / "config.json"
    return _default_data_dir() / "config.json"


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "info"
    # Public URL for this gateway (e.g. an ngrok/cloudflared tunnel), used
    # to auto-register the Composio webhook target on startup.
    public_url: str | None = None
    # Bearer tokens for programmatic ingestion from outside localhost.
    # Console/API users authenticate through the dashboard session instead.
    api_keys: list[str] = Field(default_factory=list)


class StorageConfig(_Section):
    data_dir: Path = Field(default_factory=_default_data_dir)
    db_name: str = "brain.db"
    workspace_root: Path | None = None
    # Durable organization files and disposable agent worktrees are separate
    # roots.  Operators can place the latter on an agent-owned volume while
    # keeping the database, connector state, and control-plane files in
    # ``data_dir``.
    agent_workspace_root: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir.expanduser() / self.db_name

    @property
    def workspaces(self) -> Path:
        return (self.workspace_root or (self.data_dir / "workspaces")).expanduser()

    @property
    def agent_workspaces(self) -> Path:
        default = self.data_dir.expanduser().parent / f"{self.data_dir.expanduser().name}-agent-workspaces"
        return (self.agent_workspace_root or default).expanduser()


class OrganizationConfig(_Section):
    """The single tenant this gateway serves.

    ``toda init`` creates this organization, its public scope, and the owner
    dashboard account. The gateway refuses to start until that has happened.
    """

    id: str = "org_acme"
    name: str = "Acme"
    owner_email: str | None = None


class ModelPolicyConfig(_Section):
    """Organization policy governing model selection, pricing ceilings, and rankings."""

    allow_models: list[str] = Field(default_factory=list)
    deny_models: list[str] = Field(default_factory=list)
    min_context: int | None = None
    max_input_price_per_1m: float | None = None
    max_output_price_per_1m: float | None = None
    allow_unknown_price: bool = False
    preference_order: list[str] = Field(default_factory=list)


class ModelsConfig(_Section):
    """Credentials belong to named provider connections; model roles
    reference a connection plus a model id, so a key is stored once no
    matter how many roles use it.

    ``assignments`` stays an open mapping rather than a fixed set of fields
    because the resolver looks roles up by string and iterates the whole
    mapping (see harness/model_client.py and harness/media_providers.py).
    """

    providers: dict[str, ProviderConnection] = Field(default_factory=dict)
    assignments: dict[str, ModelAssignment] = Field(default_factory=dict)
    policy: ModelPolicyConfig = Field(default_factory=ModelPolicyConfig)
    embedding_dimensions: int = 96
    timeout_seconds: float = 600.0
    catalog_enabled: bool = True
    catalog_url: str = "https://models.dev/catalog.json"
    catalog_refresh_interval_seconds: int = 86_400
    catalog_timeout_seconds: float = 5.0
    # Enables the underlying model's native extended-thinking tokens via the
    # unified `reasoning` request field. Applied only to the main agent
    # model, not the small auxiliary classifiers, where deeper reasoning
    # adds cost and latency for no real benefit. One of "low"/"medium"/
    # "high", or null to disable for a model that rejects the field.
    main_reasoning_effort: str | None = "medium"
    main_supports_vision: bool = False


class SlackConfig(_Section):
    """This operator's own workspace, configured directly.

    There is no OAuth app-distribution flow: one gateway serves one
    workspace, and its credentials come from this file.
    """

    signing_secret: str | None = None
    bot_token: str | None = None
    app_token: str | None = None
    bot_user_id: str | None = None
    require_mention: bool = True
    free_response_channels: list[str] = Field(default_factory=list)
    allowed_channels: list[str] = Field(default_factory=list)
    proactive_channels: list[str] = Field(default_factory=list)
    signature_tolerance_seconds: int = 300


class ComposioConfig(_Section):
    api_key: str | None = None
    base_url: str = "https://backend.composio.dev/api/v3.1"
    connected_account_id: str | None = None
    toolkit_versions: str = "latest"
    webhook_secret: str | None = None
    webhook_tolerance_seconds: int = 300


class ToolsConfig(_Section):
    # Search and extraction providers, keyed by connection name and
    # referenced below. Fetching public pages needs no provider key.
    connections: dict[str, ToolConnection] = Field(default_factory=dict)
    web_search_connection: str | None = None
    web_extract_connection: str | None = None
    web_fetch_enabled: bool = True
    web_fetch_max_chars: int = 18_000
    # Sandboxed run_code. Callbacks are restricted to a read-only allow-list
    # re-verified on every call, and every invocation goes through the
    # action reviewer, which may approve it outright rather than escalating
    # to a person -- only the `supervised` and `restricted` tool-policy
    # modes guarantee a human sees it first. Opt-in, and defaults off,
    # because neither review nor approval is a substitute for an OS sandbox:
    # this runs a subprocess on the host.
    run_code_enabled: bool = False
    run_code_timeout_seconds: int = 900
    run_code_max_tool_calls: int = 100
    browser_enabled: bool = False
    browser_allowed_domains: list[str] = Field(default_factory=list)
    browser_cdp_enabled: bool = False


class MediaConfig(_Section):
    transcription_language: str | None = None
    tts_voice: str = "alloy"
    tts_format: str = "mp3"
    max_bytes: int = 25 * 1024 * 1024


class AgentConfig(_Section):
    # Development/tests may run with deterministic offline clients. A
    # supervised pilot must opt in to this gate so a new Slack/API/dashboard
    # Task cannot start until behavioral readiness is real-world ready.
    require_real_world_readiness: bool = False
    # Root Session Manager Tasks coordinate the conversation and synthesize
    # worker results — generous enough for a long multi-stage request
    # without making the Manager loop unbounded.
    max_iterations: int = 50
    max_tool_calls: int = 100
    max_model_calls: int = 100
    max_cost_usd: float | None = 10.0
    max_duration_seconds: int = 3_600
    context_token_budget: int = 200_000
    max_total_tokens: int = 2_000_000
    # Output budget for a single completion, distinct from the input-side
    # context budget. Too small and a turn producing several parallel tool
    # calls, or one call embedding a large document body, gets cut off
    # mid-JSON: unparseable arguments and a failed run.
    completion_max_tokens: int = 40_000
    child_max_parallel: int = 3
    child_max_depth: int = 1
    # Temporary specialists get a larger envelope than their parent Manager
    # because they may own an entire research or execution sequence.
    child_max_iterations: int = 200
    child_max_tool_calls: int = 500
    # A lease is a renewable ownership window, not a runtime limit: every
    # reasoning iteration extends it.
    session_manager_lease_seconds: int = 3600
    task_execution_lease_seconds: int = 3600
    # `pi` coding child-worker subprocess. It has network access and no human
    # approval pause for routine work, so it defaults off and is isolated by a
    # dedicated Unix account when enabled.
    pi_coding_enabled: bool = False
    # When Pi is enabled, provider and model must be explicitly configured
    # by the operator. There is no hidden default model ID.
    pi_provider: str | None = None
    pi_model: str | None = None
    # Pi discovers its credential from an environment variable, so the key is
    # injected into the child's env rather than passed as `--api-key`, which
    # would expose it in argv to every process on the host. The value is read
    # from models.providers[pi_credential_connection].api_key.
    pi_credential_env: str = "OPENROUTER_API_KEY"
    pi_credential_connection: str = "reasoning"
    # Optional low-privilege Unix account for Pi. When configured, the Toda
    # service must have permission to launch this account (normally via a
    # dedicated runner/service policy); otherwise startup of a Pi task fails
    # closed instead of silently running as the control-plane user.
    pi_execution_user: str | None = None
    # Temporary directories and environment scrubbing are not an OS boundary
    # when Pi shares the control-plane Unix account. Keep this true in hosted
    # deployments so Pi fails closed until a dedicated account is configured.
    pi_isolation_required: bool = True
    # Agent-owned home directory. Pi never receives Toda's control-plane home.
    pi_home: Path | None = None
    pi_cpu_seconds: int | None = 3_600
    pi_max_processes: int = 256
    pi_max_file_bytes: int = 50_000_000
    pi_memory_bytes: int | None = None
    # Completed task copies remain available for review for this long; the
    # recovery heartbeat removes only unreferenced, inactive copies after it.
    pi_workspace_retention_hours: int = 24
    pi_checkpoint_interval_seconds: int = 3600
    pi_stale_heartbeat_seconds: int = 1800
    pi_poll_interval_seconds: int = 10
    pi_cancel_grace_seconds: int = 20

    @model_validator(mode="after")
    def _validate_pi_config(self) -> AgentConfig:
        if self.pi_coding_enabled and (not self.pi_provider or not self.pi_model):
            raise ValueError("pi_coding_enabled requires explicit pi_provider and pi_model configuration")
        return self


class WorkerConfig(_Section):
    # There is deliberately no `enabled` flag. It existed for a documented
    # split-process topology (`TODA_WORKER_ENABLED=false` plus a separate
    # `toda-worker`) whose entry point and module no longer exist, so the
    # only thing turning it off could still do was produce a gateway that
    # accepted Slack events and processed no jobs at all, silently. The
    # worker lanes are part of the gateway.
    poll_interval_seconds: float = 0.5
    lease_seconds: int = 60
    batch_size: int = 20
    concurrency: int = 30
    # A separate, smaller pool for chunking/embedding/extraction/episode
    # jobs so an ingest burst can never crowd out event routing, dispatch,
    # or delivery.
    knowledge_concurrency: int = 8
    dispatcher_tick_interval_seconds: float = 30.0
    attention_minimum_confidence: float = 0.72
    proactive_cooldown_seconds: int = 900
    episode_quiet_seconds: int = 300


class DashboardConfig(_Section):
    session_secret: str | None = None
    cookie_secure: bool = False
    auth_disabled: bool = False
    # Dev-only: auto-resolve every write approval instead of pausing for a
    # human. The approval record is still created and immediately marked
    # approved, so the audit trail stays intact.
    auto_approve_writes: bool = False


class ObservabilityConfig(_Section):
    """OpenTelemetry execution telemetry and local trace retention."""

    enabled: bool = True
    service_name: str = "toda"
    environment: str = "development"
    otlp_endpoint: str | None = None
    otlp_headers: dict[str, str] = Field(default_factory=dict)
    export_timeout_seconds: float = 10.0
    capture_payloads: bool = False
    max_payload_bytes: int = 100_000
    retention_days: int = 7
    sample_ratio: float = 1.0
    console_export: bool = False


class Settings(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    organization: OrganizationConfig = Field(default_factory=OrganizationConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    composio: ComposioConfig = Field(default_factory=ComposioConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


class ConfigError(RuntimeError):
    """Raised when config.json exists but cannot be used."""


def load_settings(path: Path | None = None) -> Settings:
    """Read and validate config.json.

    A missing file yields defaults, which is what a fresh checkout wants:
    `toda init` writes the file. A file that exists but is malformed is a
    hard error — silently falling back to defaults there would start the
    gateway with the wrong model, the wrong org, or no Slack credentials,
    and the failure would surface much later as confusing behaviour.
    """
    target = path or config_path()
    if not target.is_file():
        return Settings()
    if os.name != "nt":
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError as exc:
            raise ConfigError(f"cannot inspect permissions for {target}: {exc}") from exc
        if mode & 0o077:
            raise ConfigError(
                f"{target} must be owner-readable only (chmod 600); it may contain credentials"
            )
    try:
        raw: Any = json.loads(target.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} must contain a JSON object, got {type(raw).__name__}")
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{target} is invalid:\n{exc}") from exc


def save_settings(value: Settings, path: Path | None = None) -> Path:
    """Atomically persist validated settings with credential-safe permissions."""
    target = (path or config_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value.model_dump(mode="json"), handle, indent=2, sort_keys=True)
            handle.write("\n")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


settings = load_settings()
