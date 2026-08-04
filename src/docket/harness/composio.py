"""Composio connections, triggers, signed webhooks, and tool execution."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx


class ComposioClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        connected_account_id: str | None,
        toolkit_versions: str = "latest",
        webhook_secret: str | None = None,
        webhook_tolerance_seconds: int = 300,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.connected_account_id = connected_account_id
        self.toolkit_versions = toolkit_versions
        self.webhook_secret = webhook_secret
        self.webhook_tolerance_seconds = webhook_tolerance_seconds

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def webhook_configured(self) -> bool:
        return bool(self.webhook_secret)

    @property
    def v3_base_url(self) -> str:
        if self.base_url.endswith("/v3.1"):
            return self.base_url[:-2]
        return self.base_url

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        body: dict[str, Any] | None = None,
        v3: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("Composio API key is not configured")
        response = httpx.request(
            method,
            f"{self.v3_base_url if v3 else self.base_url}/{path.lstrip('/')}",
            params=params,
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {"successful": True}
        return response.json()

    def execute(
        self,
        tool_slug: str,
        arguments: dict[str, Any],
        *,
        connected_account_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = connected_account_id or self.connected_account_id
        if not self.api_key or not account_id:
            raise RuntimeError(
                "Composio is not configured; set DOCKET_COMPOSIO_API_KEY "
                "and connect an organization account"
            )
        # Composio's v3 execute API rejects a connected_account_id without
        # the entity/user id it was linked under (error
        # ActionExecute_ConnectedAccountEntityIdRequired) — every connected
        # account in this codebase is created with composio_user_id set, so
        # callers should always have one to pass through.
        body: dict[str, Any] = {
            "connected_account_id": account_id,
            "arguments": arguments,
        }
        if user_id:
            body["entity_id"] = user_id
        return self._request(
            "POST",
            f"tools/execute/{tool_slug}",
            params={"toolkit_versions": self.toolkit_versions},
            body=body,
            timeout=60.0,
        )

    def describe(self, tool_slug: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"tools/{tool_slug}",
            params={"toolkit_versions": self.toolkit_versions},
        )

    def create_tool_router_session(
        self,
        *,
        user_id: str,
        connected_accounts: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Open a discovery-only Tool Router session: COMPOSIO_SEARCH_TOOLS
        and COMPOSIO_GET_TOOL_SCHEMAS only. Execute, workbench, and
        connection-management are explicitly disabled here — confirmed
        against the live API that execute defaults ON otherwise (it appears
        in tool_router_tools even when never requested). Actual write
        execution stays exclusively on this codebase's own approval-gated
        COMPOSIO_EXECUTE tool; this session can never reach it, by
        construction, not just by choosing not to call it.

        connected_accounts must be passed explicitly (toolkit slug -> list
        of connected_account_id) — confirmed against the live API that a
        session otherwise has no visibility into a user's existing
        connections at all, even when user_id matches exactly. Without
        this, COMPOSIO_SEARCH_TOOLS reports every toolkit as unconnected
        regardless of real connector_accounts state.
        """
        body: dict[str, Any] = {
            "user_id": user_id,
            "search": {"enable": True},
            "execute": {"enable_multi_execute": False},
            "workbench": {"enable": False},
            "manage_connections": {"enable": False},
        }
        if connected_accounts:
            body["connected_accounts"] = connected_accounts
        return self._request("POST", "tool_router/session", body=body)

    def execute_tool_router_action(
        self, *, session_id: str, tool_slug: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"tool_router/session/{session_id}/execute",
            body={"tool_slug": tool_slug, "arguments": arguments},
            timeout=60.0,
        )

    def search_tools(
        self,
        *,
        query: str,
        toolkit: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": query,
            "limit": min(max(limit, 1), 100),
            "toolkit_versions": self.toolkit_versions,
            "include_deprecated": False,
        }
        if toolkit:
            params["toolkit_slug"] = toolkit
        return self._request("GET", "tools", params=params)

    def list_connected_accounts(
        self,
        *,
        user_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = [("limit", limit)]
        if user_id:
            params.append(("user_ids", user_id))
        for status in statuses or ["ACTIVE"]:
            params.append(("statuses", status))
        return self._request("GET", "connected_accounts", params=params)

    def create_connect_link(
        self,
        *,
        user_id: str,
        auth_config_id: str,
        callback_url: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "user_id": user_id,
            "auth_config_id": auth_config_id,
        }
        if callback_url:
            body["callback_url"] = callback_url
        if alias:
            body["alias"] = alias
        return self._request(
            "POST", "connected_accounts/link", body=body, v3=True
        )

    def list_auth_configs(
        self, *, toolkit: str | None = None
    ) -> dict[str, Any]:
        params = {"toolkit_slug": toolkit} if toolkit else None
        return self._request("GET", "auth_configs", params=params)

    def create_auth_config(
        self, *, toolkit: str, name: str | None = None
    ) -> dict[str, Any]:
        """Create a Composio-managed auth config for a toolkit (no custom OAuth app needed)."""
        return self._request(
            "POST",
            "auth_configs",
            body={
                "toolkit": {"slug": toolkit},
                "auth_config": {
                    "type": "use_composio_managed_auth",
                    "name": name or toolkit,
                },
            },
        )

    def list_webhook_subscriptions(self) -> dict[str, Any]:
        """Composio allows exactly one webhook subscription per project."""
        return self._request("GET", "webhook_subscriptions", v3=True)

    def list_webhook_event_types(self) -> dict[str, Any]:
        return self._request(
            "GET", "webhook_subscriptions/event_types", v3=True
        )

    def create_webhook_subscription(
        self, *, webhook_url: str, enabled_events: list[str], version: str = "V3"
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "webhook_subscriptions",
            body={
                "webhook_url": webhook_url,
                "enabled_events": enabled_events,
                "version": version,
            },
            v3=True,
        )

    def update_webhook_subscription(
        self, subscription_id: str, *, webhook_url: str
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"webhook_subscriptions/{subscription_id}",
            body={"webhook_url": webhook_url},
            v3=True,
        )

    def delete_connected_account(self, connected_account_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"connected_accounts/{connected_account_id}", v3=True
        )

    def list_trigger_types(
        self, *, toolkit: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = [("limit", limit)]
        if toolkit:
            params.append(("toolkit_slugs", toolkit))
        return self._request("GET", "triggers_types", params=params)

    def describe_trigger(self, slug: str) -> dict[str, Any]:
        return self._request("GET", f"triggers_types/{slug}")

    def create_trigger(
        self,
        *,
        slug: str,
        connected_account_id: str,
        user_id: str | None = None,
        trigger_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "connected_account_id": connected_account_id,
            "trigger_config": trigger_config or {},
        }
        if user_id:
            body["user_id"] = user_id
        return self._request(
            "POST", f"trigger_instances/{slug}/upsert", body=body
        )

    def list_active_triggers(
        self, *, connected_account_ids: list[str] | None = None
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = []
        for account_id in connected_account_ids or []:
            params.append(("connected_account_ids", account_id))
        return self._request("GET", "trigger_instances/active", params=params)

    def set_trigger_enabled(
        self, trigger_id: str, *, enabled: bool
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"trigger_instances/manage/{trigger_id}",
            body={"enabled": enabled},
        )

    def delete_trigger(self, trigger_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"trigger_instances/manage/{trigger_id}"
        )

    def verify_webhook(
        self,
        *,
        raw_body: bytes,
        webhook_id: str,
        webhook_timestamp: str,
        webhook_signature: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Verify Composio's V3 HMAC envelope and return its JSON payload."""
        if not self.webhook_secret:
            raise RuntimeError("Composio webhook secret is not configured")
        if not webhook_id or not webhook_timestamp or not webhook_signature:
            raise ValueError("Composio webhook signature headers are missing")
        try:
            timestamp = int(webhook_timestamp)
        except (TypeError, ValueError) as exc:
            raise ValueError("Composio webhook timestamp is invalid") from exc
        current = int(time.time()) if now is None else now
        if abs(current - timestamp) > self.webhook_tolerance_seconds:
            raise ValueError("Composio webhook timestamp is outside tolerance")
        signing = (
            webhook_id.encode()
            + b"."
            + webhook_timestamp.encode()
            + b"."
            + raw_body
        )
        expected = base64.b64encode(
            hmac.new(
                self.webhook_secret.encode(), signing, hashlib.sha256
            ).digest()
        ).decode()
        received = (
            webhook_signature.split(",", 1)[1]
            if "," in webhook_signature
            else webhook_signature
        )
        if not hmac.compare_digest(expected, received):
            raise ValueError("Composio webhook signature is invalid")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise ValueError("Composio webhook body is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Composio webhook body must be an object")
        return payload
