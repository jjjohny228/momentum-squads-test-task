"""Knowledge-base ingestion: markdown files -> kb_articles + PGVector chunks with metadata.

Embeddings are cached in `kb/embeddings.json` (keyed by chunk id + content hash) so the KB can be
seeded without an API key and without paying for re-embedding unchanged chunks.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_postgres import PGVector
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.drafting.rate_limiter import CallKind, RateLimiter
from app.drafting.retry import call_provider
from app.knowledge.repository import ArticleRepository
from app.knowledge.retrieval import article_url

log = get_logger(__name__)

KB_DIR = Path(__file__).resolve().parents[2] / "kb"
ARTICLES_DIR = KB_DIR / "articles"
EMBEDDINGS_CACHE = KB_DIR / "embeddings.json"


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ArticleFile:
    slug: str
    title: str
    category: str
    tags: list[str]
    language: str
    body: str
    path: str

    @property
    def content_hash(self) -> str:
        return sha256(f"{self.title}\n{self.body}")


def parse_article(path: Path) -> ArticleFile:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path}: missing YAML front matter")
    _, front, body = raw.split("---", 2)
    meta: dict[str, Any] = yaml.safe_load(front)
    return ArticleFile(
        slug=str(meta["slug"]),
        title=str(meta["title"]),
        category=str(meta.get("category", "general")),
        tags=[str(t) for t in meta.get("tags", [])],
        language=str(meta.get("language", "uk")),
        body=body.strip(),
        path=str(path.relative_to(KB_DIR.parent)),
    )


def load_articles(directory: Path = ARTICLES_DIR) -> list[ArticleFile]:
    return [parse_article(p) for p in sorted(directory.glob("*.md"))]


_header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "h1"), ("##", "h2")], strip_headers=False
)
_size_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)


def build_chunks(article: ArticleFile, settings: Settings) -> list[Document]:
    """Split an article and attach the article's metadata to every chunk."""
    sections = _size_splitter.split_documents(_header_splitter.split_text(article.body))
    ingested_at = datetime.now(UTC).isoformat()
    chunks: list[Document] = []
    for index, section in enumerate(sections):
        # Title in every chunk gives the embedding the article's context
        content = f"{article.title}\n\n{section.page_content}"
        chunks.append(
            Document(
                id=f"{article.slug}:{index}",
                page_content=content,
                metadata={
                    "slug": article.slug,
                    "article_id": article.slug,
                    "title": article.title,
                    "url": article_url(settings, article.slug),
                    "category": article.category,
                    "tags": article.tags,
                    "language": article.language,
                    "section": section.metadata.get("h2") or section.metadata.get("h1"),
                    "chunk_index": index,
                    "chunk_count": len(sections),
                    "content_hash": article.content_hash,
                    "chunk_hash": sha256(content),
                    "source_path": article.path,
                    "embedding_model": settings.openai_embedding_model,
                    "ingested_at": ingested_at,
                },
            )
        )
    return chunks


class EmbeddingCache:
    def __init__(self, path: Path, model: str, dimensions: int) -> None:
        self.path = path
        self.model = model
        self.dimensions = dimensions
        self.items: dict[str, dict[str, Any]] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("model") == model and data.get("dimensions") == dimensions:
                self.items = data.get("chunks", {})
            else:
                log.warning("embedding_cache_model_mismatch", cached=data.get("model"), model=model)

    def get(self, doc: Document) -> list[float] | None:
        item = self.items.get(str(doc.id))
        if item and item.get("hash") == doc.metadata["chunk_hash"]:
            return list(item["embedding"])
        return None

    def put(self, doc: Document, embedding: list[float]) -> None:
        self.items[str(doc.id)] = {"hash": doc.metadata["chunk_hash"], "embedding": embedding}

    def save(self, keep_ids: set[str]) -> None:
        payload = {
            "model": self.model,
            "dimensions": self.dimensions,
            "chunks": {k: v for k, v in sorted(self.items.items()) if k in keep_ids},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class IngestReport:
    articles: int
    chunks: int
    embedded_from_cache: int
    embedded_via_api: int
    skipped_without_embedding: int


async def embed_missing(
    chunks: list[Document],
    cache: EmbeddingCache,
    *,
    embeddings: Embeddings,
    limiter: RateLimiter,
    settings: Settings,
) -> int:
    """Embed chunks that are not in the cache (one batched API request). Returns count embedded."""
    missing = [c for c in chunks if cache.get(c) is None]
    if not missing:
        return 0
    vectors = await call_provider(
        lambda: embeddings.aembed_documents([c.page_content for c in missing]),
        limiter=limiter,
        kind=CallKind.EMBEDDING,
        attempts=settings.llm_max_attempts,
    )
    for chunk, vector in zip(missing, vectors, strict=True):
        cache.put(chunk, vector)
    return len(missing)


async def ingest_knowledge_base(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: PGVector,
    embeddings: Embeddings,
    limiter: RateLimiter,
    articles: list[ArticleFile] | None = None,
    cache: EmbeddingCache | None = None,
) -> IngestReport:
    articles = articles if articles is not None else load_articles()
    cache = cache or EmbeddingCache(
        EMBEDDINGS_CACHE, settings.openai_embedding_model, settings.openai_embedding_dimensions
    )

    await store.acreate_collection()  # creates PGVector tables + collection if missing

    async with sessionmaker() as session, session.begin():
        repo = ArticleRepository(session)
        for article in articles:
            await repo.upsert(
                {
                    "slug": article.slug,
                    "title": article.title,
                    "category": article.category,
                    "tags": article.tags,
                    "language": article.language,
                    "body": article.body,
                    "content_hash": article.content_hash,
                }
            )
        await repo.delete_except([a.slug for a in articles])

    chunks = [chunk for article in articles for chunk in build_chunks(article, settings)]
    from_cache = sum(1 for c in chunks if cache.get(c) is not None)
    via_api = 0
    if settings.has_openai_key:
        via_api = await embed_missing(
            chunks, cache, embeddings=embeddings, limiter=limiter, settings=settings
        )

    ready = [(c, v) for c in chunks if (v := cache.get(c)) is not None]
    skipped = len(chunks) - len(ready)
    if skipped:
        log.warning(
            "chunks_without_embeddings",
            count=skipped,
            hint="set OPENAI_API_KEY or run scripts/embed_kb.py; full-text search still works",
        )
    if ready:
        await store.aadd_embeddings(
            texts=[c.page_content for c, _ in ready],
            embeddings=[v for _, v in ready],
            metadatas=[c.metadata for c, _ in ready],
            ids=[str(c.id) for c, _ in ready],
        )
    await _delete_stale_vectors(sessionmaker, settings.kb_collection, {str(c.id) for c, _ in ready})

    report = IngestReport(
        articles=len(articles),
        chunks=len(chunks),
        embedded_from_cache=from_cache,
        embedded_via_api=via_api,
        skipped_without_embedding=skipped,
    )
    log.info(
        "kb_ingested",
        articles=report.articles,
        chunks=report.chunks,
        from_cache=report.embedded_from_cache,
        via_api=report.embedded_via_api,
        skipped=report.skipped_without_embedding,
    )
    return report


async def _delete_stale_vectors(
    sessionmaker: async_sessionmaker[AsyncSession], collection: str, keep_ids: set[str]
) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                """
                DELETE FROM langchain_pg_embedding e
                USING langchain_pg_collection c
                WHERE e.collection_id = c.uuid AND c.name = :name
                  AND NOT (e.id = ANY(CAST(:keep AS text[])))
                """
            ),
            {"name": collection, "keep": list(keep_ids)},
        )
