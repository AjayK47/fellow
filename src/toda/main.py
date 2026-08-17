"""Toda API entry point."""

from __future__ import annotations

import os
import asyncio
import secrets
import time
import argparse
import logging
import shutil
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Callable

import uvicorn
from fastapi import Depends, FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware

from toda.api.routes import enforce_tenant, router
from toda.config import config_path, settings
from toda.runtime_owner import DatabaseOwner
from toda.services.bootstrap import initialize, is_initialized
from toda.services.container import TodaApp, get_app, reset_app
from toda.observability import configure_json_logging

log = logging.getLogger("toda")


def init_cli(args: argparse.Namespace) -> None:
    """Create the organization and owner account described by config.json."""
    owner_email = settings.organization.owner_email
    if not owner_email:
        raise ValueError(
            f"Set organization.owner_email in {config_path()} before running toda init"
        )
    owner = DatabaseOwner(settings.storage.db_path)
    owner.acquire()
    try:
        brain = reset_app(settings.storage.db_path)
        result = initialize(
            brain.store,
            organization_id=settings.organization.id,
            organization_name=settings.organization.name,
            owner_email=owner_email,
            owner_name=owner_email.split("@")[0],
            password=args.password,
        )
        brain.close()
    finally:
        owner.release()

    print(f"Organization : {result['organization_id']}")
    print(f"Public scope : {result['scope_id']}")
    print(f"Owner        : {result['owner']['email']}")
    if result["generated"]:
        print(f"Password     : {result['password']}   (shown once)")
    print("\nStart the gateway with: toda gateway")
    print(f"Then sign in at http://127.0.0.1:{settings.server.port}")


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def validate_runtime_exposure() -> None:
    """Refuse explicitly unsafe combinations on a network-bound server."""
    host = settings.server.host.strip().lower()
    local_only = host in {"127.0.0.1", "::1", "localhost"}
    # Names the config.json paths, not the environment variables these used
    # to be -- Toda reads exactly one file and no environment layer, so an
    # operator who searched for the old TODA_* names found nothing at all.
    if not local_only and settings.dashboard.auth_disabled:
        raise RuntimeError(
            f'dashboard.auth_disabled cannot be true when server.host is '
            f'network-accessible (currently "{settings.server.host}"). '
            f"Set it to false in {config_path()}, or bind to 127.0.0.1."
        )
    if not local_only and settings.dashboard.auto_approve_writes:
        raise RuntimeError(
            f'dashboard.auto_approve_writes cannot be true when server.host '
            f'is network-accessible (currently "{settings.server.host}"). '
            f"Set it to false in {config_path()}, or bind to 127.0.0.1."
        )


def _dashboard_session_secret() -> str:
    """Resolve the key SessionMiddleware signs the console login cookie
    with. The installer persists a dedicated key; a generated fallback keeps
    a bare developer run usable without coupling login to Slack."""
    if settings.dashboard.session_secret:
        return settings.dashboard.session_secret
    return secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    owner = getattr(app.state, "database_owner", None)
    if owner is None:
        prebuilt_brain = getattr(app.state, "prebuilt_brain", None)
        db = os.environ.get("TODA_DB")
        owner_path = (
            prebuilt_brain.store.db_path
            if prebuilt_brain is not None
            else Path(db) if db else settings.storage.db_path
        )
        owner = DatabaseOwner(owner_path)
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
            brain = reset_app(Path(db) if db else settings.storage.db_path)
        app.state.brain = brain
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.observability.retention_days)).isoformat()
        brain.store.purge_trace_events(before=cutoff, limit=100_000)
        brain.indexing.backfill()
        # Independent lanes keep long agent work and ingestion from
        # delaying infrastructure jobs, while all lanes remain inside
        # the single database-owning process.
        async def _lane(kinds: list[str], *, tick_automations: bool) -> None:
            last_dispatcher_tick = 0.0
            last_recovery_tick = 0.0
            while True:
                if tick_automations and (
                    time.monotonic() - last_dispatcher_tick
                    >= settings.worker.dispatcher_tick_interval_seconds
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
                    await asyncio.sleep(settings.worker.poll_interval_seconds)
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
        max_age=14 * 24 * 60 * 60,
        https_only=settings.dashboard.cookie_secure,
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
                brain.audit.record(settings.organization.id, "http.request", {"request_id": request_id, "path": request.url.path, "method": request.method, "status_code": 500, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
            raise
        response.headers["X-Request-ID"] = request_id
        brain = getattr(request.app.state, "brain", None)
        # A successful read is not an auditable act. Recording every one of
        # them put the console's own 8-second polling into the audit trail
        # -- roughly 900 rows an hour from a single idle browser tab, in the
        # same SQLite file as memory, embeddings and the job queue -- which
        # diluted the record that actually matters: who changed what, and
        # anything that failed. Mutations and non-2xx responses are still
        # recorded in full, including the 401/403 an intrusion attempt
        # produces.
        auditable = request.method not in _READ_METHODS or response.status_code >= 400
        if brain and not suppress_audit and auditable:
            brain.audit.record(settings.organization.id, "http.request", {"request_id": request_id, "path": request.url.path, "method": request.method, "status_code": response.status_code, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
        return response
    return app


app = create_app()


def serve(target_app: FastAPI | str | None = None) -> None:
    validate_runtime_exposure()
    uvicorn.run(
        target_app or "toda.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=False,
        log_level=settings.server.log_level,
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
    if not (brain.composio.configured and settings.server.public_url):
        return
    webhook_url = f"{settings.server.public_url.rstrip('/')}/v1/composio/webhooks"
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
    owner = DatabaseOwner(settings.storage.db_path)
    owner.acquire()
    brain = reset_app(settings.storage.db_path)
    brain.indexing.backfill()
    _register_composio_webhook(brain)
    if not is_initialized(brain.store, organization_id=settings.organization.id):
        owner.release()
        raise SystemExit(
            "This installation has not been initialised.\n"
            f"Edit {config_path()}, then run: toda init"
        )

    slack_stopper: Callable[[], None] | None = None
    if settings.slack.app_token:
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
    owner = DatabaseOwner(settings.storage.db_path)
    owner.acquire()
    brain = get_app(settings.storage.db_path)
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
    db_path = settings.storage.db_path.resolve()
    workspace_root = settings.storage.workspaces.resolve()
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
        if workspace_root.parent == settings.storage.data_dir.resolve() or workspace_root.name in {
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
    owner = DatabaseOwner(settings.storage.db_path)
    owner.acquire()
    brain = get_app(settings.storage.db_path)
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
    configure_json_logging(settings.server.log_level)
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
    init = sub.add_parser(
        "init",
        help="create the organization and owner account described by config.json",
    )
    init.add_argument(
        "--password",
        default=None,
        help="owner password (a strong one is generated and printed when omitted)",
    )
    args = parser.parse_args()
    if args.command == "gateway":
        gateway()
    elif args.command == "reset":
        reset_cli(args)
    elif args.command == "retention":
        retention_cli(args)
    elif args.command == "reindex":
        reindex_cli(args)
    elif args.command == "init":
        try:
            init_cli(args)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from None
    else:
        serve()


if __name__ == "__main__":
    main()
