"""Seed the knowledge base: `python -m scripts.seed_kb` (idempotent, run by `migrate`)."""

import asyncio

from app.core.config import get_settings
from app.core.db import create_engine, create_sessionmaker
from app.core.logging import configure_logging
from app.drafting.providers import build_embeddings
from app.drafting.rate_limiter import PostgresRateLimiter
from app.knowledge.ingest import ingest_knowledge_base
from app.knowledge.retrieval import build_vector_store


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    embeddings = build_embeddings(settings)
    try:
        await ingest_knowledge_base(
            settings=settings,
            sessionmaker=sessionmaker,
            store=build_vector_store(settings, engine, embeddings),
            embeddings=embeddings,
            limiter=PostgresRateLimiter(
                sessionmaker,
                limit=settings.rate_limit_rpm,
                include_embeddings=settings.rate_limit_include_embeddings,
            ),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
