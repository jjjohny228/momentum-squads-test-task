import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.helpdesk.client import HttpHelpdeskClient, PermanentHelpdeskError, RetryableHelpdeskError
from mock_helpdesk.main import ChaosConfig, app, state

URL = "http://helpdesk.test/tickets/T-1/replies"


async def send(client: HttpHelpdeskClient) -> None:
    await client.send_reply(reply_url=URL, ticket_id="T-1", message="Hi", idempotency_key="reply-1")


@respx.mock
async def test_success_sends_payload_and_idempotency_key() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as http:
        await send(HttpHelpdeskClient(http))

    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == "reply-1"
    assert request.read() == b'{"ticket_id":"T-1","message":"Hi"}'


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(503), RetryableHelpdeskError),
        (httpx.Response(429), RetryableHelpdeskError),
        (httpx.ReadTimeout("slow"), RetryableHelpdeskError),
        (httpx.ConnectError("down"), RetryableHelpdeskError),
        (httpx.Response(400), PermanentHelpdeskError),
        (httpx.Response(404), PermanentHelpdeskError),
    ],
)
@respx.mock
async def test_errors_are_classified(
    response: httpx.Response | Exception, error: type[Exception]
) -> None:
    route = respx.post(URL)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(error):
            await send(HttpHelpdeskClient(http))


@pytest.fixture
async def mock_client() -> AsyncClient:
    state.config = ChaosConfig(failure_rate=0, slow_rate=0, slow_seconds=0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mock") as c:
        await c.delete("/replies")
        yield c  # type: ignore[misc]


async def test_mock_helpdesk_deduplicates_by_idempotency_key(mock_client: AsyncClient) -> None:
    body = {"ticket_id": "T-1", "message": "Hi"}
    first = await mock_client.post(
        "/tickets/T-1/replies", json=body, headers={"Idempotency-Key": "k1"}
    )
    second = await mock_client.post(
        "/tickets/T-1/replies", json=body, headers={"Idempotency-Key": "k1"}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    replies = (await mock_client.get("/replies")).json()
    assert len(replies["items"]) == 1
    assert replies["stats"]["duplicates_suppressed"] == 1
    assert replies["tickets_with_multiple_replies"] == []


async def test_mock_helpdesk_injects_failures(mock_client: AsyncClient) -> None:
    await mock_client.put("/config", json={"failure_rate": 1, "slow_rate": 0, "slow_seconds": 0})

    r = await mock_client.post("/tickets/T-1/replies", json={"ticket_id": "T-1", "message": "x"})

    assert r.status_code == 503
    assert (await mock_client.get("/replies")).json()["items"] == []
