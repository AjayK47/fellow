"""Narrow Composio v3.1 client for approved Linear and GitHub tools."""

from __future__ import annotations

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
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.connected_account_id = connected_account_id
        self.toolkit_versions = toolkit_versions

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.connected_account_id)

    def execute(
        self,
        tool_slug: str,
        arguments: dict[str, Any],
        *,
        connected_account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = connected_account_id or self.connected_account_id
        if not self.api_key or not account_id:
            raise RuntimeError(
                "Composio is not configured; set COMPANY_BRAIN_COMPOSIO_API_KEY "
                "and COMPANY_BRAIN_COMPOSIO_CONNECTED_ACCOUNT_ID"
            )
        response = httpx.post(
            f"{self.base_url}/tools/execute/{tool_slug}",
            params={"toolkit_versions": self.toolkit_versions},
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "connected_account_id": account_id,
                "arguments": arguments,
            },
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()

    def describe(self, tool_slug: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("Composio API key is not configured")
        response = httpx.get(
            f"{self.base_url}/tools/{tool_slug}",
            params={"toolkit_versions": self.toolkit_versions},
            headers={"x-api-key": self.api_key},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()
