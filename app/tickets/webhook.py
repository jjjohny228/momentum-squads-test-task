import secrets
from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.core.dependencies import SettingsDep
from app.core.exceptions import DomainError, ForbiddenError
from app.tickets.dependencies import TicketServiceDep
from app.tickets.schemas import HelpdeskWebhookIn, WebhookAck

router = APIRouter(tags=["webhooks"])


class ReplyUrlNotAllowedError(DomainError):
    status_code = 422
    title = "Unprocessable entity"


@router.post(
    "/webhooks/helpdesk",
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": WebhookAck, "description": "Duplicate event, already accepted"}},
)
async def helpdesk_webhook(
    payload: HelpdeskWebhookIn,
    response: Response,
    service: TicketServiceDep,
    settings: SettingsDep,
    x_webhook_secret: Annotated[str | None, Header()] = None,
) -> WebhookAck:
    """Accept a new support request. Only persists it and enqueues drafting, so it answers in ms.

    Re-deliveries of the same `event_id` (sequential or concurrent) are acknowledged with 200
    and do not create a second ticket.
    """
    if settings.webhook_secret is not None and not secrets.compare_digest(
        (x_webhook_secret or "").encode(), settings.webhook_secret.get_secret_value().encode()
    ):
        raise ForbiddenError("Invalid webhook secret")
    # We POST to reply_url later, so only trust known helpdesk hosts (SSRF protection)
    allowed = settings.reply_url_allowed_hosts
    if allowed and payload.reply_url.host not in allowed:
        raise ReplyUrlNotAllowedError(f"reply_url host '{payload.reply_url.host}' is not allowed")

    result = await service.intake(payload)
    if not result.created:
        response.status_code = status.HTTP_200_OK
        return WebhookAck(status="duplicate")
    return WebhookAck(status="accepted", ticket_id=result.ticket_id)
