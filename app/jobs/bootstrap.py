from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.core.config import Settings
from app.core.db import create_engine, create_sessionmaker
from app.drafting.llm import LLMDraftGenerator, build_draft_chain
from app.drafting.providers import build_chat_model, build_embeddings
from app.drafting.rate_limiter import PostgresRateLimiter
from app.helpdesk.client import HttpHelpdeskClient
from app.jobs.handlers import JobContext
from app.knowledge.retrieval import KnowledgeRetriever, build_vector_store


@asynccontextmanager
async def job_context(settings: Settings) -> AsyncIterator[JobContext]:
    """Wires real dependencies for the worker process."""
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    limiter = PostgresRateLimiter(
        sessionmaker,
        limit=settings.rate_limit_rpm,
        include_embeddings=settings.rate_limit_include_embeddings,
    )
    embeddings = build_embeddings(settings)
    async with httpx.AsyncClient(timeout=settings.helpdesk_timeout_seconds) as http:
        yield JobContext(
            settings=settings,
            sessionmaker=sessionmaker,
            retriever=KnowledgeRetriever(
                settings=settings,
                store=build_vector_store(settings, engine, embeddings),
                embeddings=embeddings,
                sessionmaker=sessionmaker,
                limiter=limiter,
            ),
            drafter=LLMDraftGenerator(
                settings=settings,
                chain=build_draft_chain(build_chat_model(settings)),
                limiter=limiter,
            ),
            helpdesk=HttpHelpdeskClient(http),
        )
    await engine.dispose()
