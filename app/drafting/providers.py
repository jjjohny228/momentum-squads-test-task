"""Factories for LangChain/OpenAI clients.

Built-in SDK retries are disabled (`max_retries=0`): retries are done by our code so that every
single HTTP request to the provider passes through the shared rate limiter.
"""

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

from app.core.config import Settings


def build_chat_model(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=SecretStr(settings.openai_key_value),
        base_url=settings.openai_base_url,
        reasoning_effort=settings.openai_reasoning_effort,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    )


def build_embeddings(settings: Settings) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        dimensions=settings.openai_embedding_dimensions,
        openai_api_key=SecretStr(settings.openai_key_value),
        openai_api_base=settings.openai_base_url,
        request_timeout=settings.llm_timeout_seconds,
        max_retries=0,
        # Avoids tiktoken downloading encodings at runtime; KB chunks and tickets are short
        check_embedding_ctx_length=False,
    )
