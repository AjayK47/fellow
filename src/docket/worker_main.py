"""Standalone durable worker process for production-style deployments."""

from __future__ import annotations

import asyncio
import signal
import time

from docket.config import settings
from docket.services.container import DocketApp


async def run_worker() -> None:
    brain = DocketApp(settings.db_path)
    brain.indexing.backfill()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            loop.add_signal_handler(getattr(signal, name), stopping.set)
    last_dispatcher_tick = 0.0
    try:
        while not stopping.is_set():
            if (
                time.monotonic() - last_dispatcher_tick
                >= settings.dispatcher_tick_interval_seconds
            ):
                brain.automations.tick()
                last_dispatcher_tick = time.monotonic()
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
