"""Company Brain API entry point."""

from __future__ import annotations

import os
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

from company_brain.api.routes import router
from company_brain.config import settings
from company_brain.services.container import BrainApp, get_app, reset_app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db = os.environ.get("COMPANY_BRAIN_DB")
    brain = reset_app(Path(db) if db else settings.db_path)
    app.state.brain = brain
    # Auto-seed if empty
    if not brain.store.get_organization(settings.default_org_id):
        from company_brain.fixtures.seed import seed_synthetic_org

        seed_synthetic_org(brain)
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
            _worker_loop(), name="company-brain-harness-worker"
        )
    yield
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    brain.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Company Brain",
        description="Slack-native company-wide memory and agent harness",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)

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


def main() -> None:
    uvicorn.run(
        "company_brain.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
