"""Runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    env = os.environ.get("DOCKET_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCKET_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = _default_data_dir()
    workspace_root: Path = Field(default_factory=lambda: _default_data_dir() / "workspaces")
    db_name: str = "brain.db"
    default_org_id: str = "org_acme"
    log_level: str = "info"
    # Embeddings are optional in local/test environments, and configured
    # independently of the chat model provider/key below — they are commonly
    # different providers (e.g. a dedicated embeddings host vs. OpenRouter).
    embedding_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DOCKET_EMBEDDING_API_KEY", "EMBEDDING_API_KEY"),
    )
    embedding_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DOCKET_EMBEDDING_MODEL", "OPENROUTER_EMBEDDING_MODEL"),
    )
    embedding_base_url: str = "https://openrouter.ai/api/v1"
    embedding_dimensions: int = 96
    embedding_provider: str = "deterministic"

    # Agent runtime. The deterministic provider keeps local development and
    # tests zero-cost; set DOCKET_MODEL_PROVIDER=openrouter to use the
    # configured chat models.
    model_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DOCKET_MODEL_API_KEY", "OPENROUTER_API_KEY"),
    )
    model_provider: str = "deterministic"
    chat_model: str = "openai/gpt-5.6-luna"
    attention_model: str = "openai/gpt-5.6-luna"
    extraction_model: str | None = None
    # H-14: dedicated cheap model for compressing oversized tool output.
    # Falls back to chat_model (a separate call, not a shared context) when unset.
    tool_output_summary_model: str | None = None
    model_base_url: str = "https://openrouter.ai/api/v1"
    model_timeout_seconds: float = 90.0
    # General web research. Search is opt-in because Brave uses a metered API;
    # fetching public pages needs no provider key and remains read-only.
    brave_search_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DOCKET_BRAVE_SEARCH_API_KEY", "BRAVE_SEARCH_API_KEY"),
    )
    firecrawl_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DOCKET_FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY"),
    )
    firecrawl_base_url: str = "https://api.firecrawl.dev/v2"
    web_fetch_enabled: bool = True
    web_fetch_max_chars: int = 18_000
    # H-07 (rescoped): sandboxed run_code tool. Every invocation still
    # requires human approval (risk="write", approval_required=True) before
    # it runs, and callbacks are restricted to a read-only allow-list
    # re-verified on every call — on by default under those guarantees.
    run_code_enabled: bool = True
    run_code_timeout_seconds: int = 60
    run_code_max_tool_calls: int = 20
    agent_max_iterations: int = 100
    agent_max_tool_calls: int = 150
    agent_context_token_budget: int = 200_000
    agent_max_total_tokens: int = 2_000_000
    specialist_max_parallel: int = 3
    specialist_max_depth: int = 1
    browser_enabled: bool = False
    browser_allowed_domains: str = ""
    vision_enabled: bool = False
    image_generation_enabled: bool = False

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
    slack_oauth_scopes: str = (
        "app_mentions:read,channels:history,channels:read,chat:write,"
        "chat:write.public,groups:history,groups:read,im:history,im:read,"
        "im:write,mpim:history,mpim:read,reactions:write,users:read,"
        "files:write"
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
    attention_minimum_confidence: float = 0.72
    proactive_cooldown_seconds: int = 900
    episode_quiet_seconds: int = 300

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name


settings = Settings()
