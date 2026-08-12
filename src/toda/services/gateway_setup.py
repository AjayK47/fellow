"""Local-only gateway onboarding: configuration, Slack validation, and tenant bootstrap."""

from __future__ import annotations

import json
import re
import hashlib
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from toda.config import settings_env_file
from toda.providers import ProviderConnection, providers


SLACK_MANIFEST = """_metadata:
  major_version: 1
display_information:
  name: Toda
  description: Toda workspace assistant, built by Ajay
  features:
    bot_user:
      display_name: Toda
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
      - reactions:write
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
        return settings_env_file()

    def status(self) -> dict[str, Any]:
        env = self._read_env()
        connections = self._json_env(env, "TODA_PROVIDER_CONNECTIONS")
        assignments = self._json_env(env, "TODA_MODEL_ASSIGNMENTS")
        tool_connections = self._json_env(env, "TODA_TOOL_CONNECTIONS")
        organizations = self.store.list_organizations()
        installations = [
            item
            for organization in organizations
            for item in self.store.list_harness_records(
                "slack_installations", organization["id"]
            )
        ]
        provider_ready = bool(connections) and bool(assignments.get("main"))
        slack_ready = bool(installations)
        organization_ready = bool(organizations)
        steps = [
            {"id": "system", "label": "System check", "status": "complete"},
            {"id": "provider", "label": "Model provider", "status": "complete" if provider_ready else "current"},
            {"id": "models", "label": "Model roles", "status": "complete" if provider_ready else "pending"},
            {"id": "tools", "label": "Web and media", "status": "complete" if tool_connections else "optional"},
            {"id": "slack", "label": "Slack workspace", "status": "complete" if slack_ready else ("current" if provider_ready else "pending")},
            {"id": "organization", "label": "Organization", "status": "complete" if organization_ready else "pending"},
            {"id": "verify", "label": "Verification", "status": "current" if provider_ready and slack_ready else "pending"},
        ]
        return {
            "environment_file": str(self.env_path()),
            "available_providers": [
                {
                    "id": profile.name,
                    "name": profile.display_name,
                    "api_mode": profile.api_mode,
                    "api_key_required": profile.api_key_required,
                }
                for profile in providers.list()
            ],
            "providers": [
                {
                    "id": connection_id,
                    "provider": item.get("provider"),
                    "base_url": item.get("base_url"),
                    "credential_configured": bool(item.get("api_key")),
                }
                for connection_id, item in connections.items()
            ],
            "assignments": assignments,
            "slack": {
                "bot_token_configured": bool(env.get("TODA_SLACK_BOT_TOKEN")),
                "app_token_configured": bool(env.get("TODA_SLACK_APP_TOKEN")),
                "signing_secret_configured": bool(env.get("TODA_SLACK_SIGNING_SECRET")),
                "installations": installations,
            },
            "tools": {
                "connections": [
                    {
                        "id": connection_id,
                        "provider": item.get("provider"),
                        "base_url": item.get("base_url"),
                        "credential_configured": bool(item.get("api_key")),
                    }
                    for connection_id, item in tool_connections.items()
                ],
                "search_connection": env.get("TODA_WEB_SEARCH_CONNECTION") or None,
                "extract_connection": env.get("TODA_WEB_EXTRACT_CONNECTION") or None,
                "web_fetch_enabled": env.get("TODA_WEB_FETCH_ENABLED", "true").lower()
                not in {"0", "false", "no"},
            },
            "organizations": organizations,
            "setup": {
                "complete": provider_ready and slack_ready and organization_ready,
                "current_step": next(
                    (step["id"] for step in steps if step["status"] == "current"),
                    "verify",
                ),
                "steps": steps,
            },
        }

    def verify_installation(self) -> dict[str, Any]:
        status = self.status()
        checks = [
            {
                "id": "database",
                "label": "Database is writable",
                "ok": True,
                "detail": "SQLite WAL coordination is ready",
            },
            {
                "id": "workspace",
                "label": "Task workspace is writable",
                "ok": self._directory_writable(self.env_path().parent),
                "detail": str(self.env_path().parent),
            },
            {
                "id": "provider",
                "label": "Main model is assigned",
                "ok": bool(status["providers"]) and bool(status["assignments"].get("main")),
                "detail": str((status["assignments"].get("main") or {}).get("model") or "Not assigned"),
            },
            {
                "id": "embedding",
                "label": "Embedding path is available",
                "ok": bool(status["assignments"].get("embedding")) or bool(status["assignments"].get("main")),
                "detail": str((status["assignments"].get("embedding") or {}).get("model") or "Uses local deterministic fallback"),
            },
            {
                "id": "slack",
                "label": "Slack workspace is connected",
                "ok": bool(status["slack"]["installations"]),
                "detail": str((status["slack"]["installations"] or [{}])[0].get("workspace_name") or "Not connected"),
            },
            {
                "id": "workers",
                "label": "Internal worker lanes are available",
                "ok": True,
                "detail": "Infrastructure, agent, and knowledge lanes share this daemon",
            },
        ]
        return {
            "ready": all(item["ok"] for item in checks),
            "checks": checks,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _directory_writable(path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".toda-write-{secrets.token_hex(4)}"
            probe.touch(exist_ok=False)
            probe.unlink()
            return True
        except OSError:
            return False


    def configure_provider(
        self,
        *,
        connection_id: str,
        provider: str,
        api_key: str,
        base_url: str | None = None,
        assignments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        profile = providers.get(provider)
        connection_id = _slug(connection_id)
        env = self._read_env()
        connections = self._json_env(env, "TODA_PROVIDER_CONNECTIONS")
        previous = connections.get(connection_id) or {}
        credential = api_key.strip() or previous.get("api_key")
        if profile.api_key_required and not credential:
            raise ValueError(f"{profile.display_name} API key is required")
        connections[connection_id] = {
            "provider": provider,
            "api_key": credential,
            "base_url": (base_url or "").strip() or None,
        }
        model_assignments = self._json_env(env, "TODA_MODEL_ASSIGNMENTS")
        for role, model in (assignments or {}).items():
            if model and model.strip():
                model_assignments[role] = {
                    "connection": connection_id,
                    "model": model.strip(),
                }
        self._update_env(
            {
                "TODA_PROVIDER_CONNECTIONS": json.dumps(connections, separators=(",", ":")),
                "TODA_MODEL_ASSIGNMENTS": json.dumps(model_assignments, separators=(",", ":")),
            }
        )
        return {
            "connection_id": connection_id,
            "provider": provider,
            "assigned_roles": sorted((assignments or {}).keys()),
            "restart_required": True,
        }

    def model_catalog(
        self,
        *,
        provider: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        profile = providers.get(provider)
        connection = ProviderConnection(provider=provider, api_key=api_key, base_url=base_url)
        return {
            "provider": provider,
            "models": providers.models(connection),
            "api_mode": profile.api_mode,
        }

    def configure_tools(
        self, *, connection_id: str, provider: str, api_key: str | None = None,
        base_url: str | None = None, use_for_search: bool = False,
        use_for_extraction: bool = False, web_fetch_enabled: bool = True
    ) -> dict[str, Any]:
        if provider not in {"brave", "tavily", "exa", "firecrawl", "searxng"}:
            raise ValueError(f"Unknown web provider: {provider}")
        if provider != "searxng" and not (api_key or "").strip():
            raise ValueError(f"{provider} requires an API key")
        if provider == "searxng" and not (base_url or "").strip():
            raise ValueError("SearXNG requires an instance URL")
        connection_id = _slug(connection_id)
        env = self._read_env()
        connections = self._json_env(env, "TODA_TOOL_CONNECTIONS")
        previous = connections.get(connection_id) or {}
        connections[connection_id] = {
            "provider": provider,
            "api_key": (api_key or "").strip() or previous.get("api_key"),
            "base_url": (base_url or "").strip() or None,
        }
        values = {
            "TODA_TOOL_CONNECTIONS": json.dumps(connections, separators=(",", ":")),
            "TODA_WEB_FETCH_ENABLED": str(web_fetch_enabled).lower(),
        }
        if use_for_search:
            values["TODA_WEB_SEARCH_CONNECTION"] = connection_id
        if use_for_extraction:
            if provider != "firecrawl":
                raise ValueError("Only Firecrawl currently supports web extraction")
            values["TODA_WEB_EXTRACT_CONNECTION"] = connection_id
        self._update_env(values)
        return {
            "connection_id": connection_id,
            "provider": provider,
            "web_fetch_enabled": web_fetch_enabled,
            "restart_required": True,
        }

    @staticmethod
    def _json_env(env: dict[str, str], key: str) -> dict[str, Any]:
        try:
            value = json.loads(env.get(key) or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

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
                "TODA_SLACK_SIGNING_SECRET": signing_secret.strip(),
                "TODA_SLACK_BOT_TOKEN": bot_token.strip(),
                "TODA_SLACK_APP_TOKEN": app_token.strip(),
                "TODA_SLACK_BOT_USER_ID": bot_user_id,
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


class SetupBootstrapToken:
    """One-time, local bootstrap credential stored only as a digest."""

    def __init__(self, data_dir: Path, *, ttl_seconds: int = 1800) -> None:
        self.path = data_dir / "setup-bootstrap.json"
        self.ttl_seconds = ttl_seconds

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "digest": hashlib.sha256(token.encode()).hexdigest(),
                    "expires_at": int(time.time()) + self.ttl_seconds,
                }
            )
        )
        self.path.chmod(0o600)
        return token

    def consume(self, token: str) -> bool:
        try:
            record = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        expected = str(record.get("digest") or "")
        actual = hashlib.sha256(token.encode()).hexdigest()
        if int(record.get("expires_at") or 0) < int(time.time()):
            return False
        if not secrets.compare_digest(expected, actual):
            return False
        self.path.unlink(missing_ok=True)
        return True
