"""Mock helpdesk: receives replies (with injected failures) and can fire webhooks at the service.

State is in memory; run a single process.
"""

import asyncio
import os
import random
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

app = FastAPI(title="Mock helpdesk", version="0.1.0")


class ChaosConfig(BaseModel):
    failure_rate: float = Field(default=float(os.getenv("MOCK_FAILURE_RATE", "0.2")), ge=0, le=1)
    slow_rate: float = Field(default=float(os.getenv("MOCK_SLOW_RATE", "0.1")), ge=0, le=1)
    slow_seconds: float = Field(default=float(os.getenv("MOCK_SLOW_SECONDS", "12")), ge=0)


class ReplyIn(BaseModel):
    ticket_id: str
    message: str


class StoredReply(BaseModel):
    id: str
    ticket_id: str
    message: str
    idempotency_key: str | None
    received_at: datetime


class Stats(BaseModel):
    delivered: int = 0
    duplicates_suppressed: int = 0
    failures_injected: int = 0
    slow_responses_injected: int = 0


class State:
    def __init__(self) -> None:
        self.config = ChaosConfig()
        self.replies: list[StoredReply] = []
        self.by_key: dict[str, StoredReply] = {}
        self.stats = Stats()
        self.lock = asyncio.Lock()


state = State()


@app.get("/health")
async def health() -> str:
    return "OK"


@app.post("/tickets/{ticket_id}/replies", status_code=status.HTTP_201_CREATED)
async def create_reply(
    ticket_id: str,
    body: ReplyIn,
    response: Response,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> StoredReply:
    cfg = state.config
    if random.random() < cfg.failure_rate:
        state.stats.failures_injected += 1
        raise HTTPException(status_code=503, detail="Injected failure")

    async with state.lock:
        if idempotency_key and idempotency_key in state.by_key:
            state.stats.duplicates_suppressed += 1
            response.status_code = status.HTTP_200_OK
            return state.by_key[idempotency_key]
        reply = StoredReply(
            id=str(uuid.uuid4()),
            ticket_id=ticket_id,
            message=body.message,
            idempotency_key=idempotency_key,
            received_at=datetime.now(UTC),
        )
        state.replies.append(reply)
        if idempotency_key:
            state.by_key[idempotency_key] = reply
        state.stats.delivered += 1

    if random.random() < cfg.slow_rate:
        # The reply IS stored, but the caller times out: the dangerous case for duplicates
        state.stats.slow_responses_injected += 1
        await asyncio.sleep(cfg.slow_seconds)
    return reply


@app.get("/replies")
async def list_replies(ticket_id: str | None = None) -> dict[str, Any]:
    items = [r for r in state.replies if ticket_id is None or r.ticket_id == ticket_id]
    per_ticket: dict[str, int] = {}
    for r in state.replies:
        per_ticket[r.ticket_id] = per_ticket.get(r.ticket_id, 0) + 1
    return {
        "items": items,
        "stats": state.stats,
        "tickets_with_multiple_replies": [t for t, n in per_ticket.items() if n > 1],
    }


@app.delete("/replies", status_code=status.HTTP_204_NO_CONTENT)
async def reset_replies() -> None:
    state.replies.clear()
    state.by_key.clear()
    state.stats = Stats()


@app.get("/config")
async def get_config() -> ChaosConfig:
    return state.config


@app.put("/config")
async def set_config(config: ChaosConfig) -> ChaosConfig:
    state.config = config
    return config


# --- webhook simulator ------------------------------------------------------------------------

WEBHOOK_URL = os.getenv("MOCK_WEBHOOK_URL", "http://localhost:8000/webhooks/helpdesk")
PUBLIC_REPLY_BASE = os.getenv("MOCK_PUBLIC_REPLY_BASE", "http://localhost:8001")
WEBHOOK_TIMEOUT = 2.0
WEBHOOK_RETRIES = 3


class SimulateIn(BaseModel):
    message: str = "Не можу експортувати звіт у PDF"
    customer_email: str = "user@example.com"
    ticket_id: str | None = None
    event_id: str | None = None
    concurrent_duplicates: int = Field(default=0, ge=0, le=20)


async def deliver_webhook(client: httpx.AsyncClient, payload: dict[str, str]) -> dict[str, Any]:
    """Behaves like the real helpdesk: 2 s timeout, re-delivers on timeout/5xx."""
    attempts: list[str] = []
    for _ in range(WEBHOOK_RETRIES):
        try:
            r = await client.post(WEBHOOK_URL, json=payload, timeout=WEBHOOK_TIMEOUT)
            attempts.append(str(r.status_code))
            if r.status_code < 500:
                return {"attempts": attempts, "response": r.json()}
        except httpx.HTTPError as exc:
            attempts.append(type(exc).__name__)
    return {"attempts": attempts, "response": None}


@app.post("/simulate/tickets")
async def simulate_ticket(body: SimulateIn) -> dict[str, Any]:
    ticket_id = body.ticket_id or f"T-{random.randint(1000, 99999)}"
    payload = {
        "event_id": body.event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "ticket_id": ticket_id,
        "customer_email": body.customer_email,
        "message": body.message,
        "reply_url": f"{PUBLIC_REPLY_BASE}/tickets/{ticket_id}/replies",
    }
    async with httpx.AsyncClient() as client:
        deliveries = await asyncio.gather(
            *(deliver_webhook(client, payload) for _ in range(1 + body.concurrent_duplicates))
        )
    return {"payload": payload, "deliveries": deliveries}
