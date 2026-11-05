import asyncio
import time

from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs.models import Job, JobKind
from app.tickets.models import Ticket, TicketStatus
from tests.conftest import webhook_payload


async def count(sessionmaker: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessionmaker() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.text == "OK"


async def test_new_event_is_accepted_and_enqueued(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    payload = webhook_payload()
    r = await client.post("/webhooks/helpdesk", json=payload)

    assert r.status_code == 202
    assert r.json()["status"] == "accepted"
    async with sessionmaker() as s:
        ticket = await s.scalar(select(Ticket))
        job = await s.scalar(select(Job))
    assert ticket is not None
    assert ticket.status == TicketStatus.RECEIVED
    assert ticket.external_ticket_id == payload["ticket_id"]
    assert job is not None
    assert job.kind == JobKind.GENERATE_DRAFT
    assert job.ticket_id == ticket.id


async def test_redelivered_event_is_acknowledged_without_new_ticket(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    payload = webhook_payload()
    first = await client.post("/webhooks/helpdesk", json=payload)
    second = await client.post("/webhooks/helpdesk", json=payload)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "ticket_id": None}
    assert await count(sessionmaker, Ticket) == 1
    assert await count(sessionmaker, Job) == 1


async def test_concurrent_duplicates_create_exactly_one_ticket(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    payload = webhook_payload()
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=payload) for _ in range(20))
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(202) == 1
    assert statuses.count(200) == 19
    assert await count(sessionmaker, Ticket) == 1
    assert await count(sessionmaker, Job) == 1


async def test_webhook_answers_fast_under_burst(client: AsyncClient) -> None:
    started = time.perf_counter()
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=webhook_payload()) for _ in range(50))
    )
    elapsed = time.perf_counter() - started

    assert all(r.status_code == 202 for r in responses)
    assert elapsed < 2.0


async def test_invalid_payload_is_rejected(client: AsyncClient) -> None:
    r = await client.post("/webhooks/helpdesk", json={"event_id": "x"})
    assert r.status_code == 422


async def test_webhook_secret_is_enforced_when_configured(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    app.state.settings.webhook_secret = SecretStr("s3cret")
    payload = webhook_payload()

    denied = await client.post("/webhooks/helpdesk", json=payload)
    allowed = await client.post(
        "/webhooks/helpdesk", json=payload, headers={"X-Webhook-Secret": "s3cret"}
    )

    assert denied.status_code == 403
    assert denied.headers["content-type"] == "application/problem+json"
    assert allowed.status_code == 202


async def test_reply_url_host_allowlist(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    app.state.settings.reply_url_allowed_hosts = ["helpdesk.test"]

    ok = await client.post("/webhooks/helpdesk", json=webhook_payload())
    bad = await client.post(
        "/webhooks/helpdesk",
        json=webhook_payload(reply_url="http://169.254.169.254/latest/meta-data"),
    )

    assert ok.status_code == 202
    assert bad.status_code == 422
