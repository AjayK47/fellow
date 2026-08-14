"""Runtime configuration.

Toda reads exactly one file: ``config.json``. There is no ``.env``, no
environment-variable layer, and nothing writes this file at runtime — an
operator edits it and restarts the gateway. The previous split (a
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
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name

    @property
    def workspaces(self) -> Path:
        return self.workspace_root or (self.data_dir / "workspaces")


class OrganizationConfig(_Section):
    """The single tenant this gateway serves.

    ``toda init`` creates this organization, its public scope, and the owner
    dashboard account. The gateway refuses to start until that has happened.
    """

    id: str = "org_acme"
    name: str = "Acme"
    owner_email: str | None = None


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
    embedding_dimensions: int = 96
    timeout_seconds: float = 600.0
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
    # Sandboxed run_code. Every invocation still requires human approval,
    # and callbacks are restricted to a read-only allow-list re-verified on
    # every call. Opt-in: approval is not a substitute for an OS sandbox.
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
    # Root Session Manager Tasks coordinate the conversation and synthesize
    # worker results — generous enough for a long multi-stage request
    # without making the Manager loop unbounded.
    max_iterations: int = 50
    max_tool_calls: int = 100
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
    # Full-trust `pi` coding child-worker subprocess. Unlike run_code this
    # has real network access and no approval gate, so it defaults off.
    pi_coding_enabled: bool = False
    # Pinned here rather than left to Pi's own defaults, which come from the
    # operator's ~/.pi/agent/settings.json -- otherwise changing the model in
    # the Pi TUI silently changes what Toda's coding workers run on.
    pi_provider: str = "openrouter"
    pi_model: str = "z-ai/glm-5.2"
    # Pi discovers its credential from an environment variable, so the key is
    # injected into the child's env rather than passed as `--api-key`, which
    # would expose it in argv to every process on the host. The value is read
    # from models.providers[pi_credential_connection].api_key.
    pi_credential_env: str = "OPENROUTER_API_KEY"
    pi_credential_connection: str = "reasoning"
    pi_checkpoint_interval_seconds: int = 3600
    pi_stale_heartbeat_seconds: int = 1800
    pi_poll_interval_seconds: int = 10
    pi_cancel_grace_seconds: int = 20


class WorkerConfig(_Section):
    enabled: bool = True
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


settings = load_settings()
