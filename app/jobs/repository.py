import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.models import JOBS_CHANNEL, Job, JobKind, JobStatus


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: int
    kind: JobKind
    ticket_id: uuid.UUID
    attempts: int
    max_attempts: int


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(self, kind: JobKind, ticket_id: uuid.UUID, max_attempts: int) -> int:
        result = await self.session.execute(
            insert(Job)
            .values(kind=kind, ticket_id=ticket_id, max_attempts=max_attempts)
            .returning(Job.id)
        )
        job_id = result.scalar_one()
        # Delivered to LISTENers only when the surrounding transaction commits
        await self.session.execute(select(func.pg_notify(JOBS_CHANNEL, kind.value)))
        return int(job_id)

    async def claim(self, lease: timedelta) -> ClaimedJob | None:
        """Atomically take one due job. Expired leases (crashed worker) are claimable again."""
        row = (
            await self.session.execute(
                text(
                    """
                    UPDATE jobs SET
                        status = 'running',
                        attempts = attempts + 1,
                        locked_until = clock_timestamp() + :lease,
                        updated_at = clock_timestamp()
                    WHERE id = (
                        SELECT id FROM jobs
                        WHERE (status = 'queued' AND run_after <= clock_timestamp())
                           OR (status = 'running' AND locked_until < clock_timestamp())
                        ORDER BY run_after
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING id, kind, ticket_id, attempts, max_attempts
                    """
                ),
                {"lease": lease},
            )
        ).one_or_none()
        if row is None:
            return None
        return ClaimedJob(
            id=row.id,
            kind=JobKind(row.kind),
            ticket_id=row.ticket_id,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
        )

    async def extend_lease(self, job_id: int, lease: timedelta) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
            .values(locked_until=func.clock_timestamp() + lease)
        )

    async def complete(self, job_id: int) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=JobStatus.DONE, locked_until=None, last_error=None)
        )

    async def retry_later(self, job_id: int, delay: timedelta, error: str) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.QUEUED,
                run_after=func.clock_timestamp() + delay,
                locked_until=None,
                last_error=error,
            )
        )

    async def fail(self, job_id: int, error: str) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=JobStatus.FAILED, locked_until=None, last_error=error)
        )
