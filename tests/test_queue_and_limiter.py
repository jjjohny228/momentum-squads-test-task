import asyncio
import time
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.drafting.models import ProviderCall
from app.drafting.rate_limiter import CallKind, PostgresRateLimiter
from app.jobs.models import JobKind
from app.jobs.repository import JobRepository
from app.tickets.repository import TicketRepository


async def make_jobs(sessionmaker: async_sessionmaker[AsyncSession], n: int) -> None:
    async with sessionmaker() as s, s.begin():
        for _ in range(n):
            tid = await TicketRepository(s).insert_if_new(
                event_id=uuid.uuid4().hex,
                external_ticket_id="T-1",
                customer_email="a@b.c",
                message="m",
                reply_url="http://x/y",
            )
            assert tid is not None
            await JobRepository(s).enqueue(JobKind.GENERATE_DRAFT, tid, 3)


async def claim(sessionmaker: async_sessionmaker[AsyncSession], lease: float = 60):  # type: ignore[no-untyped-def]
    async with sessionmaker() as s, s.begin():
        return await JobRepository(s).claim(timedelta(seconds=lease))


async def test_concurrent_claims_never_return_the_same_job(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await make_jobs(sessionmaker, 10)

    claimed = await asyncio.gather(*(claim(sessionmaker) for _ in range(15)))

    ids = [j.id for j in claimed if j is not None]
    assert len(ids) == 10
    assert len(set(ids)) == 10


async def test_expired_lease_is_reclaimed(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await make_jobs(sessionmaker, 1)
    first = await claim(sessionmaker, lease=0.1)
    assert first is not None
    assert await claim(sessionmaker) is None  # still leased

    await asyncio.sleep(0.2)  # the worker "crashed"
    again = await claim(sessionmaker)

    assert again is not None
    assert again.id == first.id
    assert again.attempts == 2


async def test_rate_limiter_caps_calls_per_window(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Two limiter instances emulate two worker processes sharing one budget
    a = PostgresRateLimiter(sessionmaker, limit=3, window_seconds=1.0)
    b = PostgresRateLimiter(sessionmaker, limit=3, window_seconds=1.0)

    started = time.monotonic()
    await asyncio.gather(*(lim.acquire(CallKind.CHAT) for lim in [a, b, a, b, a, b]))
    elapsed = time.monotonic() - started

    assert elapsed >= 0.9  # the 4th..6th call had to wait for the window to slide
    async with sessionmaker() as s:
        rows = (await s.execute(text("SELECT created_at FROM provider_calls ORDER BY 1"))).all()
    times = [r.created_at for r in rows]
    assert len(times) == 6
    # no 1-second window contains more than 3 calls
    for i in range(len(times) - 3):
        assert (times[i + 3] - times[i]).total_seconds() >= 0.95


async def test_embeddings_can_bypass_the_limiter(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    limiter = PostgresRateLimiter(sessionmaker, limit=1, include_embeddings=False)

    await limiter.acquire(CallKind.EMBEDDING)
    await limiter.acquire(CallKind.EMBEDDING)
    await limiter.acquire(CallKind.CHAT)

    async with sessionmaker() as s:
        n = await s.scalar(select(func.count()).select_from(ProviderCall))
    assert n == 1
