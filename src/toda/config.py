"""Runtime configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from toda.providers import ModelAssignment, ProviderConnection, ToolConnection


def _default_data_dir() -> Path:
    env = os.environ.get("TODA_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Toda"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Toda"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "toda"


def settings_env_file() -> Path:
    """Resolve the gateway env file independently of its launch directory.

    The console is commonly started from ``landing/`` while Toda's ``.env``
    lives at the repository root.  Pydantic otherwise resolves ``env_file``
    against the current working directory and silently reads
    ``landing/.env``.  Prefer an explicit deployment override, then the
    nearest project root, and finally retain the conventional cwd fallback
    for installed deployments without a source checkout.
    """
    configured = os.environ.get("TODA_ENV_FILE")
    if configured:
        return Path(configured).expanduser()

    candidates = (Path.cwd(), Path(__file__).resolve().parent)
    visited: set[Path] = set()
    for start in candidates:
        for directory in (start, *start.parents):
            if directory in visited:
                continue
            visited.add(directory)
            if (directory / "pyproject.toml").is_file():
                return directory / ".env"
    return _default_data_dir() / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TODA_", env_file=settings_env_file(), extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = _default_data_dir()
    workspace_root: Path = Field(default_factory=lambda: _default_data_dir() / "workspaces")
    db_name: str = "brain.db"
    default_org_id: str = "org_acme"
    log_level: str = "info"
    # Comma-separated bearer tokens for programmatic ingestion from outside
    # localhost. Console/API users authenticate through the Slack-backed
    # session; Slack and connector webhooks retain their signature checks.
    api_keys: str = ""
    # Hermes-style configuration: credentials belong to named provider
    # connections, while model roles reference a connection + model id.
    # BaseSettings accepts these as JSON in TODA_PROVIDER_CONNECTIONS and
    # TODA_MODEL_ASSIGNMENTS, which lets setup persist the entire graph in
    # the local 0600 .env without duplicating secrets per capability.
    provider_connections: dict[str, ProviderConnection] = Field(default_factory=dict)
    model_assignments: dict[str, ModelAssignment] = Field(default_factory=dict)
    tool_connections: dict[str, ToolConnection] = Field(default_factory=dict)
    embedding_dimensions: int = 96
    model_timeout_seconds: float = 600.0
    # Ported from Hermes: enables the underlying model's native extended-
    # thinking/reasoning tokens via OpenRouter's unified `reasoning` request
    # field (works across Claude extended thinking, o1/o3, DeepSeek R1,
    # Gemini thinking, etc. without provider-specific code), so the main
    # agent loop genuinely deliberates -- including deciding whether to
    # delegate -- before producing its visible response or tool calls,
    # instead of reacting turn by turn with no internal reasoning space.
    # Only applied to the main chat model, not the small auxiliary
    # classifiers (attention/extraction/tool-output-summary), where deeper
    # reasoning would only add cost and latency for no real benefit. One
    # of "low"/"medium"/"high", or None to disable (e.g. a configured
    # model that rejects the extra field).
    main_model_reasoning_effort: str | None = "medium"
    # General web research. Search is opt-in because Brave uses a metered API;
    # fetching public pages needs no provider key and remains read-only.
    web_search_connection: str | None = None
    web_extract_connection: str | None = None
    web_fetch_enabled: bool = True
    web_fetch_max_chars: int = 18_000
    # H-07 (rescoped): sandboxed run_code tool. Every invocation still
    # requires human approval (risk="write", approval_required=True) before
    # it runs, and callbacks are restricted to a read-only allow-list
    # re-verified on every call. It is opt-in for the public distribution:
    # approval is not a substitute for an OS-level sandbox.
    run_code_enabled: bool = False
    run_code_timeout_seconds: int = 900
    run_code_max_tool_calls: int = 100
    # Phase D: full-trust `pi` coding child-worker subprocess. Unlike run_code
    # this has real network access and no approval gate (see PiTaskExecutor's
    # hardline floor), so it defaults off and must be explicitly enabled.
    pi_coding_enabled: bool = False
    # Phase 5: durable worker_type=pi Task runtime (pi_task_executor.py).
    # No total wall-clock timeout -- only heartbeat staleness (crash/hang
    # detection) and the checkpoint interval (periodic Manager judgment
    # call on a still-running attempt, plan §10.3.1) bound it.
    pi_checkpoint_interval_seconds: int = 3600
    pi_stale_heartbeat_seconds: int = 1800
    pi_poll_interval_seconds: int = 10
    pi_cancel_grace_seconds: int = 20
    # Root Session Manager Tasks coordinate the conversation and synthesize
    # worker results. These are deliberately generous enough for a long,
    # multi-stage request without making the normal Manager loop unbounded.
    agent_max_iterations: int = 50
    agent_max_tool_calls: int = 100
    agent_context_token_budget: int = 200_000
    agent_max_total_tokens: int = 2_000_000
    # Output budget for a single model completion in the loop -- distinct
    # from the input-side context_token_budget above. Too small and a
    # turn producing several parallel tool calls, or one tool call
    # embedding a large text argument (e.g. a full document/report body),
    # gets cut off mid-JSON: unparseable tool arguments, a failed run, and
    # no way to tell from the error alone whether the model just did too
    # much in one turn.
    agent_completion_max_tokens: int = 40_000
    child_task_max_parallel: int = 3
    child_task_max_depth: int = 1
    # Temporary general specialists get a substantially larger work envelope
    # than their parent Manager because they may own an entire research,
    # analysis, or execution sequence end to end. Pi workers are instead
    # governed by their durable heartbeat/checkpoint lifecycle above.
    child_agent_max_iterations: int = 200
    child_agent_max_tool_calls: int = 500
    # A Manager lease is a renewable ownership window, not a runtime limit.
    # Every reasoning iteration extends it by another hour.
    session_manager_lease_seconds: int = 3600
    task_execution_lease_seconds: int = 3600

    # Browser and media capabilities are configured independently from the
    # conversational model.  A deployment may use a text-only main model,
    # a dedicated vision model, and completely different vendors for image,
    # transcription, and speech generation.
    browser_enabled: bool = False
    browser_allowed_domains: str = ""
    browser_cdp_enabled: bool = False
    main_model_supports_vision: bool = False
    transcription_language: str | None = None
    tts_voice: str = "alloy"
    tts_format: str = "mp3"
    media_max_bytes: int = 25 * 1024 * 1024

    # Slack Events API / Web API.
    slack_signing_secret: str | None = None
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    slack_bot_user_id: str | None = None
    slack_require_mention: bool = True
    slack_free_response_channels: str = ""
    slack_allowed_channels: str = ""
    slack_proactive_channels: str = ""
    slack_signature_tolerance_seconds: int = 300

    # OAuth app-distribution credentials (identify the Slack App itself, the
    # same pair for every workspace that installs it) — distinct from
    # slack_bot_token/slack_app_token above, which configure this operator's
    # own primary workspace directly. Required only for the public
    # /slack/install -> /v1/slack/oauth/callback flow.
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    # users:read.email is what makes a Slack account joinable to the same
    # person in every other connected system -- email is the one identifier
    # Gmail, GitHub, Linear and the rest all agree on. Slack withholds the
    # address entirely without it, and adding a scope later forces every
    # workspace to reinstall, so it belongs in the default set.
    # Deliberately least-privilege. Every outbound path -- slack_send_message,
    # DeliveryService, automations -- targets the destination the run came
    # from, and Slack only delivers events for channels the bot was added to,
    # so `chat:write` always suffices. `chat:write.public` would additionally
    # let Toda keep posting into a channel it was *removed* from (a stale
    # automation destination, say), and being kicked out should stop the
    # messages. `im:write` is likewise unused: there is no conversations.open
    # call, so Toda never opens a DM, it only replies into one that already
    # exists because somebody messaged it.
    slack_oauth_scopes: str = (
        "app_mentions:read,channels:history,channels:read,chat:write,"
        "groups:history,groups:read,im:history,mpim:history,mpim:read,"
        "reactions:write,users:read,users:read.email,files:read,files:write"
    )

    # Composio connections, triggers, webhook ingestion, and tool execution.
    composio_api_key: str | None = None
    composio_base_url: str = "https://backend.composio.dev/api/v3.1"
    composio_connected_account_id: str | None = None
    composio_toolkit_versions: str = "latest"
    composio_webhook_secret: str | None = None
    composio_webhook_tolerance_seconds: int = 300

    # Public URL for this gateway (e.g. an ngrok/cloudflared tunnel), used to
    # auto-register the Composio webhook target on startup.
    public_url: str | None = None

    # Console dashboard login (Sign in with Slack). If unset, the session
    # cookie-signing secret falls back to slack_client_secret, then to a
    # random per-process key (sessions won't survive a restart in that case).
    dashboard_session_secret: str | None = None
    dashboard_auth_disabled: bool = False
    # Optional member-facing dashboard login. Local administration uses the
    # one-time setup credential and never depends on a Slack redirect URL.
    slack_dashboard_login_enabled: bool = False
    # Self-hosted installations can authenticate an operator without Slack
    # OAuth using one strong, deployment-owned token. The token is exchanged
    # for the same signed session cookie as Slack login and is never stored in
    # browser local storage.
    local_admin_token: str | None = None

    # Dev-only: auto-resolve every write approval instead of pausing for a
    # human. The approval record is still created and immediately marked
    # approved (full audit/judgment trail intact) rather than skipped
    # outright. Off by default — real deployments keep human approval
    # mandatory for every write, this setting exists only for local testing.
    auto_approve_writes: bool = False

    # Durable local worker. PostgreSQL/Redis can replace these interfaces at
    # deployment time; SQLite remains the fully runnable local backend.
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 0.5
    worker_lease_seconds: int = 60
    worker_batch_size: int = 20
    worker_concurrency: int = 30
    # A separate, smaller pool for chunking/embedding/extraction/episode
    # jobs (memory ingestion/retrieval plan §9) so a burst of ingest
    # traffic can never crowd out the main lane's event routing, dispatch,
    # or delivery jobs.
    worker_knowledge_concurrency: int = 8
    attention_minimum_confidence: float = 0.72
    proactive_cooldown_seconds: int = 900
    episode_quiet_seconds: int = 300
    # How often the worker loop ticks scheduled automations.
    dispatcher_tick_interval_seconds: float = 30.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name


settings = Settings()
