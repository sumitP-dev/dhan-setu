from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI

from app.main import create_app
from app.modules.kyc.provider.base import (
    AadhaarDetails,
    DigiLockerStatus,
    Gender,
    InvalidKycReferenceError,
    KycProviderError,
    KycProviderUnavailableError,
    KycSessionNotFoundError,
    PanDetails,
    PanStatus,
    SelfieStatus,
    is_individual_pan,
    mask_aadhaar,
    mask_pan,
    normalize_pan,
    require_session_id,
)
from app.modules.kyc.provider.fake import DEFAULT_AADHAAR, DEFAULT_PAN, FakeKycProvider
from app.modules.kyc.provider.unavailable import UnavailableKycProvider
from tests.helpers import make_settings

REDIRECT = "https://api.dhansetu.example/api/v1/kyc/digilocker/callback"


async def start(provider: FakeKycProvider, reference: str = "kyc_attempt_0001") -> str:
    session = await provider.create_digilocker_session(reference=reference, redirect_url=REDIRECT)
    return session.id


# ---- Helpers and data rules


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ABCPV1234F", "ABCPV1234F"),
        (" abcpv1234f ", "ABCPV1234F"),
        ("ABCPV1234", None),
        ("ABCZV1234F", None),  # Z is not a holder type
        ("1BCPV1234F", None),
        ("ABCPV12345", None),
    ],
)
def test_normalize_pan(raw: str, expected: str | None) -> None:
    assert normalize_pan(raw) == expected


def test_pan_holder_type_and_masks() -> None:
    assert is_individual_pan("ABCPV1234F")
    assert not is_individual_pan("ABCCV1234F")  # a company
    assert mask_pan("ABCPV1234F") == "ABCXX1234F"
    assert mask_aadhaar("1234") == "XXXX XXXX 1234"


@pytest.mark.parametrize("value", ["../orders", "short", "a" * 65, "dls_abc/def12", ""])
def test_session_ids_with_a_bad_shape_are_refused(value: str) -> None:
    with pytest.raises(InvalidKycReferenceError):
        require_session_id(value)


def test_aadhaar_details_hold_only_the_last_4_digits() -> None:
    with pytest.raises(ValueError, match="4 digits"):
        replace(DEFAULT_AADHAAR, last4="123456789012")
    field_names = set(AadhaarDetails.__dataclass_fields__)
    assert not {"number", "aadhaar_number", "xml", "photo"} & field_names


def test_birth_year_must_match_date_of_birth() -> None:
    with pytest.raises(ValueError, match="birth_year"):
        replace(DEFAULT_AADHAAR, birth_year=1990)
    year_only = replace(DEFAULT_AADHAAR, date_of_birth=None, birth_year=1990)
    assert year_only.birth_year == 1990


def test_pan_details_need_a_valid_pan() -> None:
    with pytest.raises(ValueError, match="PAN"):
        PanDetails(number="abcpv1234f", name="A", date_of_birth=None)


# ---- DigiLocker


async def test_digilocker_session_starts_pending_then_completes() -> None:
    provider = FakeKycProvider()
    session = await provider.create_digilocker_session(
        reference="kyc_attempt_0001", redirect_url=REDIRECT
    )

    assert session.consent_url.startswith("https://")
    assert session.expires_at > datetime.now(UTC)
    pending = await provider.fetch_digilocker_result(session.id)
    assert pending.status is DigiLockerStatus.PENDING
    assert pending.aadhaar is None

    provider.complete_digilocker(session.id)
    result = await provider.fetch_digilocker_result(session.id)
    assert result.status is DigiLockerStatus.COMPLETED
    assert result.aadhaar == DEFAULT_AADHAAR
    assert result.pan == DEFAULT_PAN


async def test_same_reference_reuses_the_open_session() -> None:
    provider = FakeKycProvider()
    first = await start(provider)
    assert await start(provider) == first
    assert await start(provider, "kyc_attempt_0002") != first


async def test_a_finished_session_is_not_reused() -> None:
    provider = FakeKycProvider()
    first = await start(provider)
    provider.end_digilocker(first, DigiLockerStatus.DENIED, "consent_denied")
    assert await start(provider) != first


async def test_user_can_complete_without_pan_in_digilocker() -> None:
    provider = FakeKycProvider()
    session_id = await start(provider)
    provider.complete_digilocker(session_id, pan=None)

    result = await provider.fetch_digilocker_result(session_id)
    assert result.aadhaar is not None
    assert result.pan is None


async def test_denied_session_reports_why() -> None:
    provider = FakeKycProvider()
    session_id = await start(provider)
    provider.end_digilocker(session_id, DigiLockerStatus.DENIED, "consent_denied")

    result = await provider.fetch_digilocker_result(session_id)
    assert result.status is DigiLockerStatus.DENIED
    assert result.failure_reason == "consent_denied"
    assert result.aadhaar is None


async def test_unfinished_session_expires() -> None:
    provider = FakeKycProvider()
    session_id = await start(provider)
    entry = provider.digilocker[session_id]
    entry.session = replace(entry.session, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    result = await provider.fetch_digilocker_result(session_id)
    assert result.status is DigiLockerStatus.EXPIRED


async def test_unknown_and_malformed_sessions() -> None:
    provider = FakeKycProvider()
    with pytest.raises(KycSessionNotFoundError):
        await provider.fetch_digilocker_result("dls_doesnotexist00")
    with pytest.raises(InvalidKycReferenceError):
        await provider.fetch_digilocker_result("../../admin")


@pytest.mark.parametrize("reference", ["", "short", "x" * 41, "has space here"])
async def test_bad_references_are_refused(reference: str) -> None:
    with pytest.raises(ValueError, match="reference"):
        await FakeKycProvider().create_digilocker_session(
            reference=reference, redirect_url=REDIRECT
        )


async def test_redirect_must_be_a_web_url() -> None:
    with pytest.raises(ValueError, match="redirect_url"):
        await FakeKycProvider().create_digilocker_session(
            reference="kyc_attempt_0001", redirect_url="javascript:alert(1)"
        )


# ---- PAN


async def test_known_pan_with_matching_details_is_valid() -> None:
    result = await FakeKycProvider().verify_pan(
        pan="abcpv1234f", name="Asha  Verma", date_of_birth=date(1994, 5, 17)
    )
    assert result.pan == "ABCPV1234F"
    assert result.status is PanStatus.VALID
    assert result.name_matches
    assert result.date_of_birth_matches is True


async def test_pan_with_other_name_or_date_does_not_match() -> None:
    result = await FakeKycProvider().verify_pan(
        pan="ABCPV1234F", name="Someone Else", date_of_birth=date(2000, 1, 1)
    )
    assert result.status is PanStatus.VALID
    assert not result.name_matches
    assert result.date_of_birth_matches is False


async def test_unknown_pan_is_invalid() -> None:
    result = await FakeKycProvider().verify_pan(
        pan="ZZZPZ9999Z", name="Asha Verma", date_of_birth=None
    )
    assert result.status is PanStatus.INVALID


async def test_malformed_pan_is_refused() -> None:
    with pytest.raises(ValueError, match="PAN"):
        await FakeKycProvider().verify_pan(pan="123", name="A", date_of_birth=None)


# ---- Selfie


async def test_selfie_needs_a_completed_digilocker_session() -> None:
    provider = FakeKycProvider()
    session_id = await start(provider)

    with pytest.raises(KycProviderError) as error:
        await provider.create_selfie_session(
            digilocker_session_id=session_id, reference="kyc_attempt_0001"
        )
    assert error.value.code == "digilocker_not_completed"


async def test_selfie_completes_with_liveness_and_match_score() -> None:
    provider = FakeKycProvider()
    session_id = await start(provider)
    provider.complete_digilocker(session_id)

    selfie = await provider.create_selfie_session(
        digilocker_session_id=session_id, reference="kyc_attempt_0001"
    )
    assert selfie.sdk_token
    assert (await provider.fetch_selfie_result(selfie.id)).status is SelfieStatus.PENDING

    provider.complete_selfie(selfie.id, is_live=True, match_score=88.0)
    result = await provider.fetch_selfie_result(selfie.id)
    assert result.status is SelfieStatus.COMPLETED
    assert result.is_live is True
    assert result.match_score == 88.0


async def test_selfie_can_fail_or_expire() -> None:
    provider = FakeKycProvider()
    session_id = await start(provider)
    provider.complete_digilocker(session_id)
    failed = await provider.create_selfie_session(
        digilocker_session_id=session_id, reference="kyc_attempt_0001"
    )
    expired = await provider.create_selfie_session(
        digilocker_session_id=session_id, reference="kyc_attempt_0001"
    )

    provider.fail_selfie(failed.id, "face_not_detected")
    entry = provider.selfies[expired.id]
    entry.session = replace(entry.session, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    assert (await provider.fetch_selfie_result(failed.id)).failure_reason == "face_not_detected"
    assert (await provider.fetch_selfie_result(expired.id)).status is SelfieStatus.EXPIRED


def test_match_score_must_be_0_to_100() -> None:
    with pytest.raises(ValueError, match="match_score"):
        FakeKycProvider().complete_selfie("sfs_doesnotmatter0", match_score=101)


# ---- Outages and webhooks


async def test_outage_raises_unavailable() -> None:
    provider = FakeKycProvider()
    provider.unavailable = True

    with pytest.raises(KycProviderUnavailableError):
        await start(provider)
    with pytest.raises(KycProviderUnavailableError):
        await provider.verify_pan(pan="ABCPV1234F", name="A", date_of_birth=None)


def test_webhook_signatures() -> None:
    provider = FakeKycProvider()
    body = b'{"event":"digilocker.completed","session_id":"dls_abcdefgh12345678"}'
    signature = provider.sign_webhook(body)

    assert provider.verify_webhook_signature(body, signature)
    assert not provider.verify_webhook_signature(body + b" ", signature)
    assert not provider.verify_webhook_signature(body, None)
    assert not provider.verify_webhook_signature(body, "0" * 64)
    assert not FakeKycProvider().verify_webhook_signature(body, signature)


# ---- KYC_PROVIDER=none


async def test_unavailable_provider_refuses_everything() -> None:
    provider = UnavailableKycProvider()

    with pytest.raises(KycProviderUnavailableError) as error:
        await provider.create_digilocker_session(
            reference="kyc_attempt_0001", redirect_url=REDIRECT
        )
    assert error.value.code == "kyc_not_available"
    with pytest.raises(KycProviderUnavailableError):
        await provider.fetch_digilocker_result("dls_abcdefgh12345678")
    with pytest.raises(KycProviderUnavailableError):
        await provider.verify_pan(pan="ABCPV1234F", name="A", date_of_birth=None)
    with pytest.raises(KycProviderUnavailableError):
        await provider.create_selfie_session(
            digilocker_session_id="dls_abcdefgh12345678", reference="kyc_attempt_0001"
        )
    with pytest.raises(KycProviderUnavailableError):
        await provider.fetch_selfie_result("sfs_abcdefgh12345678")
    assert not provider.verify_webhook_signature(b"{}", "0" * 64)


# ---- Settings and wiring


def test_app_uses_the_fake_provider_by_default(app: FastAPI) -> None:
    assert isinstance(app.state.kyc_provider, FakeKycProvider)


def test_app_uses_the_unavailable_provider_when_kyc_is_off() -> None:
    app = create_app(make_settings(kyc_provider="none"))
    assert isinstance(app.state.kyc_provider, UnavailableKycProvider)


def test_fake_provider_is_refused_in_production() -> None:
    production = {
        "environment": "production",
        "payment_gateway": "razorpay",
        "razorpay_key_id": "rzp_live_abc123",
        "razorpay_key_secret": "live-key-secret",
        "razorpay_webhook_secret": "live-webhook-secret",
        "public_base_url": "https://api.dhansetu.example",
    }
    with pytest.raises(ValueError, match="KYC_PROVIDER=fake"):
        make_settings(**production, kyc_provider="fake")
    assert make_settings(**production, kyc_provider="none").kyc_provider == "none"


def test_gender_values_match_aadhaar() -> None:
    assert [g.value for g in Gender] == ["M", "F", "T"]
