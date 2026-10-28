from pydantic import SecretStr

from tests.helpers import make_settings


def test_connection_urls_are_masked() -> None:
    settings = make_settings(
        database_url=SecretStr("postgresql+asyncpg://user:hunter2@db/app"),
        redis_url=SecretStr("redis://:hunter2@cache:6379/0"),
    )

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())


def test_docs_disabled_in_production() -> None:
    assert make_settings(environment="production").docs_enabled is False
    assert make_settings(environment="staging").docs_enabled is True
