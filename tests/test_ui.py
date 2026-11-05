from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.jobs.worker import Worker
from tests.conftest import webhook_payload
from tests.fakes import make_ctx


async def test_ticket_list_and_detail_pages(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    r = await client.post("/webhooks/helpdesk", json=webhook_payload(ticket_id="T-77"))
    ticket_id = r.json()["ticket_id"]
    await Worker(make_ctx(settings, sessionmaker)).run_until_idle()

    index = await client.get("/", follow_redirects=True)
    rows = await client.get("/ui/tickets", headers={"HX-Request": "true"})
    detail = await client.get(f"/ui/tickets/{ticket_id}")

    assert index.status_code == 200
    assert "T-77" in index.text
    assert "<html" not in rows.text  # HTMX partial
    assert "T-77" in rows.text
    assert detail.status_code == 200
    assert "Draft for:" in detail.text
    assert "/ui/tickets/" + ticket_id + "/approve" in detail.text


async def test_approve_from_ui_form(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    r = await client.post("/webhooks/helpdesk", json=webhook_payload())
    ticket_id = r.json()["ticket_id"]
    await Worker(make_ctx(settings, sessionmaker)).run_until_idle()

    approved = await client.post(f"/ui/tickets/{ticket_id}/approve", data={"message": "Edited"})
    again = await client.post(f"/ui/tickets/{ticket_id}/approve", data={"message": "Edited"})

    assert approved.status_code == 200
    assert "approved" in approved.text
    assert "Edited" in approved.text
    assert "Cannot approve" in again.text  # error rendered inline, nothing re-sent
    api = (await client.get(f"/api/v1/tickets/{ticket_id}")).json()
    assert api["final_reply"] == "Edited"
