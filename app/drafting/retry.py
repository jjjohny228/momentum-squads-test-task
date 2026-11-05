from collections.abc import Awaitable, Callable

import openai
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.drafting.rate_limiter import CallKind, RateLimiter

TRANSIENT_PROVIDER_ERRORS: tuple[type[Exception], ...] = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


async def call_provider[T](
    fn: Callable[[], Awaitable[T]],
    *,
    limiter: RateLimiter,
    kind: CallKind,
    attempts: int,
) -> T:
    """Run one provider request with retries; each attempt is rate-limited on its own."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1, max=20),
        retry=retry_if_exception_type(TRANSIENT_PROVIDER_ERRORS),
        reraise=True,
    ):
        with attempt:
            await limiter.acquire(kind)
            return await fn()
    raise AssertionError("unreachable")  # pragma: no cover
