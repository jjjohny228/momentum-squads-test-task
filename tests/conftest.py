import os
import uuid

# Tests must never send traces to a real LangSmith project configured in the developer's .env.
# Env vars take precedence over .env, and this runs before `app.main` is imported.
os.environ["LANGSMITH_TRACING"] = "false"
from collections.abc import AsyncIterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.db import create_engine, create_sessionmaker
from app.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg://support:support@localhost:5432/support_test"
)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "openai_api_key": None,
        "public_base_url": "http://testserver",
        "fake_llm_delay_min": 0.0,
        "fake_llm_delay_max": 0.0,
        "job_backoff_base_seconds": 0.01,
        "job_backoff_max_seconds": 0.01,
        "job_max_attempts": 3,
        "llm_max_attempts": 1,
        "log_json": False,
        "reply_url_allowed_hosts": [],
        "webhook_secret": None,
        "operator_username": None,
        "operator_password": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(scope="session", autouse=True)
def _migrate() -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine(make_settings())
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture(autouse=True)
async def _clean_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE tickets, jobs, provider_calls, kb_articles CASCADE"))
        exists = await conn.scalar(text("SELECT to_regclass('langchain_pg_embedding')"))
        if exists:
            await conn.execute(text("DELETE FROM langchain_pg_embedding"))


@pytest.fixture
def app(
    settings: Settings, engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> FastAPI:
    application = create_app(settings)
    # ASGITransport does not run the lifespan; reuse the test engine instead
    application.state.engine = engine
    application.state.sessionmaker = sessionmaker
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def webhook_payload(**overrides: str) -> dict[str, str]:
    ticket_id = overrides.pop("ticket_id", f"T-{uuid.uuid4().hex[:6]}")
    payload = {
        "event_id": f"evt_{uuid.uuid4().hex[:10]}",
        "ticket_id": ticket_id,
        "customer_email": "user@example.com",
        "message": "Не можу експортувати звіт у PDF",
        "reply_url": f"http://helpdesk.test/tickets/{ticket_id}/replies",
    }
    payload.update(overrides)
    return payload
