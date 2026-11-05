from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drafting.models import ProviderCall


class ProviderCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_last(self, seconds: float) -> int:
        since = func.clock_timestamp() - func.make_interval(0, 0, 0, 0, 0, 0, seconds)
        n = await self.session.scalar(
            select(func.count()).select_from(ProviderCall).where(ProviderCall.created_at > since)
        )
        return int(n or 0)
