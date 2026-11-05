import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class TicketStatus(StrEnum):
    RECEIVED = "received"
    GENERATING = "generating"
    DRAFT_READY = "draft_ready"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    REJECTED = "rejected"
    FAILED = "failed"


class DraftSource(StrEnum):
    LLM = "llm"
    FALLBACK = "fallback"


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    external_ticket_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    reply_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        String(32), nullable=False, default=TicketStatus.RECEIVED
    )

    draft: Mapped[str | None] = mapped_column(Text)
    draft_source: Mapped[DraftSource | None] = mapped_column(String(16))
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    needs_manual_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    final_reply: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_tickets_status_created_at", "status", "created_at"),)
