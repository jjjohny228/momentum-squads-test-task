from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.drafting.rate_limiter import NoopRateLimiter
from app.knowledge.ingest import EmbeddingCache, build_chunks, ingest_knowledge_base, load_articles
from app.knowledge.repository import build_prefix_tsquery
from app.knowledge.retrieval import KnowledgeRetriever, build_vector_store
from app.knowledge.schemas import RetrievalStrategy
from tests.conftest import make_settings
from tests.fakes import FailingEmbeddings, KeywordEmbeddings

REQUIRED_METADATA = {
    "slug",
    "article_id",
    "title",
    "url",
    "category",
    "tags",
    "language",
    "chunk_index",
    "chunk_count",
    "content_hash",
    "chunk_hash",
    "source_path",
    "embedding_model",
    "ingested_at",
}


def test_articles_parse_with_front_matter() -> None:
    articles = load_articles()
    assert 5 <= len(articles) <= 10
    assert len({a.slug for a in articles}) == len(articles)
    assert all(a.title and a.body and a.language == "uk" for a in articles)


def test_chunks_carry_article_metadata(settings: Settings) -> None:
    article = next(a for a in load_articles() if a.slug == "export-pdf")
    chunks = build_chunks(article, settings)

    assert chunks
    for i, chunk in enumerate(chunks):
        assert chunk.metadata.keys() >= REQUIRED_METADATA
        assert chunk.id == f"export-pdf:{i}"
        assert chunk.metadata["url"] == "http://testserver/kb/export-pdf"
        assert chunk.metadata["tags"] == ["export", "pdf", "reports"]
        assert chunk.page_content.startswith(article.title)


def test_prefix_tsquery() -> None:
    assert (
        build_prefix_tsquery("Не можу експортувати звіт у PDF")
        == "pdf:* | експо:* | звіт:* | можу:*"
    )
    assert build_prefix_tsquery("?! a b") is None


async def ingest(
    settings: Settings,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    embeddings: KeywordEmbeddings | FailingEmbeddings,
    cache_path: Path,
):  # type: ignore[no-untyped-def]
    store = build_vector_store(settings, engine, embeddings)
    report = await ingest_knowledge_base(
        settings=settings,
        sessionmaker=sessionmaker,
        store=store,
        embeddings=embeddings,
        limiter=NoopRateLimiter(),
        cache=EmbeddingCache(cache_path, "test-model", settings.openai_embedding_dimensions),
    )
    return store, report


async def test_ingest_stores_vectors_with_metadata_and_uses_cache(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = make_settings(openai_api_key="sk-test")
    embeddings = KeywordEmbeddings()
    cache_path = tmp_path / "embeddings.json"

    _, first = await ingest(settings, engine, sessionmaker, embeddings, cache_path)
    assert first.embedded_via_api == first.chunks
    assert first.skipped_without_embedding == 0

    async with sessionmaker() as s:
        rows = (await s.execute(text("SELECT id, cmetadata FROM langchain_pg_embedding"))).all()
    assert len(rows) == first.chunks
    assert all(set(r.cmetadata) >= REQUIRED_METADATA for r in rows)

    # A committed cache makes re-seeding free: nothing is sent to the provider again
    chunks = [c for a in load_articles() for c in build_chunks(a, settings)]
    cache = EmbeddingCache(cache_path, "test-model", settings.openai_embedding_dimensions)
    for chunk in chunks:
        cache.put(chunk, embeddings.embed_query(chunk.page_content))
    cache.save(keep_ids={str(c.id) for c in chunks})
    _, second = await ingest(settings, engine, sessionmaker, KeywordEmbeddings(), cache_path)
    assert second.embedded_from_cache == second.chunks
    assert second.embedded_via_api == 0


async def test_ingest_without_key_keeps_fulltext_working(
    settings: Settings,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    _, report = await ingest(
        settings, engine, sessionmaker, FailingEmbeddings(), tmp_path / "c.json"
    )

    assert report.articles == len(load_articles())
    assert report.embedded_via_api == 0
    assert report.skipped_without_embedding == report.chunks


async def test_vector_retrieval_finds_relevant_article(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = make_settings(openai_api_key="sk-test", retrieval_max_distance=0.95)
    embeddings = KeywordEmbeddings()
    store, _ = await ingest(settings, engine, sessionmaker, embeddings, tmp_path / "c.json")
    retriever = KnowledgeRetriever(
        settings=settings,
        store=store,
        embeddings=embeddings,
        sessionmaker=sessionmaker,
        limiter=NoopRateLimiter(),
    )

    result = await retriever.retrieve("Як експортувати звіт у PDF?")

    assert result.strategy is RetrievalStrategy.VECTOR
    assert result.articles[0].slug == "export-pdf"
    assert result.articles[0].url == "http://testserver/kb/export-pdf"
    assert len(result.articles) <= settings.retrieval_top_k


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Не можу експортувати звіт у PDF", "export-pdf"),
        ("Забув пароль, не можу увійти", "reset-password"),
        ("Як запросити колегу в команду?", "invite-users"),
    ],
)
async def test_fulltext_fallback_when_embedding_fails(
    settings: Settings,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    query: str,
    expected: str,
) -> None:
    store, _ = await ingest(
        settings, engine, sessionmaker, FailingEmbeddings(), tmp_path / "c.json"
    )
    retriever = KnowledgeRetriever(
        settings=settings,
        store=store,
        embeddings=FailingEmbeddings(),
        sessionmaker=sessionmaker,
        limiter=NoopRateLimiter(),
    )

    result = await retriever.retrieve(query)

    assert result.strategy is RetrievalStrategy.FULLTEXT
    assert result.articles[0].slug == expected


async def test_kb_page_and_list(
    client: AsyncClient,
    settings: Settings,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await ingest(settings, engine, sessionmaker, FailingEmbeddings(), tmp_path / "c.json")

    page = await client.get("/kb/export-pdf")
    listing = await client.get("/api/v1/kb")
    missing = await client.get("/kb/nope")

    assert page.status_code == 200
    assert "<h1>Як експортувати звіт у PDF</h1>" in page.text
    assert {a["slug"] for a in listing.json()} >= {"export-pdf", "reset-password"}
    assert missing.status_code == 404
