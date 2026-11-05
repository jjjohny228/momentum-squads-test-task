"""LangSmith tracing: every run of a ticket carries the same thread_id (one thread per ticket)."""

from typing import Any
from unittest.mock import MagicMock

import langsmith
from httpx import AsyncClient
from langchain_core.runnables import RunnableLambda
from langsmith import get_current_run_tree, tracing_context
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.tracing import configure_tracing, thread_metadata, ticket_trace
from app.drafting.llm import LLMDraftGenerator
from app.drafting.rate_limiter import NoopRateLimiter
from app.drafting.schemas import GeneratedDraft
from app.jobs.worker import Worker
from app.knowledge.schemas import RetrievalResult, RetrievalStrategy
from tests.conftest import make_settings, webhook_payload
from tests.fakes import ARTICLE, FakeDrafter, FakeHelpdesk, make_ctx


class ThreadAwareDrafter(FakeDrafter):
    """Records the metadata of the run active while the LLM would be called."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_metadata: dict[str, Any] = {}

    async def generate(self, message: str, retrieval: RetrievalResult) -> GeneratedDraft:
        run = get_current_run_tree()
        assert run is not None
        self.seen_metadata = dict(run.metadata)
        return await super().generate(message, retrieval)


def created_runs(client: MagicMock) -> list[dict[str, Any]]:
    runs = []
    for call in client.create_run.call_args_list:
        kwargs = call.kwargs
        metadata = (kwargs.get("extra") or {}).get("metadata", {})
        runs.append(
            {"name": kwargs["name"], "metadata": metadata, "parent": kwargs.get("parent_run_id")}
        )
    return runs


async def test_ticket_lifecycle_is_one_langsmith_thread(
    client: AsyncClient, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ls_client = MagicMock(spec=langsmith.Client)
    drafter = ThreadAwareDrafter()
    worker = Worker(make_ctx(settings, sessionmaker, drafter=drafter, helpdesk=FakeHelpdesk()))

    with tracing_context(enabled=True, client=ls_client):
        r = await client.post("/webhooks/helpdesk", json=webhook_payload(ticket_id="T-555"))
        ticket_id = r.json()["ticket_id"]
        await worker.run_until_idle()
        await client.post(f"/api/v1/tickets/{ticket_id}/approve", json={"message": "Edited"})
        await worker.run_until_idle()

    runs = created_runs(ls_client)
    roots = [run for run in runs if run["parent"] is None]
    assert [run["name"] for run in roots] == ["generate_draft", "operator_approve", "send_reply"]
    assert {run["metadata"]["thread_id"] for run in runs} == {"T-555"}
    assert {run["metadata"]["ticket_uuid"] for run in roots} == {ticket_id}
    # nested work (retrieval, LLM chain) inherits the thread metadata
    assert drafter.seen_metadata["thread_id"] == "T-555"


def test_tracing_requires_api_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert configure_tracing(make_settings(langsmith_tracing=True, langsmith_api_key=None)) is False
    assert configure_tracing(make_settings(langsmith_tracing=False)) is False

    monkeypatch.setenv("LANGSMITH_API_KEY", "x")  # restored by monkeypatch afterwards
    monkeypatch.setenv("LANGSMITH_PROJECT", "x")
    on = make_settings(langsmith_tracing=True, langsmith_api_key="lsv2_test", langsmith_project="p")
    assert configure_tracing(on) is True
    configure_tracing(make_settings())  # leave tracing off for the other tests


async def test_langchain_and_retriever_runs_nest_under_ticket_trace() -> None:
    def provider_401(_: dict[str, str]) -> None:
        raise RuntimeError("401 Unauthorized")

    ls_client = MagicMock(spec=langsmith.Client)
    generator = LLMDraftGenerator(
        settings=make_settings(openai_api_key=None),
        chain=RunnableLambda(provider_401),
        limiter=NoopRateLimiter(),
    )
    meta = thread_metadata(external_ticket_id="T-9", ticket_id="u-1")

    with tracing_context(enabled=True, client=ls_client):
        async with ticket_trace("generate_draft", metadata=meta):
            await generator.generate("PDF?", RetrievalResult([ARTICLE], RetrievalStrategy.VECTOR))

    runs = {run["name"]: run for run in created_runs(ls_client)}
    assert {"generate_draft", "keyless_static_draft", "draft_chain"} <= runs.keys()
    assert runs["draft_chain"]["parent"] is not None
    assert all(run["metadata"]["thread_id"] == "T-9" for run in runs.values())
