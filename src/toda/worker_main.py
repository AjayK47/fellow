"""Standalone durable worker process for production-style deployments."""

from __future__ import annotations

import asyncio
import signal
import time

from toda.config import settings
from toda.services.container import TodaApp


async def run_worker() -> None:
    brain = TodaApp(settings.db_path)
    brain.indexing.backfill()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            loop.add_signal_handler(getattr(signal, name), stopping.set)
    # Same two-lane split as the in-process gateway worker (main.py): a
    # long agent run must never block fast infrastructure jobs behind it,
    # since drain_once() waits for its whole leased batch.
    async def _lane(kinds: list[str], *, tick_automations: bool) -> None:
        last_dispatcher_tick = 0.0
        while not stopping.is_set():
            if tick_automations and (
                time.monotonic() - last_dispatcher_tick
                >= settings.dispatcher_tick_interval_seconds
            ):
                brain.automations.tick()
                last_dispatcher_tick = time.monotonic()
            result = await asyncio.to_thread(
                brain.harness_worker.pump, kinds=kinds
            )
            if not result["leased"]:
                try:
                    await asyncio.wait_for(
                        stopping.wait(),
                        timeout=settings.worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass

    try:
        await asyncio.gather(
            _lane(
                brain.harness_worker.infrastructure_job_kinds(),
                tick_automations=True,
            ),
            _lane(brain.harness_worker.agent_job_kinds(), tick_automations=False),
            # Third lane: chunking/embedding/extraction/episode work. Kept
            # off the other two pools entirely (harness/worker.py's
            # knowledge pool) so ingest volume can't delay event routing or
            # agent dispatch.
            _lane(brain.harness_worker.knowledge_job_kinds(), tick_automations=False),
        )
    finally:
        brain.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
