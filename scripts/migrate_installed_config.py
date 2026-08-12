#!/usr/bin/env python3
"""Migrate a source-checkout Toda .env into a running local installation.

Secrets are read locally and sent only to Toda's loopback setup API. The script
prints validation outcomes, never credential values.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import dotenv_values


def required(env: dict[str, str | None], key: str) -> str:
    value = str(env.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Source configuration is missing {key}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--setup-url", required=True)
    args = parser.parse_args()

    env = dict(dotenv_values(args.source))
    token = (parse_qs(urlparse(args.setup_url).query).get("bootstrap") or [""])[0]
    if not token:
        raise RuntimeError("Setup URL has no bootstrap token")

    client = httpx.Client(base_url="http://127.0.0.1:8080", timeout=30)
    response = client.post("/v1/gateway/setup/bootstrap", json={"token": token})
    response.raise_for_status()

    provider_roles = [
        (
            "reasoning",
            "TODA_MODEL_API_KEY",
            {
                "main": required(env, "TODA_CHAT_MODEL"),
                "attention": required(env, "TODA_ATTENTION_MODEL"),
                "extraction": required(env, "TODA_EXTRACTION_MODEL"),
            },
        ),
        ("embedding", "TODA_EMBEDDING_API_KEY", {"embedding": required(env, "TODA_EMBEDDING_MODEL")}),
        ("image", "TODA_IMAGE_API_KEY", {"image": required(env, "TODA_IMAGE_MODEL")}),
        (
            "transcription",
            "TODA_TRANSCRIPTION_API_KEY",
            {"transcription": required(env, "TODA_TRANSCRIPTION_MODEL")},
        ),
        ("speech", "TODA_TTS_API_KEY", {"tts": required(env, "TODA_TTS_MODEL")}),
    ]
    for connection_id, key_name, assignments in provider_roles:
        credential = str(env.get(key_name) or "").strip()
        if not credential and connection_id in {"image", "transcription", "speech"}:
            # The legacy configuration treated blank media keys as "reuse the
            # primary OpenRouter credential". Make that relationship explicit
            # instead of creating empty provider connections.
            connection_id = "reasoning"
            credential = required(env, "TODA_MODEL_API_KEY")
        response = client.post(
            "/v1/gateway/setup/providers",
            json={
                "connection_id": connection_id,
                "provider": "openrouter",
                "api_key": credential or required(env, key_name),
                "assignments": assignments,
            },
        )
        response.raise_for_status()
        print(f"configured provider connection: {connection_id} ({', '.join(assignments)})")

    for connection_id, provider, key_name, search, extraction in (
        ("brave-search", "brave", "BRAVE_SEARCH_API_KEY", True, False),
        ("firecrawl-extract", "firecrawl", "FIRECRAWL_API_KEY", False, True),
    ):
        response = client.post(
            "/v1/gateway/setup/tools",
            json={
                "connection_id": connection_id,
                "provider": provider,
                "api_key": required(env, key_name),
                "use_for_search": search,
                "use_for_extraction": extraction,
                "web_fetch_enabled": True,
            },
        )
        response.raise_for_status()
        print(f"configured tool connection: {connection_id}")

    slack = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {required(env, 'TODA_SLACK_BOT_TOKEN')}"},
        timeout=20,
    )
    slack.raise_for_status()
    slack_identity = slack.json()
    if not slack_identity.get("ok"):
        raise RuntimeError(f"Slack rejected the existing bot token: {slack_identity.get('error')}")
    workspace_name = str(slack_identity.get("team") or "Toda workspace")
    team_id = str(slack_identity.get("team_id") or "workspace")
    response = client.post(
        "/v1/gateway/setup/slack",
        json={
            "organization_id": f"org_slack_{team_id.lower()}",
            "organization_name": workspace_name,
            "bot_token": required(env, "TODA_SLACK_BOT_TOKEN"),
            "app_token": required(env, "TODA_SLACK_APP_TOKEN"),
            "signing_secret": required(env, "TODA_SLACK_SIGNING_SECRET"),
            "admin_slack_user_id": str(slack_identity.get("user_id") or "") or None,
            "admin_name": str(slack_identity.get("user") or "") or None,
        },
    )
    response.raise_for_status()
    print(f"connected Slack workspace: {workspace_name}")

    # Copy non-secret runtime preferences not represented by setup forms.
    installed_env = Path.home() / "Library" / "Application Support" / "Toda" / ".env"
    current = installed_env.read_text(encoding="utf-8")
    preferences = {
        key: value
        for key in (
            "TODA_SLACK_CLIENT_ID",
            "TODA_SLACK_CLIENT_SECRET",
            "TODA_COMPOSIO_API_KEY",
            "TODA_PUBLIC_URL",
            "TODA_BROWSER_ENABLED",
            "TODA_BROWSER_ALLOWED_DOMAINS",
            "TODA_BROWSER_CDP_ENABLED",
            "TODA_MAIN_MODEL_SUPPORTS_VISION",
            "TODA_TRANSCRIPTION_LANGUAGE",
            "TODA_TTS_VOICE",
            "TODA_TTS_FORMAT",
            "TODA_MEDIA_MAX_BYTES",
        )
        if (value := env.get(key)) is not None
    }
    lines = [line for line in current.splitlines() if line.partition("=")[0] not in preferences]
    lines.extend(f"{key}={value}" for key, value in preferences.items())
    installed_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    installed_env.chmod(0o600)
    print("copied runtime preferences: " + ", ".join(sorted(preferences)))

    status = client.get("/v1/gateway/setup/status").json()
    print(json.dumps({"setup_complete": status["setup"]["complete"]}))


if __name__ == "__main__":
    main()
