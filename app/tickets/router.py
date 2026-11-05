import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import require_operator
from app.tickets.dependencies import TicketServiceDep
from app.tickets.models import TicketStatus
from app.tickets.schemas import ApproveIn, TicketOut, TicketPage

router = APIRouter(
    prefix="/api/v1/tickets", tags=["tickets"], dependencies=[Depends(require_operator)]
)


@router.get("")
async def list_tickets(
    service: TicketServiceDep,
    status: TicketStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketPage:
    items, total = await service.list_page(status=status, limit=limit, offset=offset)
    return TicketPage(
        items=[TicketOut.model_validate(t) for t in items], total=total, limit=limit, offset=offset
    )


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: uuid.UUID, service: TicketServiceDep) -> TicketOut:
    return TicketOut.model_validate(await service.get(ticket_id))


@router.post("/{ticket_id}/approve")
async def approve_ticket(
    ticket_id: uuid.UUID, service: TicketServiceDep, body: ApproveIn | None = None
) -> TicketOut:
    """Approve the draft (optionally edited). The reply is sent asynchronously, exactly once."""
    ticket = await service.approve(ticket_id, body.message if body else None)
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/reject")
async def reject_ticket(ticket_id: uuid.UUID, service: TicketServiceDep) -> TicketOut:
    return TicketOut.model_validate(await service.reject(ticket_id))


@router.post("/{ticket_id}/retry")
async def retry_ticket(ticket_id: uuid.UUID, service: TicketServiceDep) -> TicketOut:
    """Re-run a failed ticket: sending if a reply was approved, otherwise draft generation."""
    return TicketOut.model_validate(await service.retry(ticket_id))
