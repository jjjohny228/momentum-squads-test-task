"""Operator UI: thin HTML/HTMX adapter over the same TicketService used by the JSON API."""

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.dependencies import require_operator
from app.core.exceptions import DomainError
from app.tickets.dependencies import TicketServiceDep
from app.tickets.models import Ticket, TicketStatus

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_operator)])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

STATUS_FILTERS = [None, *TicketStatus]


@router.get("/", response_class=HTMLResponse)
async def index() -> RedirectResponse:
    return RedirectResponse("/ui/tickets")


@router.get("/ui/tickets", response_class=HTMLResponse)
async def tickets_page(
    request: Request, service: TicketServiceDep, status: TicketStatus | None = None
) -> HTMLResponse:
    items, total = await service.list_page(status=status, limit=100)
    template = "_ticket_rows.html" if request.headers.get("HX-Request") else "tickets.html"
    return templates.TemplateResponse(
        request,
        template,
        {"tickets": items, "total": total, "status": status, "filters": STATUS_FILTERS},
    )


@router.get("/ui/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_page(
    request: Request, ticket_id: uuid.UUID, service: TicketServiceDep
) -> HTMLResponse:
    ticket = await service.get(ticket_id)
    template = "_ticket_card.html" if request.headers.get("HX-Request") else "ticket.html"
    return templates.TemplateResponse(request, template, {"ticket": ticket, "error": None})


@router.post("/ui/tickets/{ticket_id}/{action}", response_class=HTMLResponse)
async def ticket_action(
    request: Request,
    ticket_id: uuid.UUID,
    action: str,
    service: TicketServiceDep,
    message: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    error: str | None = None
    ticket: Ticket | None = None
    try:
        if action == "approve":
            ticket = await service.approve(ticket_id, message)
        elif action == "reject":
            ticket = await service.reject(ticket_id)
        elif action == "retry":
            ticket = await service.retry(ticket_id)
        else:
            error = f"Unknown action '{action}'"
    except DomainError as exc:
        error = exc.detail
    if ticket is None:
        ticket = await service.get(ticket_id)
    return templates.TemplateResponse(
        request, "_ticket_card.html", {"ticket": ticket, "error": error}
    )
