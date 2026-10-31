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


def test_refresh_tokens_last_seven_days_by_default() -> None:
    assert make_settings().refresh_token_ttl_days == 7


def test_dev_otp_never_exposed_in_production() -> None:
    assert make_settings(debug=True, environment="local").expose_dev_otp is True
    assert make_settings(debug=False, environment="local").expose_dev_otp is False
    assert make_settings(debug=True, environment="production").expose_dev_otp is False
