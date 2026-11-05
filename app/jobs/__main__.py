"""Worker entrypoint: `python -m app.jobs`."""

import asyncio
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.tracing import configure_tracing, flush_traces
from app.jobs.bootstrap import job_context
from app.jobs.worker import Worker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log = get_logger("app.jobs")
    configure_tracing(settings)
    if not settings.has_openai_key:
        log.warning("openai_key_missing", mode="keyless: static drafts with simulated latency")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        async with job_context(settings) as ctx:
            await Worker(ctx).run(stop)
    finally:
        flush_traces()


if __name__ == "__main__":
    asyncio.run(main())
