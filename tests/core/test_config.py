import pytest
from pydantic import SecretStr, ValidationError

from tests.helpers import make_settings

PRODUCTION = {
    "environment": "production",
    "payment_gateway": "razorpay",
    "razorpay_key_id": "rzp_live_abc123",
    "razorpay_key_secret": SecretStr("live-key-secret"),
    "razorpay_webhook_secret": SecretStr("live-webhook-secret"),
    "kyc_provider": "none",
    "public_base_url": "https://api.dhansetu.example",
}


def test_connection_urls_are_masked() -> None:
    settings = make_settings(
        database_url=SecretStr("postgresql+asyncpg://user:hunter2@db/app"),
        redis_url=SecretStr("redis://:hunter2@cache:6379/0"),
    )

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())


def test_docs_disabled_in_production() -> None:
    assert make_settings(**PRODUCTION).docs_enabled is False
    assert make_settings(environment="staging").docs_enabled is True


def test_refresh_tokens_last_seven_days_by_default() -> None:
    assert make_settings().refresh_token_ttl_days == 7


def test_dev_otp_never_exposed_in_production() -> None:
    assert make_settings(debug=True, environment="local").expose_dev_otp is True
    assert make_settings(debug=False, environment="local").expose_dev_otp is False
    assert make_settings(**(PRODUCTION | {"debug": True})).expose_dev_otp is False


# ---- Payment gateway settings

RAZORPAY_TEST = {
    "payment_gateway": "razorpay",
    "razorpay_key_id": "rzp_test_abc123",
    "razorpay_key_secret": SecretStr("key-secret-hunter2"),
    "razorpay_webhook_secret": SecretStr("webhook-secret-hunter2"),
}


def test_fake_gateway_is_the_default_outside_production() -> None:
    assert make_settings().payment_gateway == "fake"


def test_fake_gateway_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="not allowed in production"):
        make_settings(environment="production", payment_gateway="fake")


@pytest.mark.parametrize(
    "missing", ["razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret"]
)
def test_razorpay_needs_every_key(missing: str) -> None:
    with pytest.raises(ValidationError, match="needs RAZORPAY_KEY_ID"):
        make_settings(**(RAZORPAY_TEST | {missing: None}))


def test_short_webhook_secret_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        make_settings(**(RAZORPAY_TEST | {"razorpay_webhook_secret": SecretStr("short")}))


def test_live_keys_only_in_production_and_production_only_with_live_keys() -> None:
    live = RAZORPAY_TEST | {"razorpay_key_id": "rzp_live_abc123"}

    for environment in ("local", "test", "staging"):
        with pytest.raises(ValidationError, match="rzp_test_"):
            make_settings(**(live | {"environment": environment}))
    with pytest.raises(ValidationError, match="rzp_live_"):
        make_settings(
            **(
                RAZORPAY_TEST
                | {
                    "environment": "production",
                    "kyc_provider": "none",
                    "public_base_url": "https://x.example",
                }
            )
        )
    production = live | {
        "environment": "production",
        "kyc_provider": "none",
        "public_base_url": "https://api.dhansetu.example",
    }
    assert make_settings(**production).payment_gateway == "razorpay"


def test_razorpay_secrets_are_masked() -> None:
    settings = make_settings(**RAZORPAY_TEST)

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())
