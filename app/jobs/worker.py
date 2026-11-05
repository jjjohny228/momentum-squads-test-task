import asyncio
import contextlib
import random
from datetime import timedelta

import psycopg
from sqlalchemy.engine import make_url

from app.core.logging import get_logger
from app.jobs.handlers import HANDLERS, PERMANENT_ERRORS, Handler, JobContext
from app.jobs.models import JOBS_CHANNEL, JobKind
from app.jobs.repository import ClaimedJob, JobRepository

log = get_logger(__name__)


def to_libpq_dsn(sqlalchemy_url: str) -> str:
    return (
        make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


class Worker:
    """Runs up to `concurrency` jobs at once (LLM calls are I/O-bound, so asyncio is enough).

    Wakes up on Postgres NOTIFY when a job is enqueued and also polls as a fallback.
    """

    def __init__(
        self,
        ctx: JobContext,
        *,
        handlers: dict[JobKind, Handler] | None = None,
        shutdown_timeout: float = 35.0,
    ) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.handlers = handlers or HANDLERS
        self.lease = timedelta(seconds=self.settings.job_lease_seconds)
        self.shutdown_timeout = shutdown_timeout
        self._wake = asyncio.Event()
        self._running: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        semaphore = asyncio.Semaphore(self.settings.worker_concurrency)
        listener = asyncio.create_task(self._listen(stop))
        log.info("worker_started", concurrency=self.settings.worker_concurrency)
        try:
            while not stop.is_set():
                await semaphore.acquire()
                self._wake.clear()
                try:
                    job = await self._claim()
                except Exception:
                    log.exception("claim_failed")
                    job = None
                if job is None:
                    semaphore.release()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._wake.wait(), self.settings.worker_poll_interval
                        )
                    continue
                task = asyncio.create_task(self._execute(job))
                self._running.add(task)
                task.add_done_callback(self._running.discard)
                task.add_done_callback(lambda _: semaphore.release())
        finally:
            listener.cancel()
            await self._drain()
            log.info("worker_stopped")

    async def run_until_idle(self) -> None:
        """Process jobs until none is due (used by tests)."""
        while job := await self._claim():
            await self._execute(job)

    async def _drain(self) -> None:
        if not self._running:
            return
        log.info("worker_draining", jobs=len(self._running))
        _, pending = await asyncio.wait(self._running, timeout=self.shutdown_timeout)
        for task in pending:  # their leases expire and another worker picks them up
            task.cancel()

    async def _claim(self) -> ClaimedJob | None:
        async with self.ctx.sessionmaker() as session, session.begin():
            return await JobRepository(session).claim(self.lease)

    async def _execute(self, job: ClaimedJob) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        log_ctx = {
            "job_id": job.id,
            "kind": job.kind.value,
            "ticket_id": str(job.ticket_id),
            "attempt": job.attempts,
        }
        try:
            await self.handlers[job.kind](self.ctx, job)
        except PERMANENT_ERRORS as exc:
            log.error("job_failed_permanently", error=repr(exc), **log_ctx)
            await self._finish_failed(job, repr(exc))
        except Exception as exc:
            if job.attempts >= job.max_attempts:
                log.error("job_failed_max_attempts", error=repr(exc), **log_ctx)
                await self._finish_failed(job, repr(exc))
            else:
                delay = self._backoff(job.attempts)
                log.warning("job_retry_scheduled", error=repr(exc), delay=delay, **log_ctx)
                async with self.ctx.sessionmaker() as session:
                    async with session.begin():
                        await JobRepository(session).retry_later(
                            job.id, timedelta(seconds=delay), repr(exc)
                        )
                    await self.ctx.tickets(session).record_error(job.ticket_id, repr(exc))
        else:
            async with self.ctx.sessionmaker() as session, session.begin():
                await JobRepository(session).complete(job.id)
        finally:
            heartbeat.cancel()

    async def _finish_failed(self, job: ClaimedJob, error: str) -> None:
        async with self.ctx.sessionmaker() as session:
            async with session.begin():
                await JobRepository(session).fail(job.id, error)
            await self.ctx.tickets(session).mark_failed(job.ticket_id, error)

    def _backoff(self, attempt: int) -> float:
        base = self.settings.job_backoff_base_seconds * 2 ** (attempt - 1)
        jitter = random.uniform(0.8, 1.2)  # noqa: S311
        return float(min(base, self.settings.job_backoff_max_seconds) * jitter)

    async def _heartbeat(self, job_id: int) -> None:
        """Keeps the lease alive for long jobs so they are not picked up twice."""
        interval = self.lease.total_seconds() / 3
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.ctx.sessionmaker() as session, session.begin():
                    await JobRepository(session).extend_lease(job_id, self.lease)
            except Exception:
                log.exception("lease_extend_failed", job_id=job_id)

    async def _listen(self, stop: asyncio.Event) -> None:
        dsn = to_libpq_dsn(self.settings.database_url)
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {JOBS_CHANNEL}")
                    async for _ in conn.notifies():
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("listen_connection_lost", exc_info=True)
                await asyncio.sleep(5)
