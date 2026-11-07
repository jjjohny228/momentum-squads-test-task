from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Used when no key is configured: the request still reaches OpenAI (and gets 401),
# as required by the "no API key" mode.
PLACEHOLDER_OPENAI_KEY = "sk-no-key-configured"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Momentum Support"
    # Product the support team answers for: used in the LLM prompt and reply signatures
    product_name: str = "Pulsar Analytics"
    log_level: str = "INFO"
    log_json: bool = True
    public_base_url: str = "http://localhost:8000"

    # Either a full DATABASE_URL or the POSTGRES_* parts (the URL is built with proper escaping,
    # so passwords may contain any characters). No credentials are hard-coded.
    database_url: str = ""
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    postgres_db: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Webhook
    webhook_secret: SecretStr | None = None
    reply_url_allowed_hosts: list[str] = Field(default_factory=list)

    # OpenAI / LangChain
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: str | None = "low"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1536
    llm_timeout_seconds: float = 60.0
    llm_max_attempts: int = 3

    # Keyless mode: static answer with realistic latency
    fake_llm_delay_min: float = 5.0
    fake_llm_delay_max: float = 30.0

    # Global provider rate limit (shared by all workers, stored in Postgres)
    rate_limit_rpm: int = 60
    rate_limit_include_embeddings: bool = True

    # Retrieval
    kb_collection: str = "knowledge_base"
    retrieval_top_k: int = 3
    retrieval_max_distance: float = 0.75

    # Worker / jobs
    worker_concurrency: int = 30
    worker_send_concurrency: int = 10
    worker_poll_interval: float = 1.0
    job_lease_seconds: int = 120
    job_max_attempts: int = 6
    job_backoff_base_seconds: float = 5.0
    job_backoff_max_seconds: float = 300.0

    # Helpdesk client
    helpdesk_timeout_seconds: float = 10.0

    # LangSmith tracing (threads = helpdesk tickets)
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "momentum-support"
    langsmith_endpoint: str | None = None

    # Operator UI / API basic auth (disabled when empty)
    operator_username: str | None = None
    operator_password: SecretStr | None = None

    @model_validator(mode="after")
    def _build_database_url(self) -> Self:
        if self.database_url:
            return self
        if not (self.postgres_user and self.postgres_password and self.postgres_db):
            raise ValueError(
                "Database is not configured: set DATABASE_URL or "
                "POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB"
            )
        self.database_url = URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)
        return self

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value())

    @property
    def openai_key_value(self) -> str:
        if self.openai_api_key and self.openai_api_key.get_secret_value():
            return self.openai_api_key.get_secret_value()
        return PLACEHOLDER_OPENAI_KEY


@lru_cache
def get_settings() -> Settings:
    return Settings()
