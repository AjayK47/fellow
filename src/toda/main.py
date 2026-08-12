"""Toda API entry point."""

from __future__ import annotations

import os
import asyncio
import secrets
import time
import argparse
import logging
import shutil
import subprocess
import sys
import threading
import webbrowser
import plistlib
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

import uvicorn
from fastapi import Depends, FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware

from toda.api.routes import enforce_tenant, router
from toda.config import settings
from toda.runtime_owner import DatabaseOwner
from toda.services.container import TodaApp, get_app, reset_app
from toda.services.gateway_setup import GatewaySetupService, SetupBootstrapToken

log = logging.getLogger("toda")


def _toda_executable() -> str:
    return shutil.which("toda") or str(Path(sys.executable).with_name("toda"))


def _wait_for_gateway(timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{settings.port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"Toda service did not become healthy within {timeout_seconds:g}s")


def service_cli(args: argparse.Namespace) -> None:
    """Install the one Toda gateway as a per-user login service."""
    data_dir = settings.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "toda.log"
    executable = _toda_executable()
    environment = {
        "TODA_DATA_DIR": str(data_dir),
        "TODA_ENV_FILE": str(data_dir / ".env"),
        "PYTHONUNBUFFERED": "1",
    }

    if args.action == "update":
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit("uv is required to update Toda")
        subprocess.run([uv, "tool", "upgrade", "toda-ai"], check=True)
        args.action = "restart"

    if args.action == "status":
        if sys.platform == "darwin":
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/ai.toda.gateway"],
                check=False,
            )
        elif os.name == "nt":
            result = subprocess.run(["schtasks", "/Query", "/TN", "Toda"], check=False)
        else:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "toda.service"], check=False
            )
        raise SystemExit(result.returncode)

    if sys.platform == "darwin":
        service_path = Path.home() / "Library" / "LaunchAgents" / "ai.toda.gateway.plist"
        domain = f"gui/{os.getuid()}"
        service_name = f"{domain}/ai.toda.gateway"
        if args.action in {"stop", "restart"}:
            subprocess.run(["launchctl", "bootout", service_name], check=False)
            if args.action == "stop":
                print("Toda service stopped.")
                return
        if args.action == "start" and service_path.exists():
            subprocess.run(["launchctl", "bootstrap", domain, str(service_path)], check=False)
            subprocess.run(["launchctl", "kickstart", "-k", service_name], check=True)
            print("Toda service started.")
            return
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_bytes(
            plistlib.dumps(
                {
                    "Label": "ai.toda.gateway",
                    "ProgramArguments": [executable, "gateway"],
                    "EnvironmentVariables": environment,
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "StandardOutPath": str(log_path),
                    "StandardErrorPath": str(log_path),
                }
            )
        )
        subprocess.run(["launchctl", "bootstrap", domain, str(service_path)], check=False)
        subprocess.run(
            ["launchctl", "kickstart", "-k", service_name], check=True
        )
    elif os.name == "nt":
        if args.action in {"stop", "restart"}:
            subprocess.run(["schtasks", "/End", "/TN", "Toda"], check=False)
            if args.action == "stop":
                print("Toda service stopped.")
                return
        if args.action == "start":
            subprocess.run(["schtasks", "/Run", "/TN", "Toda"], check=True)
            print("Toda service started.")
            return
        command = f'"{executable}" gateway'
        subprocess.run(
            [
                "schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "Toda",
                "/TR", command,
            ],
            check=True,
        )
        subprocess.run(["schtasks", "/Run", "/TN", "Toda"], check=True)
    else:
        service_path = Path.home() / ".config" / "systemd" / "user" / "toda.service"
        if args.action in {"stop", "restart"}:
            subprocess.run(["systemctl", "--user", "stop", "toda.service"], check=False)
            if args.action == "stop":
                print("Toda service stopped.")
                return
        if args.action == "start":
            subprocess.run(["systemctl", "--user", "start", "toda.service"], check=True)
            print("Toda service started.")
            return
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_text(
            "\n".join(
                [
                    "[Unit]", "Description=Toda organizational coworker", "After=network-online.target", "",
                    "[Service]", f'ExecStart="{executable.replace(chr(34), chr(92) + chr(34))}" gateway', "Restart=on-failure", "RestartSec=3",
                    *(f'Environment=\"{key}={value}\"' for key, value in environment.items()),
                    f"StandardOutput=append:{log_path}", f"StandardError=append:{log_path}", "",
                    "[Install]", "WantedBy=default.target", "",
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "toda.service"], check=True
        )

    print(f"Toda service installed. Logs: {log_path}")
    # The installer is already a trusted local process. Issuing the token here
    # works identically on launchd, systemd, and Windows Task Scheduler and
    # avoids scraping platform-specific service logs.
    _wait_for_gateway()
    bootstrap = SetupBootstrapToken(data_dir).issue()
    setup_url = f"http://127.0.0.1:{settings.port}/setup?bootstrap={bootstrap}"
    print(f"Continue setup: {setup_url}")
    if not args.no_open:
        webbrowser.open(setup_url)


def validate_runtime_exposure() -> None:
    """Refuse explicitly unsafe combinations on a network-bound server."""
    host = settings.host.strip().lower()
    local_only = host in {"127.0.0.1", "::1", "localhost"}
    if not local_only and settings.dashboard_auth_disabled:
        raise RuntimeError(
            "TODA_DASHBOARD_AUTH_DISABLED cannot be used when TODA_HOST is "
            "network-accessible"
        )
    if not local_only and settings.auto_approve_writes:
        raise RuntimeError(
            "TODA_AUTO_APPROVE_WRITES cannot be used when TODA_HOST is "
            "network-accessible"
        )


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
        "No TODA_DASHBOARD_SESSION_SECRET or TODA_SLACK_CLIENT_SECRET configured; "
        "using a random per-process session secret. Dashboard logins will not "
        "survive a restart of this process."
    )
    return secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    owner = getattr(app.state, "database_owner", None)
    if owner is None:
        db = os.environ.get("TODA_DB")
        owner = DatabaseOwner(Path(db) if db else settings.db_path)
        app.state.database_owner = owner
    owner.acquire()
    worker_tasks: list[asyncio.Task[None]] = []
    brain: TodaApp | None = None
    try:
        prebuilt_brain = getattr(app.state, "prebuilt_brain", None)
        if prebuilt_brain is not None:
            brain = prebuilt_brain
        else:
            db = os.environ.get("TODA_DB")
            brain = reset_app(Path(db) if db else settings.db_path)
        app.state.brain = brain
        brain.indexing.backfill()
        if settings.worker_enabled:
            # Independent lanes keep long agent work and ingestion from
            # delaying infrastructure jobs, while all lanes remain inside
            # the single database-owning process.
            async def _lane(kinds: list[str], *, tick_automations: bool) -> None:
                last_dispatcher_tick = 0.0
                last_recovery_tick = 0.0
                while True:
                    if tick_automations and (
                        time.monotonic() - last_dispatcher_tick
                        >= settings.dispatcher_tick_interval_seconds
                    ):
                        await asyncio.to_thread(brain.automations.tick)
                        last_dispatcher_tick = time.monotonic()
                    if (
                        tick_automations
                        and time.monotonic() - last_recovery_tick >= 30
                    ):
                        for organization in brain.store.list_organizations():
                            await asyncio.to_thread(
                                brain.recovery_heartbeat.tick, organization["id"]
                            )
                            await asyncio.to_thread(
                                brain.proactive.heartbeat, organization["id"]
                            )
                        last_recovery_tick = time.monotonic()
                    # pump() starts work without waiting for it, so a long
                    # agent run never stalls this loop's next lease.
                    result = await asyncio.to_thread(
                        brain.harness_worker.pump, kinds=kinds
                    )
                    if result["leased"] == 0:
                        await asyncio.sleep(settings.worker_poll_interval_seconds)
                    else:
                        await asyncio.sleep(0)

            worker_tasks = [
                asyncio.create_task(
                    _lane(
                        brain.harness_worker.infrastructure_job_kinds(),
                        tick_automations=True,
                    ),
                    name="toda-harness-worker-infra",
                ),
                asyncio.create_task(
                    _lane(
                        brain.harness_worker.agent_job_kinds(), tick_automations=False
                    ),
                    name="toda-harness-worker-agent",
                ),
                asyncio.create_task(
                    _lane(
                        brain.harness_worker.knowledge_job_kinds(), tick_automations=False
                    ),
                    name="toda-harness-worker-knowledge",
                ),
            ]
        yield
    finally:
        # Coordinated shutdown: stop accepting new Slack events first, then let
        # the worker finish draining in-flight jobs, then close the database.
        slack_stopper = getattr(app.state, "slack_stopper", None)
        if slack_stopper:
            slack_stopper()
        for worker_task in worker_tasks:
            worker_task.cancel()
        for worker_task in worker_tasks:
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        if brain is not None:
            brain.close()
        owner.release()


def create_app(
    *,
    prebuilt_brain: TodaApp | None = None,
    slack_stopper: Callable[[], None] | None = None,
    database_owner: DatabaseOwner | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Toda",
        description="Slack-native company-wide memory and agent harness",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.prebuilt_brain = prebuilt_brain
    app.state.slack_stopper = slack_stopper
    app.state.database_owner = database_owner
    # Applied to the whole router rather than per route: a data endpoint
    # that forgets the tenant check is exactly the failure this prevents,
    # and there are ~163 of them.
    app.include_router(router, dependencies=[Depends(enforce_tenant)])
    app.add_middleware(
        SessionMiddleware,
        secret_key=_dashboard_session_secret(),
        session_cookie="toda_session",
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
    validate_runtime_exposure()
    uvicorn.run(
        target_app or "toda.main:app",
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


def _register_composio_webhook(brain: TodaApp) -> None:
    """Point Composio's one-per-project webhook subscription at this gateway.

    Requires TODA_PUBLIC_URL (an ngrok/cloudflared tunnel to this process)
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
        print(f"Toda gateway: Composio webhooks registered at {webhook_url}")
    except Exception as exc:
        print(f"Toda gateway: Composio webhook registration failed: {exc}")


def gateway() -> None:
    """Run the local control plane and, when configured, Slack Socket Mode.

    Builds exactly one ``TodaApp`` and shares it between the HTTP API and
    Slack Socket Mode, so the process runs one service container and one
    worker loop rather than two independent copies of each.
    """
    owner = DatabaseOwner(settings.db_path)
    owner.acquire()
    brain = reset_app(settings.db_path)
    brain.indexing.backfill()
    _register_composio_webhook(brain)
    setup_status = GatewaySetupService(
        brain.store, brain.graph, brain.audit, skills=brain.skills
    ).status()
    if not (setup_status.get("setup") or {}).get("complete"):
        bootstrap = SetupBootstrapToken(settings.data_dir).issue()
        print(
            "Toda setup: "
            f"http://127.0.0.1:{settings.port}/setup?bootstrap={bootstrap}"
        , flush=True)

    slack_stopper: Callable[[], None] | None = None
    if settings.slack_app_token:
        from toda.slack_socket import run_socket_mode

        stop_event = threading.Event()

        def _socket_runner() -> None:
            try:
                asyncio.run(run_socket_mode(brain, stop_event=stop_event))
            except Exception as exc:
                # The setup UI remains available if Slack is not ready yet.
                print(f"Toda Slack gateway unavailable: {exc}")

        socket_thread = threading.Thread(
            target=_socket_runner,
            name="toda-slack-gateway",
            daemon=True,
        )
        socket_thread.start()
        print("Toda gateway: Slack Socket Mode starting")

        def _stop_socket_mode() -> None:
            stop_event.set()
            socket_thread.join(timeout=10)

        slack_stopper = _stop_socket_mode
    else:
        print("Toda gateway: configure Slack at http://127.0.0.1:8080/setup")

    gateway_app = create_app(
        prebuilt_brain=brain,
        slack_stopper=slack_stopper,
        database_owner=owner,
    )
    try:
        serve(gateway_app)
    finally:
        # Lifespan normally releases this. Keep startup failures safe too.
        owner.release()


def retention_cli(args: argparse.Namespace) -> None:
    """Preview or apply retention cleanup, or delete an organization entirely."""
    owner = DatabaseOwner(settings.db_path)
    owner.acquire()
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
        owner.release()


def reset_cli(args: argparse.Namespace) -> None:
    """Delete every piece of local Toda state: the database (memories,
    sessions, tasks, automations, artifacts all live there) and the
    on-disk task workspaces.

    The workspace tree matters as much as the database here: it is real
    files written by tasks, it is NOT recreated from the database, and
    leaving it behind means a "fresh start" still has thousands of stale
    directories in it (a live install had 4,191 left over, all from an
    architecture that no longer exists).
    """
    db_path = settings.db_path.resolve()
    workspace_root = settings.workspace_root.resolve()
    if not args.yes:
        print(
            f"This permanently deletes all Toda data in {db_path} "
            f"and all task workspaces in {workspace_root}. "
            "Pass --yes to continue."
        )
        raise SystemExit(2)

    owner = DatabaseOwner(db_path)
    owner.acquire()
    removed: list[Path] = []
    try:
        for path in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
            Path(f"{db_path}-journal"),
        ):
            if path.exists():
                path.unlink()
                removed.append(path)
    finally:
        owner.release()
    if removed:
        print(f"Reset Toda database: {db_path}")
        print(f"Removed {len(removed)} database file(s).")
    else:
        print(f"Toda database is already clean: {db_path}")

    # Guard against a misconfigured workspace_root (e.g. pointed at "/" or a
    # home directory) turning a reset into an unrecoverable wipe: only ever
    # delete a directory that actually looks like Toda's own workspace
    # tree, and only its contents.
    if workspace_root.exists() and workspace_root.is_dir():
        if workspace_root.parent == settings.data_dir.resolve() or workspace_root.name in {
            "workspaces",
            ".toda-workspaces",
        }:
            workspace_count = 0
            for child in workspace_root.iterdir():
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(
                    missing_ok=True
                )
                workspace_count += 1
            print(
                f"Removed {workspace_count} workspace entr(ies) under {workspace_root}."
            )
        else:
            print(
                f"Skipped workspace cleanup: {workspace_root} does not look like "
                "a Toda workspace directory. Remove it manually if intended."
            )


def reindex_cli(args: argparse.Namespace) -> None:
    """Rebuild the vec0 ANN tables from the JSON embedding columns that
    remain the source of truth (storage/db.py:reindex_vectors). Needed
    after a bulk backfill, or to repopulate the indexes after an embedding
    model change rebuilt them empty."""
    owner = DatabaseOwner(settings.db_path)
    owner.acquire()
    brain = get_app(settings.db_path)
    try:
        counts = brain.store.reindex_vectors(force=args.force)
        print(
            f"Reindexed vectors: {counts['chunks']} chunk(s), "
            f"{counts['memories']} memory embedding(s), {counts['episodes']} episode summar(ies)."
        )
        if counts["skipped_wrong_width"]:
            print(
                f"Skipped {counts['skipped_wrong_width']} row(s) whose stored embedding "
                f"predates the current embedding model. Re-embed them by restarting the "
                f"gateway/worker (IndexingService.backfill runs at startup)."
            )
    finally:
        brain.close()
        owner.release()


def main() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="toda",
        description="Toda local control plane and company-agent gateway",
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
    reindex = sub.add_parser(
        "reindex",
        help="rebuild the vec0 ANN vector indexes from stored embeddings",
    )
    reindex.add_argument(
        "--force",
        action="store_true",
        help="drop and fully rebuild the vec0 tables (needed after an embedding dimension/provider change)",
    )
    service = sub.add_parser(
        "service", help="install or inspect the per-user Toda gateway service"
    )
    service.add_argument(
        "action",
        choices=["install", "start", "stop", "restart", "status", "update"],
        nargs="?",
        default="install",
    )
    service.add_argument("--no-open", action="store_true", help="do not open the setup browser")
    args = parser.parse_args()
    if args.command == "gateway":
        gateway()
    elif args.command == "reset":
        reset_cli(args)
    elif args.command == "retention":
        retention_cli(args)
    elif args.command == "reindex":
        reindex_cli(args)
    elif args.command == "service":
        service_cli(args)
    else:
        serve()


if __name__ == "__main__":
    main()
