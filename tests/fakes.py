"""Test doubles for the external boundaries (LLM, retrieval, helpdesk, embeddings)."""

import hashlib
import math
import re
from typing import Any

from langchain_core.embeddings import Embeddings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.drafting.schemas import GeneratedDraft
from app.helpdesk.client import RetryableHelpdeskError
from app.jobs.handlers import JobContext
from app.knowledge.schemas import RetrievalResult, RetrievalStrategy, RetrievedArticle
from app.tickets.models import DraftSource
from app.tickets.schemas import SourceRef

ARTICLE = RetrievedArticle(
    slug="export-pdf",
    title="Як експортувати звіт у PDF",
    url="http://testserver/kb/export-pdf",
    body="Натисніть Експорт -> PDF",
    score=0.9,
)


class FakeRetriever:
    def __init__(self, articles: list[RetrievedArticle] | None = None) -> None:
        self.articles = [ARTICLE] if articles is None else articles
        self.queries: list[str] = []

    async def retrieve(self, query: str) -> RetrievalResult:
        self.queries.append(query)
        return RetrievalResult(articles=self.articles, strategy=RetrievalStrategy.VECTOR)


class FakeDrafter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def generate(self, message: str, retrieval: RetrievalResult) -> GeneratedDraft:
        self.calls += 1
        if self.error:
            raise self.error
        return GeneratedDraft(
            text=f"Draft for: {message}",
            source=DraftSource.LLM,
            sources=[SourceRef(slug=a.slug, title=a.title, url=a.url) for a in retrieval.articles],
        )


class FakeHelpdesk:
    """Fails `failures` times with a retryable error, then succeeds. Records every attempt."""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error
        self.calls: list[dict[str, str]] = []

    async def send_reply(
        self, *, reply_url: str, ticket_id: str, message: str, idempotency_key: str
    ) -> None:
        self.calls.append(
            {
                "reply_url": reply_url,
                "ticket_id": ticket_id,
                "message": message,
                "idempotency_key": idempotency_key,
            }
        )
        if self.error:
            raise self.error
        if len(self.calls) <= self.failures:
            raise RetryableHelpdeskError("503")


class KeywordEmbeddings(Embeddings):
    """Deterministic bag-of-words embeddings: texts sharing word prefixes are close."""

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions
        self.calls = 0

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.lower()):
            if len(word) >= 3:
                idx = int(hashlib.md5(word[:5].encode()).hexdigest(), 16) % self.dimensions  # noqa: S324
                vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return self._embed(text)


class FailingEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no key")

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("no key")


def make_ctx(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    **overrides: Any,
) -> JobContext:
    return JobContext(
        settings=settings,
        sessionmaker=sessionmaker,
        retriever=overrides.get("retriever", FakeRetriever()),
        drafter=overrides.get("drafter", FakeDrafter()),
        helpdesk=overrides.get("helpdesk", FakeHelpdesk()),
    )
