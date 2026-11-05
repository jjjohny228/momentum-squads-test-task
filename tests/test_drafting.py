import asyncio

import httpx
import openai
import pytest
from langchain_core.runnables import RunnableLambda

from app.drafting.llm import LLMDraftGenerator, build_draft_chain
from app.drafting.providers import build_chat_model
from app.drafting.rate_limiter import CallKind
from app.drafting.schemas import DraftOutput
from app.knowledge.schemas import RetrievalResult, RetrievalStrategy, RetrievedArticle
from app.tickets.models import DraftSource
from tests.conftest import make_settings
from tests.fakes import ARTICLE

RETRIEVAL = RetrievalResult(articles=[ARTICLE], strategy=RetrievalStrategy.VECTOR)
OTHER = RetrievedArticle(
    slug="billing-plans",
    title="Тарифи",
    url="http://testserver/kb/billing-plans",
    body="...",
    score=0.5,
)


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls: list[CallKind] = []

    async def acquire(self, kind: CallKind) -> None:
        self.calls.append(kind)


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def fake_openai_server() -> tuple[asyncio.Server, list[bytes]]:
    """Minimal HTTP server answering 401 like OpenAI does for an invalid key."""
    requests: list[bytes] = []
    body = b'{"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}'

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, requests


async def test_keyless_mode_calls_provider_then_returns_static_draft() -> None:
    server, requests = await fake_openai_server()
    port = server.sockets[0].getsockname()[1]
    settings = make_settings(
        openai_api_key=None,
        openai_base_url=f"http://127.0.0.1:{port}/v1",
        fake_llm_delay_min=5,
        fake_llm_delay_max=30,
    )
    limiter, sleep = RecordingLimiter(), RecordingSleep()
    generator = LLMDraftGenerator(
        settings=settings,
        chain=build_draft_chain(build_chat_model(settings)),
        limiter=limiter,
        sleep=sleep,
    )

    async with server:
        draft = await generator.generate("Не можу експортувати звіт у PDF", RETRIEVAL)

    assert len(requests) == 1  # a real request reached the provider...
    assert b"Bearer sk-no-key-configured" in requests[0]  # ...with the placeholder key
    assert limiter.calls == [CallKind.CHAT]
    assert len(sleep.delays) == 1
    assert 4 <= sleep.delays[0] <= 30
    assert draft.source is DraftSource.FALLBACK
    assert "http://testserver/kb/export-pdf" in draft.text
    assert [s.slug for s in draft.sources] == ["export-pdf"]
    assert draft.needs_manual_reply is False


async def test_keyless_without_articles_needs_manual_reply() -> None:
    settings = make_settings(openai_api_key=None)
    generator = LLMDraftGenerator(
        settings=settings,
        chain=RunnableLambda(lambda _: (_ for _ in ()).throw(RuntimeError("401"))),
        limiter=RecordingLimiter(),
        sleep=RecordingSleep(),
    )

    draft = await generator.generate("???", RetrievalResult([], RetrievalStrategy.FULLTEXT))

    assert draft.needs_manual_reply is True
    assert "Корисні статті" not in draft.text


async def test_llm_draft_links_only_cited_known_articles() -> None:
    settings = make_settings(openai_api_key="sk-test")
    seen: list[dict[str, str]] = []

    def fake_llm(inputs: dict[str, str]) -> DraftOutput:
        seen.append(inputs)
        return DraftOutput(
            answer="Відкрийте звіт і натисніть Експорт.",
            cited_slugs=["export-pdf", "made-up-slug", "export-pdf"],
        )

    limiter = RecordingLimiter()
    generator = LLMDraftGenerator(
        settings=settings, chain=RunnableLambda(fake_llm), limiter=limiter
    )

    retrieval = RetrievalResult(articles=[ARTICLE, OTHER], strategy=RetrievalStrategy.VECTOR)
    draft = await generator.generate("PDF?", retrieval)

    assert "[slug: export-pdf]" in seen[0]["context"]
    assert seen[0]["message"] == "PDF?"
    assert draft.source is DraftSource.LLM
    assert draft.text == (
        "Відкрийте звіт і натисніть Експорт.\n\nКорисні статті:\n"
        "- Як експортувати звіт у PDF: http://testserver/kb/export-pdf"
    )
    assert [s.slug for s in draft.sources] == ["export-pdf"]
    assert limiter.calls == [CallKind.CHAT]


async def test_transient_llm_errors_are_retried_and_each_attempt_is_rate_limited() -> None:
    settings = make_settings(openai_api_key="sk-test", llm_max_attempts=3)
    attempts = 0

    def flaky(_: dict[str, str]) -> DraftOutput:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
        return DraftOutput(answer="ok", cited_slugs=[], needs_human=True)

    limiter = RecordingLimiter()
    generator = LLMDraftGenerator(settings=settings, chain=RunnableLambda(flaky), limiter=limiter)

    draft = await generator.generate("q", RETRIEVAL)

    assert attempts == 3
    assert limiter.calls == [CallKind.CHAT] * 3
    assert draft.needs_manual_reply is True


async def test_non_transient_llm_error_is_not_retried() -> None:
    settings = make_settings(openai_api_key="sk-test", llm_max_attempts=3)
    calls = 0

    def bad(_: dict[str, str]) -> DraftOutput:
        nonlocal calls
        calls += 1
        raise ValueError("bad schema")

    generator = LLMDraftGenerator(
        settings=settings, chain=RunnableLambda(bad), limiter=RecordingLimiter()
    )
    with pytest.raises(ValueError, match="bad schema"):
        await generator.generate("q", RETRIEVAL)
    assert calls == 1
