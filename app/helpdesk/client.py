from typing import Protocol

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)


class HelpdeskError(Exception):
    """Base error of the helpdesk API."""


class RetryableHelpdeskError(HelpdeskError):
    """5xx, timeout or network error: the outcome is unknown, retry with the same key."""


class PermanentHelpdeskError(HelpdeskError):
    """4xx: retrying will not help."""


class HelpdeskClient(Protocol):
    async def send_reply(
        self, *, reply_url: str, ticket_id: str, message: str, idempotency_key: str
    ) -> None: ...


class HttpHelpdeskClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def send_reply(
        self, *, reply_url: str, ticket_id: str, message: str, idempotency_key: str
    ) -> None:
        """POST the reply. A stable Idempotency-Key lets the helpdesk drop re-sent duplicates,
        which is the only way to stay exactly-once when a request timed out mid-flight."""
        try:
            response = await self.http.post(
                reply_url,
                json={"ticket_id": ticket_id, "message": message},
                headers={"Idempotency-Key": idempotency_key},
            )
        except httpx.TransportError as exc:  # timeouts, connection errors
            raise RetryableHelpdeskError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in (200, 201):
            return
        if response.status_code >= 500 or response.status_code == 429:
            raise RetryableHelpdeskError(f"helpdesk returned {response.status_code}")
        raise PermanentHelpdeskError(
            f"helpdesk returned {response.status_code}: {response.text[:200]}"
        )
