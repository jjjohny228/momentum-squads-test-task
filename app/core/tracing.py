"""LangSmith tracing: one LangSmith *thread* per helpdesk ticket.

LangSmith groups traces into threads by the `thread_id` metadata key. Every unit of work for a
ticket (draft generation, operator decision, reply sending) is a separate root trace carrying the
same `thread_id`, so the whole lifecycle of a ticket reads as one thread in the UI. LangChain runs
(prompt, chat model, structured output) nest automatically under the active trace via contextvars.

Tracing is off unless LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set; then every helper
here is a cheap no-op.
"""

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from langsmith import run_helpers, trace, tracing_context
from langsmith.run_trees import RunTree, get_cached_client

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

THREAD_KEY = "thread_id"


def configure_tracing(settings: Settings) -> bool:
    """Export LangSmith settings to the env vars the SDK reads. Returns whether tracing is on."""
    key = settings.langsmith_api_key.get_secret_value() if settings.langsmith_api_key else ""
    if not settings.langsmith_tracing or not key:
        if settings.langsmith_tracing:
            log.warning("langsmith_tracing_requested_without_api_key")
        os.environ["LANGSMITH_TRACING"] = "false"
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.langsmith_endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    log.info("langsmith_tracing_enabled", project=settings.langsmith_project)
    return True


def thread_metadata(*, external_ticket_id: str, ticket_id: object, **extra: Any) -> dict[str, Any]:
    """Metadata attached to every run of a ticket. The helpdesk ticket id is the thread: follow-up
    events of the same conversation land in the same LangSmith thread."""
    return {THREAD_KEY: external_ticket_id, "ticket_uuid": str(ticket_id), **extra}


@asynccontextmanager
async def ticket_trace(
    name: str,
    *,
    metadata: Mapping[str, Any],
    inputs: Mapping[str, Any] | None = None,
    tags: list[str] | None = None,
) -> AsyncIterator[RunTree | None]:
    """Root trace for one unit of work on a ticket; nested LangChain/traceable runs inherit the
    thread metadata. Yields the run (or None when tracing is off) to attach outputs."""
    context = run_helpers.get_tracing_context()
    if not context.get("enabled") and not _env_enabled():
        yield None
        return
    with tracing_context(metadata=dict(metadata), tags=tags):
        async with trace(
            name,
            run_type="chain",
            inputs=dict(inputs or {}),
            metadata=dict(metadata),
            tags=tags,
            client=context.get("client"),
        ) as run:
            yield run


def _env_enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "").lower() == "true"


def flush_traces() -> None:
    """Send buffered runs before the process exits."""
    if not _env_enabled():
        return
    try:
        get_cached_client().flush()
    except Exception:
        log.warning("langsmith_flush_failed", exc_info=True)
