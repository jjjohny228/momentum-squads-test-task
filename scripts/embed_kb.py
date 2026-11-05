"""Refresh `kb/embeddings.json`: `OPENAI_API_KEY=... uv run python -m scripts.embed_kb`.

Embeds only new/changed chunks, so the committed cache stays in sync with `kb/articles`.
"""

import asyncio
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.drafting.providers import build_embeddings
from app.drafting.rate_limiter import NoopRateLimiter
from app.knowledge.ingest import (
    EMBEDDINGS_CACHE,
    EmbeddingCache,
    build_chunks,
    embed_missing,
    load_articles,
)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=False)
    if not settings.has_openai_key:
        sys.exit("OPENAI_API_KEY is required to compute embeddings")
    cache = EmbeddingCache(
        EMBEDDINGS_CACHE, settings.openai_embedding_model, settings.openai_embedding_dimensions
    )
    chunks = [c for a in load_articles() for c in build_chunks(a, settings)]
    embedded = await embed_missing(
        chunks,
        cache,
        embeddings=build_embeddings(settings),
        limiter=NoopRateLimiter(),
        settings=settings,
    )
    cache.save(keep_ids={str(c.id) for c in chunks})
    print(f"chunks={len(chunks)} embedded={embedded} -> {EMBEDDINGS_CACHE}")


if __name__ == "__main__":
    asyncio.run(main())
