import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl

from app.tickets.models import DraftSource, TicketStatus


class HelpdeskWebhookIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=255, examples=["evt_8f2a"])
    ticket_id: str = Field(min_length=1, max_length=255, examples=["T-1042"])
    customer_email: EmailStr = Field(examples=["user@example.com"])
    message: str = Field(
        min_length=1, max_length=20_000, examples=["Не можу експортувати звіт у PDF"]
    )
    reply_url: HttpUrl = Field(examples=["https://helpdesk.example.com/tickets/T-1042/replies"])


class WebhookAck(BaseModel):
    status: Literal["accepted", "duplicate"]
    ticket_id: uuid.UUID | None = None


class SourceRef(BaseModel):
    slug: str
    title: str
    url: str
    score: float | None = None


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: str
    external_ticket_id: str
    customer_email: str
    message: str
    status: TicketStatus
    draft: str | None
    draft_source: DraftSource | None
    sources: list[SourceRef]
    needs_manual_reply: bool
    final_reply: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    sent_at: datetime | None


class TicketPage(BaseModel):
    items: list[TicketOut]
    total: int
    limit: int
    offset: int


class ApproveIn(BaseModel):
    message: str | None = Field(
        default=None,
        min_length=1,
        max_length=20_000,
        description="Edited reply text; the generated draft is sent when omitted",
    )
