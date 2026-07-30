"""Slack Socket Mode development runner."""

from __future__ import annotations

import asyncio
import json

import httpx
from websockets.asyncio.client import connect

from company_brain.config import settings
from company_brain.fixtures.seed import seed_synthetic_org
from company_brain.services.container import BrainApp


async def _socket_url(app_token: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://slack.com/api/apps.connections.open",
            headers={"Authorization": f"Bearer {app_token}"},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok") or not payload.get("url"):
            raise RuntimeError(
                f"Slack Socket Mode connection failed: {payload.get('error')}"
            )
        return str(payload["url"])


async def run_socket_mode() -> None:
    if not settings.slack_app_token:
        raise RuntimeError("COMPANY_BRAIN_SLACK_APP_TOKEN is required")
    brain = BrainApp(settings.db_path)
    if not brain.store.get_organization(settings.default_org_id):
        seed_synthetic_org(brain)
    brain.indexing.backfill()

    async def worker_loop() -> None:
        while True:
            result = await asyncio.to_thread(brain.harness_worker.drain_once)
            await asyncio.sleep(
                0 if result["leased"] else settings.worker_poll_interval_seconds
            )

    worker = asyncio.create_task(worker_loop())
    try:
        while True:
            url = await _socket_url(settings.slack_app_token)
            try:
                async with connect(url, ping_interval=20, ping_timeout=20) as socket:
                    async for raw in socket:
                        envelope = json.loads(raw)
                        envelope_id = envelope.get("envelope_id")
                        if envelope_id:
                            # Slack requires prompt Socket Mode acknowledgement.
                            await socket.send(
                                json.dumps({"envelope_id": envelope_id})
                            )
                        if envelope.get("type") != "events_api":
                            continue
                        payload = envelope.get("payload") or {}
                        await asyncio.to_thread(
                            brain.slack_gateway.accept,
                            body=json.dumps(payload).encode(),
                            headers={},
                            allow_unsigned=True,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        brain.close()


def main() -> None:
    asyncio.run(run_socket_mode())


if __name__ == "__main__":
    main()
