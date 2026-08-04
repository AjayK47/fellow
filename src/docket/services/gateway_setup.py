"""Local-only gateway onboarding: configuration, Slack validation, and tenant bootstrap."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


SLACK_MANIFEST = """_metadata:
  major_version: 1
display_information:
  name: Docket
  description: Docket workspace assistant, built by Ajay
  features:
    bot_user:
      display_name: Docket
      always_online: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - groups:history
      - im:history
      - mpim:history
      - channels:read
      - groups:read
      - users:read
      - chat:write
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
      - message.groups
      - message.im
      - message.mpim
  socket_mode_enabled: true
  org_deploy_enabled: false
  token_rotation_enabled: false
  app_home:
    home_tab_enabled: true
    messages_tab_enabled: true
    messages_tab_read_only: false
"""


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "admin"


class GatewaySetupService:
    """Writes only the gateway-local env file; never returns stored secrets."""

    def __init__(
        self, store: Any, graph: Any, audit: Any, skills: Any | None = None
    ) -> None:
        self.store = store
        self.graph = graph
        self.audit = audit
        self.skills = skills

    @staticmethod
    def env_path() -> Path:
        configured = os.environ.get("DOCKET_ENV_FILE")
        return Path(configured) if configured else Path.cwd() / ".env"

    def status(self) -> dict[str, Any]:
        env = self._read_env()
        organizations = self.store.list_organizations()
        installations = [
            item
            for organization in organizations
            for item in self.store.list_harness_records(
                "slack_installations", organization["id"]
            )
        ]
        return {
            "environment_file": str(self.env_path()),
            "model": {
                "configured": bool(env.get("DOCKET_MODEL_API_KEY")),
                "model_provider": env.get("DOCKET_MODEL_PROVIDER", "deterministic"),
                "chat_model": env.get("DOCKET_CHAT_MODEL", ""),
            },
            "embedding": {
                "configured": bool(env.get("DOCKET_EMBEDDING_API_KEY")),
                "embedding_model": env.get("DOCKET_EMBEDDING_MODEL", ""),
            },
            "slack": {
                "bot_token_configured": bool(env.get("DOCKET_SLACK_BOT_TOKEN")),
                "app_token_configured": bool(env.get("DOCKET_SLACK_APP_TOKEN")),
                "signing_secret_configured": bool(env.get("DOCKET_SLACK_SIGNING_SECRET")),
                "installations": installations,
            },
            "tools": {
                "brave_search_configured": bool(env.get("BRAVE_SEARCH_API_KEY")),
                "firecrawl_configured": bool(env.get("FIRECRAWL_API_KEY")),
                "web_fetch_enabled": env.get("DOCKET_WEB_FETCH_ENABLED", "true").lower()
                not in {"0", "false", "no"},
            },
            "organizations": organizations,
        }

    def configure_openrouter(
        self,
        *,
        api_key: str,
        chat_model: str = "openai/gpt-5.6-luna",
        attention_model: str | None = None,
        extraction_model: str | None = None,
        embedding_api_key: str | None = None,
        embedding_model: str = "qwen/qwen3-embedding-8b",
    ) -> dict[str, Any]:
        if not api_key.strip():
            raise ValueError("Model API key is required")
        self._update_env(
            {
                "DOCKET_MODEL_API_KEY": api_key.strip(),
                "DOCKET_MODEL_PROVIDER": "openrouter",
                "DOCKET_CHAT_MODEL": chat_model.strip(),
                "DOCKET_ATTENTION_MODEL": (attention_model or chat_model).strip(),
                "DOCKET_EXTRACTION_MODEL": (extraction_model or chat_model).strip(),
                # Independent from the chat-model key above so the two can
                # point at different providers; defaults to the same key for
                # a one-field quick setup when they happen to share one.
                "DOCKET_EMBEDDING_API_KEY": (embedding_api_key or api_key).strip(),
                "DOCKET_EMBEDDING_PROVIDER": "openrouter",
                "DOCKET_EMBEDDING_MODEL": embedding_model.strip(),
            }
        )
        return {"configured": True, "restart_required": True}

    def configure_tools(
        self, *, brave_search_api_key: str | None = None, firecrawl_api_key: str | None = None,
        web_fetch_enabled: bool = True
    ) -> dict[str, Any]:
        values = {"DOCKET_WEB_FETCH_ENABLED": str(web_fetch_enabled).lower()}
        if brave_search_api_key and brave_search_api_key.strip():
            values["BRAVE_SEARCH_API_KEY"] = brave_search_api_key.strip()
        if firecrawl_api_key and firecrawl_api_key.strip():
            values["FIRECRAWL_API_KEY"] = firecrawl_api_key.strip()
        self._update_env(values)
        return {
            "brave_search_configured": bool(values.get("BRAVE_SEARCH_API_KEY") or self._read_env().get("BRAVE_SEARCH_API_KEY")),
            "firecrawl_configured": bool(values.get("FIRECRAWL_API_KEY") or self._read_env().get("FIRECRAWL_API_KEY")),
            "web_fetch_enabled": web_fetch_enabled,
            "restart_required": True,
        }

    def connect_slack(
        self,
        *,
        organization_id: str,
        organization_name: str,
        bot_token: str,
        app_token: str,
        signing_secret: str,
        admin_slack_user_id: str | None = None,
        admin_name: str | None = None,
        channel_id: str | None = None,
        proactive: bool = True,
    ) -> dict[str, Any]:
        required = {
            "Slack bot token": bot_token,
            "Slack app token": app_token,
            "Slack signing secret": signing_secret,
            "Organization ID": organization_id,
            "Organization name": organization_name,
        }
        for label, value in required.items():
            if not str(value or "").strip():
                raise ValueError(f"{label} is required")
        response = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token.strip()}"},
            timeout=20.0,
        )
        response.raise_for_status()
        slack = response.json()
        if not slack.get("ok"):
            raise ValueError(f"Slack validation failed: {slack.get('error', 'unknown error')}")
        team_id = str(slack["team_id"])
        bot_user_id = str(slack["user_id"])
        self._update_env(
            {
                "DOCKET_SLACK_SIGNING_SECRET": signing_secret.strip(),
                "DOCKET_SLACK_BOT_TOKEN": bot_token.strip(),
                "DOCKET_SLACK_APP_TOKEN": app_token.strip(),
                "DOCKET_SLACK_BOT_USER_ID": bot_user_id,
            }
        )
        public_scope_id = self._bootstrap_tenant(organization_id, organization_name)
        now = datetime.now(timezone.utc).isoformat()
        installation = {
            "id": f"slack:{organization_id}:{team_id}",
            "organization_id": organization_id,
            "workspace_id": team_id,
            "workspace_name": slack.get("team") or team_id,
            "bot_user_id": bot_user_id,
            "bot_token": bot_token.strip(),
            "created_at": now,
            "updated_at": now,
        }
        self.store.upsert_slack_installation(installation)
        if self.skills:
            self.skills.ensure_platform_skills(organization_id)
        admin = None
        if admin_slack_user_id:
            admin_id = f"person_{_slug(admin_slack_user_id)}"
            self.graph.upsert_entity(
                {
                    "id": admin_id,
                    "organization_id": organization_id,
                    "entity_type": "person",
                    "name": admin_name or admin_slack_user_id,
                    "aliases": [admin_slack_user_id],
                    "attributes": {"slack_user_id": admin_slack_user_id},
                }
            )
            self.store.add_membership(organization_id, admin_id, public_scope_id)
            admin = {
                "id": f"identity:{organization_id}:slack:{admin_slack_user_id}",
                "organization_id": organization_id,
                "source": "slack",
                "source_user_id": admin_slack_user_id,
                "principal_id": admin_id,
            }
            self.store.upsert_simple_harness_record("identity_mappings", admin)
        policy = None
        if channel_id:
            scope_id = f"scope_slack_{team_id}_{channel_id}"
            self.store.upsert_scope(
                {
                    "id": scope_id,
                    "organization_id": organization_id,
                    "kind": "organization_public",
                    "label": f"Slack {channel_id}",
                    "channel_id": channel_id,
                    "principal_ids": [],
                    "is_public": True,
                }
            )
            if admin:
                self.store.add_membership(organization_id, admin["principal_id"], scope_id)
            policy = {
                "id": f"channel_policy:{organization_id}:slack:{channel_id}",
                "organization_id": organization_id,
                "source": "slack",
                "channel_id": channel_id,
                "enabled": True,
                "observe": True,
                "proactive": proactive,
                "free_response": False,
                "is_private": False,
                "scope_id": scope_id,
            }
            self.store.upsert_simple_harness_record("channel_policies", policy)
        self.audit.record(
            organization_id,
            "gateway.slack.connected",
            {"workspace_id": team_id, "bot_user_id": bot_user_id},
        )
        return {
            "workspace": {"id": team_id, "name": slack.get("team")},
            "bot": {"user_id": bot_user_id, "name": slack.get("user")},
            "installation": {k: v for k, v in installation.items() if k != "bot_token"},
            "admin_mapping": admin,
            "channel_policy": policy,
            "restart_required": True,
        }

    def install_via_oauth(
        self,
        *,
        workspace_id: str,
        workspace_name: str,
        bot_token: str,
        bot_user_id: str,
    ) -> dict[str, Any]:
        """Bootstrap a tenant from a public OAuth install — a stranger
        clicking "Add to Slack", not this operator's own .env-configured
        workspace.

        Unlike connect_slack(), this never touches the local .env file: a
        publicly distributed app installs into many different people's
        workspaces, each with its own bot token, and those have to live in
        the database (one row per workspace) rather than a single shared
        local config file that only ever describes one workspace.

        organization_id is derived deterministically from workspace_id, so
        reinstalling (revoke and re-authorize) updates the same tenant and
        installation row rather than creating a duplicate.
        """
        for label, value in {
            "Slack workspace id": workspace_id,
            "Slack bot token": bot_token,
            "Slack bot user id": bot_user_id,
        }.items():
            if not str(value or "").strip():
                raise ValueError(f"{label} is required")
        organization_id = f"org_slack_{_slug(workspace_id)}"
        public_scope_id = self._bootstrap_tenant(
            organization_id, workspace_name or workspace_id
        )
        now = datetime.now(timezone.utc).isoformat()
        installation = {
            "id": f"slack:{organization_id}:{workspace_id}",
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name or workspace_id,
            "bot_user_id": bot_user_id,
            "bot_token": bot_token,
            "created_at": now,
            "updated_at": now,
        }
        self.store.upsert_slack_installation(installation)
        if self.skills:
            self.skills.ensure_platform_skills(organization_id)
        self.audit.record(
            organization_id,
            "gateway.slack.installed_via_oauth",
            {"workspace_id": workspace_id, "bot_user_id": bot_user_id},
        )
        return {
            "organization_id": organization_id,
            "workspace": {"id": workspace_id, "name": workspace_name},
            "scope_id": public_scope_id,
        }

    def _bootstrap_tenant(self, organization_id: str, organization_name: str) -> str:
        if not self.store.get_organization(organization_id):
            self.store.put_organization({"id": organization_id, "name": organization_name})
        existing = next(
            (
                scope
                for scope in self.store.list_scopes(organization_id)
                if scope.get("is_public")
                and scope.get("kind") == "organization_public"
            ),
            None,
        )
        if existing:
            return str(existing["id"])
        # Every other default in the codebase (seed fixtures, connector
        # ingestion fallbacks, skill packages, scope resolution) assumes the
        # literal id "scope_org_public" for the organization's public scope.
        # An org-id-prefixed id here would silently mismatch every one of
        # them — including the Composio connect flow, which resolves this
        # scope by that exact literal default rather than looking it up.
        scope_id = "scope_org_public"
        if not self.store.get_scope(scope_id):
            self.store.upsert_scope(
                {
                    "id": scope_id,
                    "organization_id": organization_id,
                    "kind": "organization_public",
                    "label": "Organization Public",
                    "principal_ids": [],
                    "is_public": True,
                }
            )
        return scope_id

    def _read_env(self) -> dict[str, str]:
        path = self.env_path()
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text().splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def _update_env(self, values: dict[str, str]) -> None:
        path = self.env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text().splitlines() if path.exists() else []
        remaining = dict(values)
        output: list[str] = []
        for line in existing:
            if "=" not in line or line.lstrip().startswith("#"):
                output.append(line)
                continue
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
            else:
                output.append(line)
        if remaining:
            if output and output[-1]:
                output.append("")
            output.extend(f"{key}={value}" for key, value in remaining.items())
        path.write_text("\n".join(output).rstrip() + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
