from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Used when no key is configured: the request still reaches OpenAI (and gets 401),
# as required by the "no API key" mode.
PLACEHOLDER_OPENAI_KEY = "sk-no-key-configured"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Momentum Support"
    log_level: str = "INFO"
    log_json: bool = True
    public_base_url: str = "http://localhost:8000"

    database_url: str = "postgresql+psycopg://support:support@localhost:5433/support"
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

    # Operator UI / API basic auth (disabled when empty)
    operator_username: str | None = None
    operator_password: SecretStr | None = None

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
