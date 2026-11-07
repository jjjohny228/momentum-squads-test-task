import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable, RunnableConfig
from langsmith import traceable

from app.core.config import Settings
from app.core.logging import get_logger
from app.drafting.prompts import (
    DRAFT_PROMPT,
    LINKS_HEADER_UK,
    format_context,
    static_reply,
)
from app.drafting.rate_limiter import CallKind, RateLimiter
from app.drafting.retry import call_provider
from app.drafting.schemas import DraftOutput, GeneratedDraft
from app.knowledge.schemas import RetrievalResult, RetrievedArticle
from app.tickets.models import DraftSource
from app.tickets.schemas import SourceRef

log = get_logger(__name__)

# Run names/tags as they appear in LangSmith (thread metadata is inherited from the job trace)
CHAIN_CONFIG: RunnableConfig = {"run_name": "draft_chain", "tags": ["llm"]}
KEYLESS_CHAIN_CONFIG: RunnableConfig = {"run_name": "draft_chain", "tags": ["llm", "keyless"]}


def build_draft_chain(model: BaseChatModel) -> Runnable[dict[str, Any], Any]:
    """LCEL: prompt -> chat model with JSON-schema structured output."""
    return DRAFT_PROMPT | model.with_structured_output(DraftOutput)


def render_reply(answer: str, articles: list[RetrievedArticle]) -> str:
    """Links come from our metadata, never from the model, so they cannot be hallucinated."""
    if not articles:
        return answer.strip()
    links = "\n".join(f"- {a.title}: {a.url}" for a in articles)
    return f"{answer.strip()}\n\n{LINKS_HEADER_UK}\n{links}"


def to_source_refs(articles: list[RetrievedArticle]) -> list[SourceRef]:
    return [SourceRef(slug=a.slug, title=a.title, url=a.url, score=a.score) for a in articles]


class DraftGenerator(Protocol):
    async def generate(self, message: str, retrieval: RetrievalResult) -> GeneratedDraft: ...


class LLMDraftGenerator:
    """Generates a reply draft with the LLM.

    Without an API key the request is still sent to the provider (and rejected), then a static
    draft is returned after a realistic 5-30 s delay, so the rest of the system behaves the same.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        chain: Runnable[dict[str, Any], Any],
        limiter: RateLimiter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.chain = chain
        self.limiter = limiter
        self.sleep = sleep

    async def generate(self, message: str, retrieval: RetrievalResult) -> GeneratedDraft:
        inputs = {
            "message": message,
            "context": format_context(retrieval.articles),
            "product": self.settings.product_name,
        }
        if not self.settings.has_openai_key:
            return await self._keyless(inputs, retrieval)

        output: DraftOutput = await call_provider(
            lambda: self.chain.ainvoke(inputs, config=CHAIN_CONFIG),
            limiter=self.limiter,
            kind=CallKind.CHAT,
            attempts=self.settings.llm_max_attempts,
        )
        by_slug = {a.slug: a for a in retrieval.articles}
        cited = [by_slug[s] for s in dict.fromkeys(output.cited_slugs) if s in by_slug]
        return GeneratedDraft(
            text=render_reply(output.answer, cited),
            source=DraftSource.LLM,
            sources=to_source_refs(cited),
            needs_manual_reply=output.needs_human or not retrieval.articles,
            retrieval=retrieval.strategy,
        )

    @traceable(
        name="keyless_static_draft",
        process_inputs=lambda inputs: {"message": inputs["inputs"]["message"]},
        process_outputs=lambda draft: {"draft": draft.text, "source": draft.source.value},
    )
    async def _keyless(self, inputs: dict[str, Any], retrieval: RetrievalResult) -> GeneratedDraft:
        started = time.monotonic()
        target_delay = random.uniform(
            self.settings.fake_llm_delay_min, self.settings.fake_llm_delay_max
        )
        await self.limiter.acquire(CallKind.CHAT)
        try:
            await self.chain.ainvoke(inputs, config=KEYLESS_CHAIN_CONFIG)
        except Exception as exc:
            log.info("keyless_provider_call_rejected", error=type(exc).__name__)
        await self.sleep(max(0.0, target_delay - (time.monotonic() - started)))

        articles = retrieval.articles
        answer = static_reply(self.settings.product_name, has_articles=bool(articles))
        return GeneratedDraft(
            text=render_reply(answer, articles),
            source=DraftSource.FALLBACK,
            sources=to_source_refs(articles),
            needs_manual_reply=not articles,
            retrieval=retrieval.strategy,
        )
