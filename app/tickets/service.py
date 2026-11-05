import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.tracing import thread_metadata, ticket_trace
from app.drafting.repository import ProviderCallRepository
from app.jobs.models import JobKind
from app.jobs.repository import JobRepository
from app.tickets.exceptions import InvalidTransitionError, TicketNotFoundError
from app.tickets.models import DraftSource, Ticket, TicketStatus
from app.tickets.repository import TicketRepository
from app.tickets.schemas import HelpdeskWebhookIn, SourceRef

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IntakeResult:
    created: bool
    ticket_id: uuid.UUID | None


async def _trace_operator_action(
    name: str,
    ticket: Ticket,
    *,
    inputs: dict[str, Any],
    outputs: dict[str, Any] | None = None,
) -> None:
    """Puts the operator's decision into the ticket's LangSmith thread (no-op when disabled)."""
    metadata = thread_metadata(
        external_ticket_id=ticket.external_ticket_id, ticket_id=ticket.id, event_id=ticket.event_id
    )
    async with ticket_trace(name, metadata=metadata, inputs=inputs, tags=["operator"]) as run:
        if run is not None:
            run.add_outputs({"status": str(ticket.status), **(outputs or {})})


class TicketService:
    """Ticket lifecycle. Every public method owns exactly one transaction."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.tickets = TicketRepository(session)
        self.jobs = JobRepository(session)

    # --- intake -------------------------------------------------------------------------------

    async def intake(self, payload: HelpdeskWebhookIn) -> IntakeResult:
        async with self.session.begin():
            ticket_id = await self.tickets.insert_if_new(
                event_id=payload.event_id,
                external_ticket_id=payload.ticket_id,
                customer_email=str(payload.customer_email),
                message=payload.message,
                reply_url=str(payload.reply_url),
            )
            if ticket_id is None:
                log.info("webhook_duplicate", event_id=payload.event_id)
                return IntakeResult(created=False, ticket_id=None)
            await self.jobs.enqueue(
                JobKind.GENERATE_DRAFT, ticket_id, self.settings.job_max_attempts
            )
        log.info("ticket_received", event_id=payload.event_id, ticket_id=str(ticket_id))
        return IntakeResult(created=True, ticket_id=ticket_id)

    # --- reads --------------------------------------------------------------------------------

    async def get(self, ticket_id: uuid.UUID) -> Ticket:
        async with self.session.begin():
            ticket = await self.tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

    async def list_page(
        self, *, status: TicketStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[Ticket], int]:
        async with self.session.begin():
            return await self.tickets.list_page(status=status, limit=limit, offset=offset)

    async def stats(self) -> tuple[dict[str, int], int]:
        async with self.session.begin():
            by_status = await self.tickets.count_by_status()
            calls = await ProviderCallRepository(self.session).count_last(60)
        return by_status, calls

    # --- operator actions ---------------------------------------------------------------------

    async def approve(self, ticket_id: uuid.UUID, message: str | None = None) -> Ticket:
        async with self.session.begin():
            current = await self.tickets.get(ticket_id)
            if current is None:
                raise TicketNotFoundError(ticket_id)
            reply = message.strip() if message and message.strip() else current.draft
            if not reply:
                raise InvalidTransitionError(ticket_id, current.status, "approve (empty reply)")
            ticket = await self.tickets.transition(
                ticket_id,
                from_statuses=[TicketStatus.DRAFT_READY],
                to=TicketStatus.APPROVED,
                final_reply=reply,
                approved_at=func.now(),
                last_error=None,
            )
            if ticket is None:
                # Lost the race (double click, second tab) or wrong state: nothing is enqueued
                raise InvalidTransitionError(ticket_id, current.status, "approve")
            await self.jobs.enqueue(JobKind.SEND_REPLY, ticket_id, self.settings.job_max_attempts)
        log.info("ticket_approved", ticket_id=str(ticket_id), edited=message is not None)
        await _trace_operator_action(
            "operator_approve",
            ticket,
            inputs={"draft": current.draft, "edited": bool(message and message.strip())},
            outputs={"final_reply": ticket.final_reply},
        )
        return ticket

    async def reject(self, ticket_id: uuid.UUID) -> Ticket:
        async with self.session.begin():
            ticket = await self.tickets.transition(
                ticket_id,
                from_statuses=[TicketStatus.DRAFT_READY, TicketStatus.FAILED],
                to=TicketStatus.REJECTED,
            )
            if ticket is None:
                raise await self._transition_error(ticket_id, "reject")
        log.info("ticket_rejected", ticket_id=str(ticket_id))
        await _trace_operator_action("operator_reject", ticket, inputs={"draft": ticket.draft})
        return ticket

    async def retry(self, ticket_id: uuid.UUID) -> Ticket:
        """Re-run whatever failed: sending if a reply was approved, otherwise draft generation."""
        async with self.session.begin():
            current = await self.tickets.get(ticket_id)
            if current is None:
                raise TicketNotFoundError(ticket_id)
            if current.final_reply:
                to, kind = TicketStatus.APPROVED, JobKind.SEND_REPLY
            else:
                to, kind = TicketStatus.RECEIVED, JobKind.GENERATE_DRAFT
            ticket = await self.tickets.transition(
                ticket_id, from_statuses=[TicketStatus.FAILED], to=to, last_error=None
            )
            if ticket is None:
                raise InvalidTransitionError(ticket_id, current.status, "retry")
            await self.jobs.enqueue(kind, ticket_id, self.settings.job_max_attempts)
        log.info("ticket_retry", ticket_id=str(ticket_id), job=kind.value)
        await _trace_operator_action(
            "operator_retry",
            ticket,
            inputs={"previous_error": current.last_error, "job": kind.value},
        )
        return ticket

    # --- worker-side transitions --------------------------------------------------------------

    async def start_generation(self, ticket_id: uuid.UUID) -> Ticket | None:
        """Returns None when the ticket no longer needs a draft (idempotent job re-runs)."""
        async with self.session.begin():
            return await self.tickets.transition(
                ticket_id,
                from_statuses=[TicketStatus.RECEIVED, TicketStatus.GENERATING],
                to=TicketStatus.GENERATING,
            )

    async def save_draft(
        self,
        ticket_id: uuid.UUID,
        *,
        draft: str,
        source: DraftSource,
        sources: list[SourceRef],
        needs_manual_reply: bool,
    ) -> Ticket | None:
        async with self.session.begin():
            return await self.tickets.transition(
                ticket_id,
                from_statuses=[TicketStatus.GENERATING],
                to=TicketStatus.DRAFT_READY,
                draft=draft,
                draft_source=source,
                sources=[s.model_dump() for s in sources],
                needs_manual_reply=needs_manual_reply,
                last_error=None,
            )

    async def start_sending(self, ticket_id: uuid.UUID) -> Ticket | None:
        async with self.session.begin():
            return await self.tickets.transition(
                ticket_id,
                from_statuses=[TicketStatus.APPROVED, TicketStatus.SENDING],
                to=TicketStatus.SENDING,
            )

    async def mark_sent(self, ticket_id: uuid.UUID) -> None:
        async with self.session.begin():
            await self.tickets.transition(
                ticket_id,
                from_statuses=[TicketStatus.SENDING],
                to=TicketStatus.SENT,
                sent_at=func.now(),
                last_error=None,
            )

    async def record_error(self, ticket_id: uuid.UUID, error: str) -> None:
        """Keeps the current status, only surfaces the latest transient error to the operator."""
        async with self.session.begin():
            current = await self.tickets.get(ticket_id)
            if current is not None:
                current.last_error = error

    async def mark_failed(self, ticket_id: uuid.UUID, error: str) -> None:
        async with self.session.begin():
            await self.tickets.transition(
                ticket_id,
                from_statuses=[
                    TicketStatus.RECEIVED,
                    TicketStatus.GENERATING,
                    TicketStatus.APPROVED,
                    TicketStatus.SENDING,
                ],
                to=TicketStatus.FAILED,
                last_error=error,
            )

    async def _transition_error(
        self, ticket_id: uuid.UUID, action: str
    ) -> TicketNotFoundError | InvalidTransitionError:
        current = await self.tickets.get(ticket_id)
        if current is None:
            return TicketNotFoundError(ticket_id)
        return InvalidTransitionError(ticket_id, current.status, action)
