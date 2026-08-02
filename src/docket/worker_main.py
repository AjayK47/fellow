"""Standalone durable worker process for production-style deployments."""

from __future__ import annotations

import asyncio
import signal

from docket.config import settings
from docket.fixtures.seed import seed_synthetic_org
from docket.services.container import DocketApp


async def run_worker() -> None:
    brain = DocketApp(settings.db_path)
    if not brain.store.get_organization(settings.default_org_id):
        seed_synthetic_org(brain)
    brain.indexing.backfill()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            loop.add_signal_handler(getattr(signal, name), stopping.set)
    try:
        while not stopping.is_set():
            brain.automations.tick()
            result = await asyncio.to_thread(brain.harness_worker.drain_once)
            if not result["leased"]:
                try:
                    await asyncio.wait_for(
                        stopping.wait(),
                        timeout=settings.worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        brain.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
