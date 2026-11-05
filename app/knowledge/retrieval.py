from typing import Protocol

from langchain_core.embeddings import Embeddings
from langchain_postgres import PGVector
from langsmith import traceable
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.drafting.rate_limiter import CallKind, RateLimiter
from app.drafting.retry import call_provider
from app.knowledge.models import Article
from app.knowledge.repository import ArticleRepository
from app.knowledge.schemas import RetrievalResult, RetrievalStrategy, RetrievedArticle

log = get_logger(__name__)


def build_vector_store(settings: Settings, engine: AsyncEngine, embeddings: Embeddings) -> PGVector:
    return PGVector(
        embeddings=embeddings,
        connection=engine,
        collection_name=settings.kb_collection,
        embedding_length=settings.openai_embedding_dimensions,
        use_jsonb=True,
        create_extension=False,  # created by the Alembic migration
        async_mode=True,
    )


def article_url(settings: Settings, slug: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/kb/{slug}"


def _retrieval_outputs(result: RetrievalResult) -> dict[str, object]:
    # LangSmith renders `documents` of a retriever run as a list of retrieved docs
    return {
        "strategy": result.strategy.value,
        "documents": [
            {
                "page_content": a.body,
                "metadata": {"slug": a.slug, "title": a.title, "url": a.url, "score": a.score},
            }
            for a in result.articles
        ],
    }


class Retriever(Protocol):
    async def retrieve(self, query: str) -> RetrievalResult: ...


class KnowledgeRetriever:
    """Vector search over PGVector; falls back to Postgres full-text search when the query
    cannot be embedded (no API key, provider outage) or the vector collection is empty."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: PGVector,
        embeddings: Embeddings,
        sessionmaker: async_sessionmaker[AsyncSession],
        limiter: RateLimiter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.embeddings = embeddings
        self.sessionmaker = sessionmaker
        self.limiter = limiter

    @traceable(
        name="kb_retrieve",
        run_type="retriever",
        process_inputs=lambda inputs: {"query": inputs["query"]},
        process_outputs=_retrieval_outputs,
    )
    async def retrieve(self, query: str) -> RetrievalResult:
        try:
            vector = await call_provider(
                lambda: self.embeddings.aembed_query(query),
                limiter=self.limiter,
                kind=CallKind.EMBEDDING,
                attempts=self.settings.llm_max_attempts,
            )
        except Exception as exc:
            # Expected in keyless mode: the real request is made and rejected with 401
            log.warning("embedding_failed_fallback_fulltext", error=type(exc).__name__)
            return await self._fulltext(query)

        hits = await self.store.asimilarity_search_with_score_by_vector(
            vector, k=self.settings.retrieval_top_k * 3
        )
        if not hits:
            log.warning("vector_collection_empty_fallback_fulltext")
            return await self._fulltext(query)

        # Several chunks of one article may match; keep the closest chunk per article
        best: dict[str, float] = {}
        for doc, distance in hits:
            slug = str(doc.metadata["slug"])
            if distance <= self.settings.retrieval_max_distance and distance < best.get(slug, 9.0):
                best[slug] = distance
        ranked = sorted(best.items(), key=lambda kv: kv[1])[: self.settings.retrieval_top_k]
        async with self.sessionmaker() as session:
            articles = await ArticleRepository(session).get_many([slug for slug, _ in ranked])
        return RetrievalResult(
            articles=[
                self._to_retrieved(articles[slug], 1.0 - distance)
                for slug, distance in ranked
                if slug in articles
            ],
            strategy=RetrievalStrategy.VECTOR,
        )

    async def _fulltext(self, query: str) -> RetrievalResult:
        async with self.sessionmaker() as session:
            rows = await ArticleRepository(session).search_fulltext(
                query, self.settings.retrieval_top_k
            )
        return RetrievalResult(
            articles=[self._to_retrieved(article, score) for article, score in rows],
            strategy=RetrievalStrategy.FULLTEXT,
        )

    def _to_retrieved(self, article: Article, score: float) -> RetrievedArticle:
        return RetrievedArticle(
            slug=article.slug,
            title=article.title,
            url=article_url(self.settings, article.slug),
            body=article.body,
            score=round(score, 4),
        )
