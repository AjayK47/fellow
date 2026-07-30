"""Runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    env = os.environ.get("COMPANY_BRAIN_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMPANY_BRAIN_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = _default_data_dir()
    db_name: str = "brain.db"
    default_org_id: str = "org_acme"
    log_level: str = "info"
    # Embeddings are optional in local/test environments. When both values are
    # configured, the OpenRouter-compatible embeddings endpoint is used.
    openrouter_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COMPANY_BRAIN_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    )
    embedding_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COMPANY_BRAIN_EMBEDDING_MODEL", "OPENROUTER_EMBEDDING_MODEL"),
    )
    embedding_base_url: str = "https://openrouter.ai/api/v1"
    embedding_dimensions: int = 96
    embedding_provider: str = "deterministic"

    # Agent runtime. The deterministic provider keeps local development and
    # tests zero-cost; set COMPANY_BRAIN_MODEL_PROVIDER=openrouter to use the
    # configured OpenRouter chat models.
    model_provider: str = "deterministic"
    chat_model: str = "openai/gpt-4.1-mini"
    attention_model: str = "openai/gpt-4.1-mini"
    extraction_model: str | None = None
    model_base_url: str = "https://openrouter.ai/api/v1"
    model_timeout_seconds: float = 90.0
    agent_max_iterations: int = 12
    agent_max_tool_calls: int = 24
    agent_context_token_budget: int = 12_000
    agent_max_total_tokens: int = 100_000

    # Slack Events API / Web API.
    slack_signing_secret: str | None = None
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    slack_bot_user_id: str | None = None
    slack_require_mention: bool = True
    slack_thread_require_mention: bool = False
    slack_free_response_channels: str = ""
    slack_allowed_channels: str = ""
    slack_proactive_channels: str = ""
    slack_signature_tolerance_seconds: int = 300

    # Composio v3.1 direct tool execution.
    composio_api_key: str | None = None
    composio_base_url: str = "https://backend.composio.dev/api/v3.1"
    composio_connected_account_id: str | None = None
    composio_toolkit_versions: str = "latest"

    # Durable local worker. PostgreSQL/Redis can replace these interfaces at
    # deployment time; SQLite remains the fully runnable local backend.
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 0.5
    worker_lease_seconds: int = 60
    worker_batch_size: int = 20
    worker_concurrency: int = 4
    attention_minimum_confidence: float = 0.72
    proactive_cooldown_seconds: int = 900
    episode_quiet_seconds: int = 300

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name


settings = Settings()
