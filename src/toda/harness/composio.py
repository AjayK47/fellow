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

    def reconfigure(self, *, api_key: str, webhook_secret: str | None = None) -> None:
        """Apply a newly saved credential to the live client.

        Credentials are read from the env file once, when the container is
        built, so a key saved from the console would otherwise sit on disk
        doing nothing until the process restarted -- the integrations page kept
        reporting "not configured" immediately after a successful save.
        """
        self.api_key = api_key
        if webhook_secret:
            self.webhook_secret = webhook_secret

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
                "Composio is not configured; set TODA_COMPOSIO_API_KEY "
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
        self,
        *,
        toolkit: str,
        name: str | None = None,
        auth_scheme: str | None = None,
    ) -> dict[str, Any]:
        """Create an auth config for a toolkit.

        OAuth2 (the default, auth_scheme=None or "OAUTH2") uses Composio's
        own managed auth — no custom OAuth app needed. Every other scheme
        (API_KEY, BEARER_TOKEN, BASIC, ...) requires "use_custom_auth"
        instead; confirmed against Composio's v3 OpenAPI schema that
        "use_composio_managed_auth" only ever declares OAUTH2 as a valid
        mode. No credentials are needed at this step even for API_KEY —
        the toolkit's own auth_config_details (see describe_toolkit) show
        connected_account_initiation, not auth_config_creation, as where
        the actual secret is collected, per-connected-account.
        """
        if auth_scheme and auth_scheme != "OAUTH2":
            auth_config: dict[str, Any] = {
                "type": "use_custom_auth",
                "authScheme": auth_scheme,
                "name": name or toolkit,
            }
        else:
            auth_config = {
                "type": "use_composio_managed_auth",
                "name": name or toolkit,
            }
        return self._request(
            "POST",
            "auth_configs",
            body={"toolkit": {"slug": toolkit}, "auth_config": auth_config},
        )

    @staticmethod
    def _auth_config_id(payload: dict[str, Any]) -> str | None:
        """Pull the id out of a create-auth-config response.

        Composio has returned this at several nestings, and reading only the
        top-level `id`/`auth_config_id` meant a successful creation looked
        like a failure: the config was created on their side, we raised
        "Composio did not return an auth config id", and the user saw a
        broken connect dialog for an app that was now half set up. Confirmed
        against a real HubSpot attempt, which left an ENABLED config with
        zero connections behind.
        """
        for candidate in (
            payload.get("id"),
            payload.get("auth_config_id"),
            (payload.get("auth_config") or {}).get("id"),
            (payload.get("data") or {}).get("id"),
            ((payload.get("data") or {}).get("auth_config") or {}).get("id"),
        ):
            if candidate:
                return str(candidate)
        return None

    def ensure_auth_config(self, *, toolkit: str) -> str:
        """Find or create this toolkit's auth config and return its id.

        One implementation, because there were two -- the console's connect
        route and the agent's `request_app_connection` tool each had their
        own copy of find-then-create-then-read, which is how they were both
        wrong in the same way at once.
        """
        existing = self.list_auth_configs(toolkit=toolkit)
        for item in existing.get("items") or []:
            item_toolkit = item.get("toolkit")
            slug = (
                item_toolkit.get("slug")
                if isinstance(item_toolkit, dict)
                else item_toolkit
            )
            # Never accept another toolkit's config just because it came
            # back first -- that would hand the user an OAuth link for the
            # wrong application.
            if not slug or str(slug).lower() == toolkit.lower():
                if item.get("id"):
                    return str(item["id"])
        created = self.create_auth_config(toolkit=toolkit)
        auth_config_id = self._auth_config_id(created)
        if auth_config_id:
            return auth_config_id
        # Creation may have succeeded server-side even when the response is
        # unreadable, so look again before giving up -- and if it really did
        # fail, say what came back instead of only that something was absent.
        for item in (self.list_auth_configs(toolkit=toolkit).get("items") or []):
            if item.get("id"):
                return str(item["id"])
        raise RuntimeError(
            "Composio did not return an auth config id for "
            f"{toolkit!r}; response keys were {sorted(created)!r}"
        )

    def list_toolkits(
        self,
        *,
        search: str | None = None,
        category: str | None = None,
        cursor: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Browse Composio's full toolkit catalog (1000+ entries) rather
        than a hardcoded shortlist — cursor-paginated per Composio's own
        `/toolkits` contract (a `total_pages` field is present but not
        usable for page-number access; `next_cursor` is the real cursor)."""
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        if category:
            params["category"] = category
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "toolkits", params=params)

    def list_toolkit_categories(self) -> dict[str, Any]:
        return self._request("GET", "toolkits/categories", params={"limit": 100})

    def describe_toolkit(self, slug: str) -> dict[str, Any]:
        """Full toolkit detail, including auth_config_details — each
        supported auth scheme (OAUTH2, API_KEY, BEARER_TOKEN, ...) plus the
        exact fields (name, displayName, description, is_secret) needed to
        initiate a connected account under that scheme. This is what lets
        the console render a correct credentials form per toolkit instead
        of guessing a single generic "API key" field."""
        return self._request("GET", f"toolkits/{slug}")

    def create_connected_account(
        self,
        *,
        auth_config_id: str,
        user_id: str,
        auth_scheme: str,
        val: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a connected account directly from caller-supplied
        credentials (API key, bearer token, basic auth, ...) — the
        non-OAuth counterpart to create_connect_link. No redirect: a
        correctly-formed request activates the connection synchronously."""
        return self._request(
            "POST",
            "connected_accounts",
            body={
                "auth_config": {"id": auth_config_id},
                "connection": {"user_id": user_id, "state": {"authScheme": auth_scheme, "val": val}},
            },
            v3=True,
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

    def subscribe_all_triggers(
        self,
        *,
        toolkit: str,
        connected_account_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Subscribe a freshly connected account to every trigger its
        toolkit offers.

        Connecting an app and receiving events from it are separate objects
        in Composio, and nothing linked them: an organization could connect
        HubSpot, watch Toda answer questions about it, and never learn that
        it would not hear about a new contact unless someone POSTed to an
        undocumented route. Trigger lifecycle now follows connection
        lifecycle.

        Only *webhook* triggers, and only those needing no configuration.
        Both exclusions are deliberate and both are reported rather than
        silently dropped. Note that today this subscribes to nothing on
        GitHub, Gmail or HubSpot -- every no-config trigger they offer is a
        poll, and every webhook they offer needs configuration. That is a
        correct fail-closed outcome, not a bug: automatic subscription
        should never be able to start a firehose.
        """
        subscribed: list[str] = []
        needs_config: list[dict[str, str]] = []
        skipped_poll: list[str] = []
        for item in (self.list_trigger_types(toolkit=toolkit).get("items") or []):
            slug = item.get("slug")
            if not slug:
                continue
            # `type` and `config` are already on the list item, so asking
            # again per slug was 46 pointless HTTP round trips per GitHub
            # connect -- visible in the gateway log as a wall of
            # triggers_types/... GETs before a single subscription happened.
            detail = item
            # Webhook triggers only. A poll trigger's first run has seen
            # nothing, so it emits the account's entire history as "new" --
            # enabling one GitHub PR poll produced 6,525 events in under an
            # hour, every one of them billed. Webhooks only fire on things
            # that genuinely happen after subscription, which is the whole
            # point of subscribing automatically.
            if str(detail.get("type") or "").lower() != "webhook":
                skipped_poll.append(str(slug))
                continue
            # Anything needing configuration Toda cannot invent (a repo to
            # watch, a HubSpot developer app) is a human decision, not an
            # automatic one.
            if (detail.get("config") or {}).get("required"):
                needs_config.append({"slug": str(slug), "error": "requires configuration"})
                continue
            try:
                # `trigger_instances/{slug}/upsert` is idempotent, so a
                # re-sync of an already-connected account is a no-op rather
                # than a duplicate subscription.
                self.create_trigger(
                    slug=str(slug),
                    connected_account_id=connected_account_id,
                    user_id=user_id,
                )
                subscribed.append(str(slug))
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                needs_config.append({"slug": str(slug), "error": str(exc)[:200]})
        return {
            "toolkit": toolkit,
            "connected_account_id": connected_account_id,
            "subscribed": subscribed,
            "needs_config": needs_config,
            "skipped_poll": skipped_poll,
        }

    def unsubscribe_all_triggers(
        self, *, connected_account_id: str
    ) -> dict[str, Any]:
        """Remove every trigger instance belonging to one connected account.

        The counterpart to subscribe_all_triggers. Without it, disconnecting
        an app leaves its triggers live on Composio's side, still delivering
        events for a connection the organization believes it has removed --
        the same add-only shape that made Slack channel membership
        irrevocable.
        """
        removed: list[str] = []
        failed: list[dict[str, str]] = []
        try:
            active = self.list_active_triggers(
                connected_account_ids=[connected_account_id]
            ).get("items") or []
        except Exception as exc:  # noqa: BLE001
            return {"removed": [], "failed": [{"slug": "*", "error": str(exc)[:200]}]}
        for instance in active:
            trigger_id = instance.get("id")
            if not trigger_id:
                continue
            try:
                self.delete_trigger(str(trigger_id))
                removed.append(str(trigger_id))
            except Exception as exc:  # noqa: BLE001
                failed.append({"slug": str(trigger_id), "error": str(exc)[:200]})
        return {
            "connected_account_id": connected_account_id,
            "removed": removed,
            "failed": failed,
        }

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
