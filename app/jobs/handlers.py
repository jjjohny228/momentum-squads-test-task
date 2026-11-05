from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import openai
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.tracing import thread_metadata, ticket_trace
from app.drafting.llm import DraftGenerator
from app.helpdesk.client import HelpdeskClient, PermanentHelpdeskError
from app.jobs.models import JobKind
from app.jobs.repository import ClaimedJob
from app.knowledge.retrieval import Retriever
from app.tickets.models import Ticket
from app.tickets.service import TicketService

log = get_logger(__name__)


class InvalidJobStateError(Exception):
    """The ticket data contradicts the job (bug or manual DB edit); retrying will not help."""


# Errors that will not go away on retry: the job fails immediately
PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    PermanentHelpdeskError,
    InvalidJobStateError,
    openai.BadRequestError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
)


@dataclass(frozen=True, slots=True)
class JobContext:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    retriever: Retriever
    drafter: DraftGenerator
    helpdesk: HelpdeskClient

    def tickets(self, session: AsyncSession) -> TicketService:
        return TicketService(session, self.settings)


Handler = Callable[[JobContext, ClaimedJob], Awaitable[None]]


def _job_metadata(ticket: Ticket, job: ClaimedJob, **extra: Any) -> dict[str, Any]:
    return thread_metadata(
        external_ticket_id=ticket.external_ticket_id,
        ticket_id=ticket.id,
        event_id=ticket.event_id,
        job_id=job.id,
        attempt=job.attempts,
        **extra,
    )


def reply_idempotency_key(ticket_id: object) -> str:
    return f"reply-{ticket_id}"


async def generate_draft(ctx: JobContext, job: ClaimedJob) -> None:
    async with ctx.sessionmaker() as session:
        service = ctx.tickets(session)
        ticket = await service.start_generation(job.ticket_id)
        if ticket is None:
            log.info("generate_draft_skipped", ticket_id=str(job.ticket_id))
            return
        # The LLM call takes 5-30 s; no transaction / connection is held meanwhile
        async with ticket_trace(
            "generate_draft",
            metadata=_job_metadata(ticket, job),
            inputs={"message": ticket.message},
            tags=[f"job:{job.kind.value}"],
        ) as run:
            retrieval = await ctx.retriever.retrieve(ticket.message)
            draft = await ctx.drafter.generate(ticket.message, retrieval)
            if run is not None:
                run.add_outputs(
                    {
                        "draft": draft.text,
                        "source": draft.source.value,
                        "retrieval": draft.retrieval.value if draft.retrieval else None,
                        "articles": [s.slug for s in draft.sources],
                        "needs_manual_reply": draft.needs_manual_reply,
                    }
                )
        await service.save_draft(
            job.ticket_id,
            draft=draft.text,
            source=draft.source,
            sources=draft.sources,
            needs_manual_reply=draft.needs_manual_reply,
        )
    log.info(
        "draft_ready",
        ticket_id=str(job.ticket_id),
        source=draft.source.value,
        retrieval=draft.retrieval.value if draft.retrieval else None,
        articles=[s.slug for s in draft.sources],
    )


async def send_reply(ctx: JobContext, job: ClaimedJob) -> None:
    async with ctx.sessionmaker() as session:
        service = ctx.tickets(session)
        ticket = await service.start_sending(job.ticket_id)
        if ticket is None:
            # Already sent (or rejected): never send twice
            log.info("send_reply_skipped", ticket_id=str(job.ticket_id))
            return
        if not ticket.final_reply:
            raise InvalidJobStateError(f"ticket {ticket.id} has no approved reply")
        key = reply_idempotency_key(ticket.id)
        async with ticket_trace(
            "send_reply",
            metadata=_job_metadata(ticket, job, idempotency_key=key),
            inputs={"reply_url": ticket.reply_url, "message": ticket.final_reply},
            tags=[f"job:{job.kind.value}"],
        ) as run:
            await ctx.helpdesk.send_reply(
                reply_url=ticket.reply_url,
                ticket_id=ticket.external_ticket_id,
                message=ticket.final_reply,
                idempotency_key=key,
            )
            if run is not None:
                run.add_outputs({"status": "sent"})
        await service.mark_sent(job.ticket_id)
    log.info("reply_sent", ticket_id=str(job.ticket_id))


HANDLERS: dict[JobKind, Handler] = {
    JobKind.GENERATE_DRAFT: generate_draft,
    JobKind.SEND_REPLY: send_reply,
}
