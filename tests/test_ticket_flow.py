"""End-to-end lifecycle through the JSON API and the worker (with faked LLM/helpdesk)."""

import asyncio
import uuid

from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.helpdesk.client import PermanentHelpdeskError
from app.jobs.models import Job, JobKind, JobStatus
from app.jobs.worker import Worker
from tests.conftest import webhook_payload
from tests.fakes import FakeDrafter, FakeHelpdesk, make_ctx


async def create_ticket(client: AsyncClient, **overrides: str) -> str:
    r = await client.post("/webhooks/helpdesk", json=webhook_payload(**overrides))
    assert r.status_code == 202
    return str(r.json()["ticket_id"])


async def get_ticket(client: AsyncClient, ticket_id: str) -> dict:  # type: ignore[type-arg]
    r = await client.get(f"/api/v1/tickets/{ticket_id}")
    assert r.status_code == 200
    return r.json()  # type: ignore[no-any-return]


async def test_full_flow_draft_approve_send(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    helpdesk = FakeHelpdesk()
    worker = Worker(make_ctx(settings, sessionmaker, helpdesk=helpdesk))
    ticket_id = await create_ticket(client, ticket_id="T-1042")

    await worker.run_until_idle()
    ticket = await get_ticket(client, ticket_id)
    assert ticket["status"] == "draft_ready"
    assert ticket["draft"].startswith("Draft for:")
    assert ticket["sources"][0]["slug"] == "export-pdf"

    r = await client.post(f"/api/v1/tickets/{ticket_id}/approve", json={"message": "Edited reply"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    await worker.run_until_idle()
    ticket = await get_ticket(client, ticket_id)
    assert ticket["status"] == "sent"
    assert ticket["sent_at"] is not None
    assert helpdesk.calls == [
        {
            "reply_url": "http://helpdesk.test/tickets/T-1042/replies",
            "ticket_id": "T-1042",
            "message": "Edited reply",
            "idempotency_key": f"reply-{ticket_id}",
        }
    ]


async def test_approve_without_body_sends_generated_draft(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    helpdesk = FakeHelpdesk()
    worker = Worker(make_ctx(settings, sessionmaker, helpdesk=helpdesk))
    ticket_id = await create_ticket(client)
    await worker.run_until_idle()

    await client.post(f"/api/v1/tickets/{ticket_id}/approve")
    await worker.run_until_idle()

    assert helpdesk.calls[0]["message"].startswith("Draft for:")


async def test_concurrent_approvals_send_only_once(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    helpdesk = FakeHelpdesk()
    worker = Worker(make_ctx(settings, sessionmaker, helpdesk=helpdesk))
    ticket_id = await create_ticket(client)
    await worker.run_until_idle()

    responses = await asyncio.gather(
        *(client.post(f"/api/v1/tickets/{ticket_id}/approve") for _ in range(5))
    )
    await worker.run_until_idle()

    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]
    assert len(helpdesk.calls) == 1
    async with sessionmaker() as s:
        send_jobs = (await s.scalars(select(Job).where(Job.kind == JobKind.SEND_REPLY))).all()
    assert len(send_jobs) == 1


async def test_transient_helpdesk_errors_are_retried_with_same_idempotency_key(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    helpdesk = FakeHelpdesk(failures=2)
    worker = Worker(make_ctx(settings, sessionmaker, helpdesk=helpdesk))
    ticket_id = await create_ticket(client)
    await worker.run_until_idle()
    await client.post(f"/api/v1/tickets/{ticket_id}/approve")

    for _ in range(3):  # each run processes the due retry
        await asyncio.sleep(0.05)
        await worker.run_until_idle()

    assert (await get_ticket(client, ticket_id))["status"] == "sent"
    assert len(helpdesk.calls) == 3
    assert {c["idempotency_key"] for c in helpdesk.calls} == {f"reply-{ticket_id}"}


async def test_permanent_helpdesk_error_fails_ticket_and_retry_resends(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    broken = FakeHelpdesk(error=PermanentHelpdeskError("404"))
    ticket_id = await create_ticket(client)
    await Worker(make_ctx(settings, sessionmaker, helpdesk=broken)).run_until_idle()
    await client.post(f"/api/v1/tickets/{ticket_id}/approve", json={"message": "Hi"})
    await Worker(make_ctx(settings, sessionmaker, helpdesk=broken)).run_until_idle()

    ticket = await get_ticket(client, ticket_id)
    assert ticket["status"] == "failed"
    assert "404" in ticket["last_error"]
    assert len(broken.calls) == 1

    healthy = FakeHelpdesk()
    r = await client.post(f"/api/v1/tickets/{ticket_id}/retry")
    assert r.json()["status"] == "approved"
    await Worker(make_ctx(settings, sessionmaker, helpdesk=healthy)).run_until_idle()

    assert (await get_ticket(client, ticket_id))["status"] == "sent"
    assert healthy.calls[0]["message"] == "Hi"


async def test_generation_failure_exhausts_attempts_then_retry_regenerates(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    failing = FakeDrafter(error=RuntimeError("LLM down"))
    worker = Worker(make_ctx(settings, sessionmaker, drafter=failing))
    ticket_id = await create_ticket(client)

    for _ in range(settings.job_max_attempts):
        await asyncio.sleep(0.05)
        await worker.run_until_idle()

    ticket = await get_ticket(client, ticket_id)
    assert ticket["status"] == "failed"
    assert failing.calls == settings.job_max_attempts
    async with sessionmaker() as s:
        job = await s.scalar(select(Job))
    assert job is not None
    assert job.status == JobStatus.FAILED

    await client.post(f"/api/v1/tickets/{ticket_id}/retry")
    await Worker(make_ctx(settings, sessionmaker)).run_until_idle()
    assert (await get_ticket(client, ticket_id))["status"] == "draft_ready"


async def test_reject(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await create_ticket(client)
    early = await client.post(f"/api/v1/tickets/{ticket_id}/reject")
    assert early.status_code == 409

    await Worker(make_ctx(settings, sessionmaker)).run_until_idle()
    r = await client.post(f"/api/v1/tickets/{ticket_id}/reject")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    again = await client.post(f"/api/v1/tickets/{ticket_id}/approve")
    assert again.status_code == 409


async def test_unknown_ticket_returns_problem_json(client: AsyncClient) -> None:
    r = await client.post(f"/api/v1/tickets/{uuid.uuid4()}/approve")
    assert r.status_code == 404
    assert r.json()["title"] == "Not found"


async def test_list_filters_by_status(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_ticket(client)
    await create_ticket(client)
    await Worker(make_ctx(settings, sessionmaker)).run_until_idle()
    await create_ticket(client)

    all_ = (await client.get("/api/v1/tickets")).json()
    ready = (await client.get("/api/v1/tickets", params={"status": "draft_ready"})).json()

    assert all_["total"] == 3
    assert ready["total"] == 2
    assert {t["status"] for t in ready["items"]} == {"draft_ready"}


async def test_operator_basic_auth(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    app.state.settings.operator_username = "op"
    app.state.settings.operator_password = SecretStr("pw")

    assert (await client.get("/api/v1/tickets")).status_code == 401
    assert (await client.get("/ui/tickets")).status_code == 401
    assert (await client.get("/api/v1/tickets", auth=("op", "pw"))).status_code == 200
    # the webhook stays reachable for the helpdesk
    assert (await client.post("/webhooks/helpdesk", json=webhook_payload())).status_code == 202


async def test_stats(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_ticket(client)
    await create_ticket(client)
    await Worker(make_ctx(settings, sessionmaker)).run_until_idle()

    stats = (await client.get("/api/v1/tickets/stats")).json()

    assert stats["tickets_by_status"] == {"draft_ready": 2}
    assert stats["rate_limit_rpm"] == 60
