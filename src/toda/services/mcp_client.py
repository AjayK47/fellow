"""Tenant-scoped MCP client servers exposed through Toda's tool runtime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import re
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp import Client, StdioServerParameters
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from toda.domain.models import new_id, utcnow
from toda.harness.models import ToolDefinition


_TRANSPORTS = {"streamable_http", "sse", "stdio"}
_AUTH_TYPES = {"none", "api_key", "oauth"}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower() or "tool"


class _PersistentTokenStorage:
    def __init__(self, service: "MCPClientService", server_id: str) -> None:
        self.service = service
        self.server_id = server_id

    def _auth(self) -> dict[str, Any]:
        server = self.service._get(self.server_id)
        return dict(server.get("auth") or {})

    async def get_tokens(self) -> OAuthToken | None:
        value = self._auth().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        server = self.service._get(self.server_id)
        auth = dict(server.get("auth") or {})
        auth["tokens"] = tokens.model_dump(mode="json")
        server["auth"] = auth
        server["updated_at"] = utcnow().isoformat()
        self.service._upsert(server)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = self._auth().get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        server = self.service._get(self.server_id)
        auth = dict(server.get("auth") or {})
        auth["client_info"] = client_info.model_dump(mode="json")
        server["auth"] = auth
        server["updated_at"] = utcnow().isoformat()
        self.service._upsert(server)


class MCPClientService:
    def __init__(self, *, store: Any, registry: Any, audit: Any) -> None:
        self.store = store
        self.registry = registry
        self.audit = audit
        self._oauth_flows: dict[str, dict[str, Any]] = {}
        self._registered_by_server: dict[str, set[str]] = {}
        self.load_registered_tools()

    def create(
        self,
        *,
        organization_id: str,
        name: str,
        transport: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        auth_type: str = "none",
        api_key_header: str = "Authorization",
        api_key: str | None = None,
        oauth_scope: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("server name is required")
        if transport not in _TRANSPORTS:
            raise ValueError("transport must be streamable_http, sse, or stdio")
        if auth_type not in _AUTH_TYPES:
            raise ValueError("auth_type must be none, api_key, or oauth")
        if transport == "stdio":
            if not command:
                raise ValueError("command is required for stdio servers")
            if auth_type != "none":
                raise ValueError("stdio credentials belong in environment variables")
        elif not url:
            raise ValueError("URL is required for HTTP and SSE servers")
        if auth_type == "api_key" and (not api_key_header.strip() or not api_key):
            raise ValueError("API key header and value are required")

        now = utcnow().isoformat()
        server = {
            "id": new_id("mcp"),
            "organization_id": organization_id,
            "name": name,
            "transport": transport,
            "url": url.strip() if url else None,
            "command": command.strip() if command else None,
            "args": list(args or []),
            "env": dict(env or {}),
            "auth": {
                "type": auth_type,
                "header_name": api_key_header.strip(),
                "secret": api_key,
                "scope": oauth_scope,
            },
            "enabled": bool(enabled),
            "status": "setup_needed" if auth_type == "oauth" else "untested",
            "tools": [],
            "last_error": None,
            "last_tested_at": None,
            "created_at": now,
            "updated_at": now,
        }
        try:
            self._upsert(server)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ValueError("an MCP server with this name already exists") from exc
            raise
        self.audit.record(
            organization_id,
            "mcp.server_created",
            {"server_id": server["id"], "name": name, "transport": transport},
        )
        return self.public(server)

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return [
            self.public(item)
            for item in self.store.list_harness_records(
                "mcp_servers", organization_id, order_by="updated_at", descending=True
            )
        ]

    def public(self, server: dict[str, Any]) -> dict[str, Any]:
        auth = dict(server.get("auth") or {})
        public = dict(server)
        public["auth"] = {
            "type": auth.get("type", "none"),
            "header_name": auth.get("header_name"),
            "scope": auth.get("scope"),
            "configured": bool(
                auth.get("secret")
                or (auth.get("tokens") or {}).get("access_token")
            ),
        }
        public["env"] = sorted((server.get("env") or {}).keys())
        return public

    def set_enabled(self, server_id: str, organization_id: str, enabled: bool) -> dict[str, Any]:
        server = self._owned(server_id, organization_id)
        server["enabled"] = bool(enabled)
        server["updated_at"] = utcnow().isoformat()
        self._upsert(server)
        if enabled:
            self._register_server_tools(server)
        else:
            self._unregister_server_tools(server_id)
        return self.public(server)

    def delete(self, server_id: str, organization_id: str) -> dict[str, Any]:
        server = self._owned(server_id, organization_id)
        self._unregister_server_tools(server_id)
        with self.store.cursor() as cur:
            cur.execute(
                "DELETE FROM mcp_servers WHERE id = ? AND organization_id = ?",
                (server_id, organization_id),
            )
        self.audit.record(
            organization_id,
            "mcp.server_deleted",
            {"server_id": server_id, "name": server["name"]},
        )
        return {"id": server_id, "deleted": True}

    def test(self, server_id: str, organization_id: str) -> dict[str, Any]:
        server = self._owned(server_id, organization_id)
        if (server.get("auth") or {}).get("type") == "oauth" and not (
            (server.get("auth") or {}).get("tokens") or {}
        ).get("access_token"):
            raise PermissionError("OAuth connection is required before testing")
        try:
            tools = self._run_async(self._discover(server))
            return self.public(self._store_discovery(server, tools))
        except Exception as exc:
            self._store_failure(server, exc)
            raise

    async def start_oauth(
        self, server_id: str, organization_id: str, redirect_uri: str
    ) -> dict[str, Any]:
        server = self._owned(server_id, organization_id)
        if (server.get("auth") or {}).get("type") != "oauth":
            raise ValueError("server authentication is not OAuth")
        auth = dict(server.get("auth") or {})
        auth["redirect_uri"] = redirect_uri
        server["auth"] = auth
        server["updated_at"] = utcnow().isoformat()
        self._upsert(server)
        redirect_ready = asyncio.Event()
        callback_future: asyncio.Future[AuthorizationCodeResult] = (
            asyncio.get_running_loop().create_future()
        )
        flow: dict[str, Any] = {
            "server_id": server_id,
            "redirect_ready": redirect_ready,
            "callback": callback_future,
            "authorization_url": None,
            "state": None,
        }

        async def redirect_handler(url: str) -> None:
            state = (parse_qs(urlparse(url).query).get("state") or [None])[0]
            flow["authorization_url"] = url
            flow["state"] = state
            if state:
                self._oauth_flows[state] = flow
            redirect_ready.set()

        async def callback_handler() -> AuthorizationCodeResult:
            return await callback_future

        task = asyncio.create_task(
            self._oauth_connect(
                server,
                redirect_uri=redirect_uri,
                redirect_handler=redirect_handler,
                callback_handler=callback_handler,
            )
        )
        flow["task"] = task
        wait_redirect = asyncio.create_task(redirect_ready.wait())
        done, _ = await asyncio.wait(
            {task, wait_redirect}, timeout=20, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            wait_redirect.cancel()
            await task
            return {"connected": True, "server": self.public(self._get(server_id))}
        if wait_redirect not in done:
            task.cancel()
            raise TimeoutError("OAuth server did not provide an authorization URL")
        return {
            "connected": False,
            "authorization_url": flow["authorization_url"],
            "state": flow["state"],
        }

    async def complete_oauth(self, *, state: str, code: str, iss: str | None) -> str:
        flow = self._oauth_flows.pop(state, None)
        if not flow:
            raise KeyError("OAuth request expired; start the connection again")
        future: asyncio.Future[AuthorizationCodeResult] = flow["callback"]
        if not future.done():
            future.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
        task: asyncio.Task[Any] = flow["task"]
        await asyncio.wait_for(task, timeout=45)
        return str(flow["server_id"])

    async def _oauth_connect(
        self,
        server: dict[str, Any],
        *,
        redirect_uri: str,
        redirect_handler: Any,
        callback_handler: Any,
    ) -> None:
        provider = self._oauth_provider(
            server,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        try:
            tools = await self._discover(server, oauth_provider=provider)
            self._store_discovery(self._get(server["id"]), tools)
        except Exception as exc:
            self._store_failure(self._get(server["id"]), exc)
            raise

    def call_tool(
        self,
        *,
        server_id: str,
        remote_name: str,
        arguments: dict[str, Any],
        organization_id: str,
    ) -> dict[str, Any]:
        server = self._owned(server_id, organization_id)
        if not server.get("enabled") or server.get("status") != "ready":
            raise RuntimeError("MCP server is not ready")
        self.store.telemetry.record(
            "mcp.tool.call", component="mcp", phase="started",
            input={"server_id": server_id, "remote_name": remote_name, "arguments": arguments},
            organization_id=organization_id,
        )
        try:
            result = self._run_async(self._call(server, remote_name, arguments))
        except Exception as exc:
            self.store.telemetry.record(
                "mcp.tool.call", component="mcp", phase="failed",
                error={"type": type(exc).__name__, "message": str(exc)}, organization_id=organization_id,
            )
            raise
        output = {
            "server_id": server_id,
            "remote_tool": remote_name,
            "result": _jsonable(result),
        }
        self.store.telemetry.record(
            "mcp.tool.call", component="mcp", phase="completed", output=output,
            organization_id=organization_id,
        )
        return output

    async def _call(
        self, server: dict[str, Any], remote_name: str, arguments: dict[str, Any]
    ) -> Any:
        async with self._client(server) as client:
            return await client.call_tool(
                remote_name, arguments, read_timeout_seconds=3600
            )

    async def _discover(
        self, server: dict[str, Any], oauth_provider: Any | None = None
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        async with self._client(server, oauth_provider=oauth_provider) as client:
            cursor: str | None = None
            while True:
                result = await client.list_tools(cursor=cursor, cache_mode="refresh")
                tools.extend(_jsonable(item) for item in result.tools)
                cursor = result.next_cursor
                if not cursor:
                    break
        return tools

    @asynccontextmanager
    async def _client(self, server: dict[str, Any], oauth_provider: Any | None = None):
        transport_kind = server["transport"]
        if transport_kind == "stdio":
            parameters = StdioServerParameters(
                command=server["command"],
                args=list(server.get("args") or []),
                env={**get_default_environment(), **dict(server.get("env") or {})},
            )
            transport = stdio_client(parameters)
        else:
            auth_config = server.get("auth") or {}
            headers: dict[str, str] = {}
            auth = oauth_provider
            if auth_config.get("type") == "api_key":
                headers[str(auth_config.get("header_name") or "Authorization")] = str(
                    auth_config.get("secret") or ""
                )
            elif auth_config.get("type") == "oauth" and auth is None:
                storage = _PersistentTokenStorage(self, server["id"])
                tokens = await storage.get_tokens()
                if not tokens:
                    raise PermissionError("OAuth connection is required")
                auth = self._oauth_provider(server)
            if transport_kind == "sse":
                transport = sse_client(
                    str(server["url"]), headers=headers or None, auth=auth,
                    timeout=60, sse_read_timeout=3600,
                )
            else:
                @asynccontextmanager
                async def http_transport():
                    async with create_mcp_http_client(
                        headers=headers or None, auth=auth
                    ) as http_client:
                        async with streamable_http_client(
                            str(server["url"]), http_client=http_client
                        ) as streams:
                            yield streams

                transport = http_transport()
        async with Client(transport, read_timeout_seconds=3600) as client:
            yield client

    def _oauth_provider(
        self,
        server: dict[str, Any],
        *,
        redirect_handler: Any | None = None,
        callback_handler: Any | None = None,
    ) -> OAuthClientProvider:
        auth_config = server.get("auth") or {}
        redirect_uri = auth_config.get("redirect_uri")
        if not redirect_uri:
            raise ValueError("OAuth redirect URI is not configured")
        metadata = OAuthClientMetadata(
            client_name="Toda",
            redirect_uris=[redirect_uri],
            scope=auth_config.get("scope") or None,
            application_type="web",
            token_endpoint_auth_method="none",
        )
        return OAuthClientProvider(
            str(server["url"]),
            metadata,
            _PersistentTokenStorage(self, server["id"]),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

    def load_registered_tools(self) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                "SELECT payload FROM mcp_servers WHERE status = 'ready' ORDER BY updated_at"
            )
            servers = [json.loads(row["payload"]) for row in cur.fetchall()]
        for server in servers:
            if server.get("enabled"):
                self._register_server_tools(server)

    def _store_discovery(
        self, server: dict[str, Any], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        server["tools"] = tools
        server["status"] = "ready"
        server["last_error"] = None
        server["last_tested_at"] = now
        server["updated_at"] = now
        self._upsert(server)
        self.store.record_connector_health(
            server["organization_id"],
            f"mcp:{server['id']}",
            status="ok",
            detail={"tool_count": len(tools)},
        )
        if server.get("enabled"):
            self._register_server_tools(server)
        self.audit.record(
            server["organization_id"],
            "mcp.server_discovered",
            {"server_id": server["id"], "tool_count": len(tools)},
        )
        return server

    def _store_failure(self, server: dict[str, Any], exc: Exception) -> None:
        server["status"] = "error"
        server["last_error"] = str(exc)[:1000]
        server["last_tested_at"] = utcnow().isoformat()
        server["updated_at"] = server["last_tested_at"]
        self._upsert(server)
        self.store.record_connector_health(
            server["organization_id"],
            f"mcp:{server['id']}",
            status="error",
            detail={"error_type": type(exc).__name__},
        )
        self._unregister_server_tools(server["id"])

    def _register_server_tools(self, server: dict[str, Any]) -> None:
        self._unregister_server_tools(server["id"])
        names: set[str] = set()
        for remote in server.get("tools") or []:
            remote_name = str(remote.get("name") or "")
            if not remote_name:
                continue
            local_name = self._local_tool_name(server, remote_name)
            schema = remote.get("inputSchema") or remote.get("input_schema") or {
                "type": "object",
                "additionalProperties": True,
            }
            self.registry.register(
                ToolDefinition(
                    name=local_name,
                    description=(
                        f"MCP · {server['name']}: "
                        f"{remote.get('description') or remote_name}"
                    ),
                    input_schema=schema,
                    risk="write",
                    approval_required=False,
                    toolkits=[f"mcp:{server['name']}"],
                    capability_id=f"mcp.{server['id']}.{remote_name}",
                    allowed_organization_ids=[server["organization_id"]],
                    timeout_seconds=3600,
                    retry_policy={"max_attempts": 3},
                ),
                lambda args, runtime, sid=server["id"], rn=remote_name: self.call_tool(
                    server_id=sid,
                    remote_name=rn,
                    arguments=args,
                    organization_id=runtime["organization_id"],
                ),
            )
            names.add(local_name)
        self._registered_by_server[server["id"]] = names

    def _unregister_server_tools(self, server_id: str) -> None:
        for name in self._registered_by_server.pop(server_id, set()):
            self.registry.unregister(name)

    @staticmethod
    def _local_tool_name(server: dict[str, Any], remote_name: str) -> str:
        digest = hashlib.sha256(
            f"{server['id']}:{remote_name}".encode("utf-8")
        ).hexdigest()[:8]
        prefix = f"mcp_{_safe_slug(server['name'])}_"
        suffix = _safe_slug(remote_name)
        available = 64 - len(prefix) - len(digest) - 1
        return f"{prefix}{suffix[:max(1, available)]}_{digest}"

    def _owned(self, server_id: str, organization_id: str) -> dict[str, Any]:
        server = self._get(server_id)
        if server.get("organization_id") != organization_id:
            raise KeyError(server_id)
        return server

    def _get(self, server_id: str) -> dict[str, Any]:
        server = self.store.get_harness_record("mcp_servers", server_id)
        if not server:
            raise KeyError(server_id)
        return server

    def _upsert(self, server: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO mcp_servers
                (id, organization_id, name, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    server["id"], server["organization_id"], server["name"],
                    server["status"], json.dumps(server), server["created_at"],
                    server["updated_at"],
                ),
            )

    @staticmethod
    def _run_async(coroutine: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        result: list[Any] = []
        error: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(asyncio.run(coroutine))
            except BaseException as exc:  # pragma: no cover - event-loop bridge
                error.append(exc)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0]
