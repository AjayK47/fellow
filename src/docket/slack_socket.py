"""Slack Socket Mode development runner."""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import httpx
from websockets.asyncio.client import connect

from docket.config import settings
from docket.services.container import DocketApp
from docket.services.gateway_setup import GatewaySetupService

log = logging.getLogger("docket.slack_socket")

_RECV_POLL_SECONDS = 1.0


async def _ensure_installation(brain: DocketApp) -> None:
    """Bootstrap the tenant/installation record for a Slack app configured
    directly through ``.env`` rather than the setup wizard.

    Socket Mode itself only needs the app token to open a connection —
    without a matching ``slack_installations`` row, organization resolution
    for every incoming event silently falls back to ``default_org_id``
    instead of a real per-workspace mapping, and no admin identity or
    channel policy ever gets created. This mirrors exactly what
    ``GatewaySetupService.connect_slack`` does, so a workspace connected via
    the setup wizard is unaffected (already registered, so this is a no-op).
    """
    if not settings.slack_bot_token:
        return
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            log.warning(
                "Slack auth.test failed while bootstrapping installation: %s",
                payload.get("error"),
            )
            return
        team_id = str(payload["team_id"])
    except Exception as exc:
        log.warning(
            "Could not verify Slack bot identity for installation bootstrap: %s", exc
        )
        return
    if brain.store.slack_installation_by_workspace(team_id):
        return
    setup = GatewaySetupService(
        brain.store, brain.graph, brain.audit, skills=brain.skills
    )
    try:
        await asyncio.to_thread(
            setup.connect_slack,
            organization_id=settings.default_org_id,
            organization_name=settings.default_org_id,
            bot_token=settings.slack_bot_token,
            app_token=settings.slack_app_token or "",
            signing_secret=settings.slack_signing_secret or "",
        )
        log.info(
            "Slack installation bootstrapped: workspace=%s organization=%s",
            team_id,
            settings.default_org_id,
        )
    except Exception as exc:
        log.warning("Slack installation bootstrap failed: %s", exc)


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


async def run_socket_mode(
    brain: DocketApp | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Run the Slack Socket Mode client against *brain*.

    When *brain* is omitted (the standalone ``docket-slack`` deployment
    mode), this builds and owns its own ``DocketApp`` plus worker-drain loop,
    exactly as before. When a *brain* is supplied (the combined ``gateway``
    command), this becomes a pure transport attached to that already-running
    instance: it does not seed, backfill, or drain jobs itself — the caller's
    own lifespan already does that against the same shared instance.
    """
    if not settings.slack_app_token:
        raise RuntimeError("DOCKET_SLACK_APP_TOKEN is required")
    owns_brain = brain is None
    if owns_brain:
        brain = DocketApp(settings.db_path)
        brain.indexing.backfill()

    worker: asyncio.Task[None] | None = None
    if owns_brain:
        async def worker_loop() -> None:
            while True:
                result = await asyncio.to_thread(brain.harness_worker.drain_once)
                await asyncio.sleep(
                    0 if result["leased"] else settings.worker_poll_interval_seconds
                )

        worker = asyncio.create_task(worker_loop())

    await _ensure_installation(brain)

    stop_event = stop_event or threading.Event()
    try:
        while not stop_event.is_set():
            url = await _socket_url(settings.slack_app_token)
            log.info("Slack Socket Mode connected to %s", settings.slack_app_token[:24] + "...")
            try:
                async with connect(url, ping_interval=20, ping_timeout=20) as socket:
                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(
                                socket.recv(), timeout=_RECV_POLL_SECONDS
                            )
                        except asyncio.TimeoutError:
                            continue
                        envelope = json.loads(raw)
                        envelope_id = envelope.get("envelope_id")
                        if envelope_id:
                            # Slack requires prompt Socket Mode acknowledgement.
                            await socket.send(
                                json.dumps({"envelope_id": envelope_id})
                            )
                        envelope_type = envelope.get("type")
                        if envelope_type == "interactive":
                            interaction_payload = envelope.get("payload") or {}
                            action = (interaction_payload.get("actions") or [{}])[0]
                            log.info(
                                "Slack interaction received: action_id=%s user=%s",
                                action.get("action_id"),
                                (interaction_payload.get("user") or {}).get("id"),
                            )
                            try:
                                result = await asyncio.to_thread(
                                    brain.slack_gateway.accept_interaction,
                                    body=json.dumps(interaction_payload).encode(),
                                    headers={},
                                    allow_unsigned=True,
                                )
                                log.info(
                                    "Slack interaction resolved: accepted=%s job_id=%s",
                                    result.get("accepted"),
                                    result.get("job_id", ""),
                                )
                            except Exception as exc:
                                log.warning("Slack interaction rejected: %s", exc)
                            continue
                        if envelope_type != "events_api":
                            continue
                        payload = envelope.get("payload") or {}
                        inner = payload.get("event") or {}
                        log.info(
                            "Slack event received: type=%s channel=%s user=%s ts=%s",
                            inner.get("type"),
                            inner.get("channel"),
                            inner.get("user"),
                            inner.get("ts"),
                        )
                        result = await asyncio.to_thread(
                            brain.slack_gateway.accept,
                            body=json.dumps(payload).encode(),
                            headers={},
                            allow_unsigned=True,
                        )
                        log.info(
                            "Slack gateway accept: accepted=%s event_id=%s reason=%s job_id=%s",
                            result.get("accepted"),
                            result.get("event_id"),
                            result.get("reason", ""),
                            result.get("job_id", ""),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                if stop_event.is_set():
                    break
                await asyncio.sleep(2)
    finally:
        if owns_brain:
            if worker:
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
