"""Docket API entry point."""

from __future__ import annotations

import os
import asyncio
import secrets
import time
import argparse
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.sessions import SessionMiddleware

from docket.api.routes import router
from docket.config import settings
from docket.services.container import DocketApp, get_app, reset_app

log = logging.getLogger("docket")


def _dashboard_session_secret() -> str:
    """Resolve the key SessionMiddleware signs the console login cookie
    with. Falls back through the Slack OAuth app secret before generating a
    random per-process key, so a bare local run still works — just without
    sessions surviving a restart."""
    if settings.dashboard_session_secret:
        return settings.dashboard_session_secret
    if settings.slack_client_secret:
        return settings.slack_client_secret
    log.warning(
        "No DOCKET_DASHBOARD_SESSION_SECRET or DOCKET_SLACK_CLIENT_SECRET configured; "
        "using a random per-process session secret. Dashboard logins will not "
        "survive a restart of this process."
    )
    return secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    prebuilt_brain = getattr(app.state, "prebuilt_brain", None)
    if prebuilt_brain is not None:
        brain = prebuilt_brain
    else:
        db = os.environ.get("DOCKET_DB")
        brain = reset_app(Path(db) if db else settings.db_path)
    app.state.brain = brain
    brain.indexing.backfill()
    worker_task: asyncio.Task[None] | None = None
    if settings.worker_enabled:
        async def _worker_loop() -> None:
            last_automation_tick = 0.0
            while True:
                if time.monotonic() - last_automation_tick >= 30:
                    await asyncio.to_thread(brain.automations.tick)
                    last_automation_tick = time.monotonic()
                result = await asyncio.to_thread(brain.harness_worker.drain_once)
                if result["leased"] == 0:
                    await asyncio.sleep(settings.worker_poll_interval_seconds)
                else:
                    await asyncio.sleep(0)

        worker_task = asyncio.create_task(
            _worker_loop(), name="docket-harness-worker"
        )
    yield
    # Coordinated shutdown: stop accepting new Slack events first, then let
    # the worker finish draining in-flight jobs, then close the database.
    slack_stopper = getattr(app.state, "slack_stopper", None)
    if slack_stopper:
        slack_stopper()
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    brain.close()


def create_app(
    *,
    prebuilt_brain: DocketApp | None = None,
    slack_stopper: Callable[[], None] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Docket",
        description="Slack-native company-wide memory and agent harness",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.prebuilt_brain = prebuilt_brain
    app.state.slack_stopper = slack_stopper
    app.include_router(router)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_dashboard_session_secret(),
        session_cookie="docket_session",
        same_site="lax",
    )

    @app.middleware("http")
    async def request_telemetry(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req_{time.time_ns():x}"
        started = time.perf_counter()
        suppress_audit = request.url.path == "/v1/admin/organizations:delete"
        try:
            response = await call_next(request)
        except Exception:
            brain = getattr(request.app.state, "brain", None)
            if brain and not suppress_audit:
                brain.audit.record(settings.default_org_id, "http.request", {"request_id": request_id, "path": request.url.path, "method": request.method, "status_code": 500, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
            raise
        response.headers["X-Request-ID"] = request_id
        brain = getattr(request.app.state, "brain", None)
        if brain and not suppress_audit:
            brain.audit.record(settings.default_org_id, "http.request", {"request_id": request_id, "path": request.url.path, "method": request.method, "status_code": response.status_code, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
        return response
    return app


app = create_app()


def serve(target_app: FastAPI | str | None = None) -> None:
    uvicorn.run(
        target_app or "docket.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level,
        # Every request is already recorded as an `http.request` audit event
        # (request_telemetry above) — uvicorn's own access-log line for it is
        # pure duplication, and it's mostly the console's own 8s polling
        # (approvals badge, dashboard refresh), which drowns out log lines
        # that actually matter (e.g. "Slack event received: ...").
        access_log=False,
    )


def _register_composio_webhook(brain: DocketApp) -> None:
    """Point Composio's one-per-project webhook subscription at this gateway.

    Requires DOCKET_PUBLIC_URL (an ngrok/cloudflared tunnel to this process)
    so Composio's cloud can reach the local /v1/composio/webhooks endpoint.
    Also captures the signing secret Composio returns so no manual .env
    copy step is needed.
    """
    if not (brain.composio.configured and settings.public_url):
        return
    webhook_url = f"{settings.public_url.rstrip('/')}/v1/composio/webhooks"
    try:
        existing = brain.composio.list_webhook_subscriptions().get("items") or []
        if existing:
            subscription = existing[0]
            if subscription.get("webhook_url") != webhook_url:
                subscription = brain.composio.update_webhook_subscription(
                    subscription["id"], webhook_url=webhook_url
                )
        else:
            event_types = brain.composio.list_webhook_event_types().get("items") or []
            enabled_events = [e["event_type"] for e in event_types]
            subscription = brain.composio.create_webhook_subscription(
                webhook_url=webhook_url,
                enabled_events=enabled_events or ["trigger.instance.event"],
            )
        secret = subscription.get("secret")
        if secret:
            brain.composio.webhook_secret = secret
        print(f"Docket gateway: Composio webhooks registered at {webhook_url}")
    except Exception as exc:
        print(f"Docket gateway: Composio webhook registration failed: {exc}")


def gateway() -> None:
    """Run the local control plane and, when configured, Slack Socket Mode.

    Builds exactly one ``DocketApp`` and shares it between the HTTP API and
    Slack Socket Mode, so the process runs one service container and one
    worker loop rather than two independent copies of each.
    """
    brain = reset_app(settings.db_path)
    brain.indexing.backfill()
    _register_composio_webhook(brain)

    slack_stopper: Callable[[], None] | None = None
    if settings.slack_app_token:
        from docket.slack_socket import run_socket_mode

        stop_event = threading.Event()

        def _socket_runner() -> None:
            try:
                asyncio.run(run_socket_mode(brain, stop_event=stop_event))
            except Exception as exc:
                # The setup UI remains available if Slack is not ready yet.
                print(f"Docket Slack gateway unavailable: {exc}")

        socket_thread = threading.Thread(
            target=_socket_runner,
            name="docket-slack-gateway",
            daemon=True,
        )
        socket_thread.start()
        print("Docket gateway: Slack Socket Mode starting")

        def _stop_socket_mode() -> None:
            stop_event.set()
            socket_thread.join(timeout=10)

        slack_stopper = _stop_socket_mode
    else:
        print("Docket gateway: configure Slack at http://127.0.0.1:8080/setup")

    gateway_app = create_app(prebuilt_brain=brain, slack_stopper=slack_stopper)
    serve(gateway_app)


def retention_cli(args: argparse.Namespace) -> None:
    """Preview or apply retention cleanup, or delete an organization entirely."""
    brain = get_app(settings.db_path)
    try:
        if args.delete_organization:
            if not args.confirm:
                parser_error = "--delete-organization requires --confirm=<organization_id>"
                print(parser_error)
                raise SystemExit(2)
            result = brain.retention.delete_organization(
                args.organization_id, confirmation=args.confirm
            )
            print(
                f"Deleted organization {args.organization_id}: "
                f"{result['deleted_total']} rows removed across "
                f"{len(result['deleted'])} tables. Not recoverable."
            )
            return

        if args.older_than_days is None:
            print("retention requires --older-than-days (or --delete-organization)")
            raise SystemExit(2)

        if args.apply:
            if not args.confirmed_by:
                print("--apply requires --confirmed-by=<principal>")
                raise SystemExit(2)
            result = brain.retention.apply(
                args.organization_id,
                older_than_days=args.older_than_days,
                confirmed_by=args.confirmed_by,
            )
            counts = result["deleted"]
            print(
                f"Deleted {result['deleted_total']} rows older than "
                f"{args.older_than_days} days (cutoff {result['cutoff']})."
            )
        else:
            result = brain.retention.preview(
                args.organization_id, older_than_days=args.older_than_days
            )
            counts = result["counts"]
            print(
                f"Preview: {result['total']} rows older than {args.older_than_days} "
                f"days would be deleted (cutoff {result['cutoff']}). "
                "Pass --apply --confirmed-by=<principal> to actually delete."
            )
        for table, count in sorted(counts.items()):
            if count:
                print(f"  {table}: {count}")
    except ValueError as exc:
        print(f"error: {exc}")
        raise SystemExit(2) from exc
    finally:
        brain.close()


def reset_cli(args: argparse.Namespace) -> None:
    """Delete the configured local database and its SQLite sidecar files."""
    db_path = settings.db_path.resolve()
    if not args.yes:
        print(
            f"This permanently deletes all Docket data in {db_path}. "
            "Pass --yes to continue."
        )
        raise SystemExit(2)

    removed: list[Path] = []
    for path in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        Path(f"{db_path}-journal"),
    ):
        if path.exists():
            path.unlink()
            removed.append(path)
    if removed:
        print(f"Reset Docket database: {db_path}")
        print(f"Removed {len(removed)} database file(s).")
    else:
        print(f"Docket database is already clean: {db_path}")


def main() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="docket",
        description="Docket local control plane and company-agent gateway",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="serve the API")
    sub.add_parser(
        "gateway", help="run the API plus configured messaging gateway"
    )
    reset = sub.add_parser(
        "reset",
        help="permanently delete the configured local database and all stored data",
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="confirm deletion of the configured SQLite database",
    )
    retention = sub.add_parser(
        "retention",
        help="preview or apply data retention cleanup, or delete an organization",
    )
    retention.add_argument("--organization-id", required=True)
    retention.add_argument("--older-than-days", type=int, default=None)
    retention.add_argument(
        "--apply",
        action="store_true",
        help="actually delete rows (default is a dry-run preview)",
    )
    retention.add_argument(
        "--confirmed-by", default=None, help="principal id, required with --apply"
    )
    retention.add_argument(
        "--delete-organization",
        action="store_true",
        help="permanently delete the entire organization and all its data",
    )
    retention.add_argument(
        "--confirm",
        default=None,
        help="must exactly match --organization-id, required with --delete-organization",
    )
    args = parser.parse_args()
    if args.command == "gateway":
        gateway()
    elif args.command == "reset":
        reset_cli(args)
    elif args.command == "retention":
        retention_cli(args)
    else:
        serve()


if __name__ == "__main__":
    main()
