import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_database_url_is_built_from_postgres_parts_with_escaping() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="",
        postgres_user="app",
        postgres_password="p@ss:w/rd#1",
        postgres_db="support",
        postgres_host="db",
    )

    assert settings.database_url == "postgresql+psycopg://app:p%40ss%3Aw%2Frd%231@db:5432/support"


def test_explicit_database_url_wins() -> None:
    url = "postgresql+psycopg://u:p@h:1/d"
    settings = Settings(_env_file=None, database_url=url, postgres_user="x")  # type: ignore[call-arg]
    assert settings.database_url == url


def test_missing_database_config_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DATABASE_URL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError, match="Database is not configured"):
        Settings(_env_file=None)  # type: ignore[call-arg]
