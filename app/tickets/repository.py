import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.tickets.models import Ticket, TicketStatus


class TicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_if_new(
        self,
        *,
        event_id: str,
        external_ticket_id: str,
        customer_email: str,
        message: str,
        reply_url: str,
    ) -> uuid.UUID | None:
        """Insert a ticket unless the event was already seen. Returns None for duplicates.

        The unique constraint on event_id resolves concurrent deliveries of the same event.
        """
        stmt = (
            insert(Ticket)
            .values(
                id=uuid.uuid4(),
                event_id=event_id,
                external_ticket_id=external_ticket_id,
                customer_email=customer_email,
                message=message,
                reply_url=reply_url,
                status=TicketStatus.RECEIVED,
                sources=[],
                needs_manual_reply=False,
            )
            .on_conflict_do_nothing(index_elements=[Ticket.event_id])
            .returning(Ticket.id)
        )
        return await self.session.scalar(stmt)

    async def get(self, ticket_id: uuid.UUID) -> Ticket | None:
        return await self.session.get(Ticket, ticket_id, populate_existing=True)

    async def get_by_event_id(self, event_id: str) -> Ticket | None:
        return await self.session.scalar(select(Ticket).where(Ticket.event_id == event_id))

    async def list_page(
        self, *, status: TicketStatus | None, limit: int, offset: int
    ) -> tuple[list[Ticket], int]:
        query = select(Ticket)
        count = select(func.count()).select_from(Ticket)
        if status is not None:
            query = query.where(Ticket.status == status)
            count = count.where(Ticket.status == status)
        rows = await self.session.scalars(
            query.order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.session.scalar(count)
        return list(rows), total or 0

    async def count_by_status(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(Ticket.status, func.count()).group_by(Ticket.status)
        )
        return {str(status): int(n) for status, n in rows}

    async def transition(
        self,
        ticket_id: uuid.UUID,
        *,
        from_statuses: Iterable[TicketStatus],
        to: TicketStatus,
        **values: Any,
    ) -> Ticket | None:
        """Compare-and-set status change; returns None when the ticket is not in `from_statuses`."""
        stmt = (
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status.in_(list(from_statuses)))
            .values(status=to, updated_at=func.now(), **values)
            .returning(Ticket)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return await self.session.scalar(stmt)
