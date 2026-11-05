import asyncio
import random
import zlib
from enum import StrEnum
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger

log = get_logger(__name__)


class CallKind(StrEnum):
    CHAT = "chat"
    EMBEDDING = "embedding"


class RateLimiter(Protocol):
    async def acquire(self, kind: CallKind) -> None: ...


class PostgresRateLimiter:
    """Sliding-window limiter shared by every process that talks to the provider.

    State lives in Postgres (`provider_calls`), and a transaction-scoped advisory lock serializes
    the check-and-record step, so N worker replicas together never exceed `limit` per window.
    """

    BUCKET = "openai"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        limit: int,
        window_seconds: float = 60.0,
        include_embeddings: bool = True,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.limit = limit
        self.window_seconds = window_seconds
        self.include_embeddings = include_embeddings
        self._lock_key = zlib.crc32(self.BUCKET.encode())

    async def acquire(self, kind: CallKind) -> None:
        if kind is CallKind.EMBEDDING and not self.include_embeddings:
            return
        while True:
            wait = await self._try_acquire(kind)
            if wait is None:
                return
            log.info("rate_limited", kind=kind.value, wait_seconds=round(wait, 2))
            # Jitter spreads waiters out so they do not all hit the lock at the same instant
            await asyncio.sleep(wait + random.uniform(0, 0.25))  # noqa: S311

    async def _try_acquire(self, kind: CallKind) -> float | None:
        """Record a call and return None, or return how long to wait before the window frees up."""
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": self._lock_key}
            )
            row = (
                await session.execute(
                    text(
                        """
                        SELECT count(*) AS n,
                               min(created_at) AS oldest,
                               clock_timestamp() AS now
                        FROM provider_calls
                        WHERE bucket = :bucket
                          AND created_at > clock_timestamp() - make_interval(secs => :window)
                        """
                    ),
                    {"bucket": self.BUCKET, "window": self.window_seconds},
                )
            ).one()
            if row.n < self.limit:
                await session.execute(
                    text("INSERT INTO provider_calls (bucket, kind) VALUES (:bucket, :kind)"),
                    {"bucket": self.BUCKET, "kind": kind.value},
                )
                await session.execute(
                    text(
                        "DELETE FROM provider_calls WHERE bucket = :bucket "
                        "AND created_at < clock_timestamp() - interval '1 hour'"
                    ),
                    {"bucket": self.BUCKET},
                )
                return None
            elapsed = (row.now - row.oldest).total_seconds()
            return float(max(self.window_seconds - elapsed, 0.05))


class NoopRateLimiter:
    async def acquire(self, kind: CallKind) -> None:
        return None
