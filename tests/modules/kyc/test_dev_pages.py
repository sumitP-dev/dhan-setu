"""Development-only stand-ins for DigiLocker's consent page and the selfie SDK."""

import re
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.main import create_app
from app.modules.kyc.provider import create_kyc_provider
from app.modules.kyc.provider.fake import FakeKycProvider
from tests.helpers import bearer, make_settings, register

KYC = "/api/v1/kyc"
DEV = f"{KYC}/dev/digilocker"

Headers = dict[str, str]


@pytest.fixture
async def headers(client: AsyncClient) -> Headers:
    body = await register(client)
    return bearer(body["tokens"]["access_token"])


async def start(client: AsyncClient, headers: Headers) -> dict[str, Any]:
    response = await client.post(f"{KYC}/digilocker", headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def page_path(consent_url: str) -> str:
    return urlsplit(consent_url).path


async def follow_to_app(client: AsyncClient, path: str, consent: str) -> str:
    """Press a button on the fake page, follow the callback; returns the app link."""
    chosen = await client.post(path, params={"consent": consent})
    assert chosen.status_code == 303
    callback = urlsplit(chosen.headers["location"])
    assert callback.path == f"{KYC}/digilocker/callback"
    back = await client.get(f"{callback.path}?{callback.query}")
    assert back.status_code == 303
    location: str = back.headers["location"]
    return location


def test_enabled_only_for_the_fake_provider_with_debug_outside_production() -> None:
    assert make_settings().fake_kyc_pages_enabled
    assert not make_settings(debug=False).fake_kyc_pages_enabled
    assert not make_settings(kyc_provider="none", kyc_encryption_key=None).fake_kyc_pages_enabled


def test_consent_links_point_at_the_dev_page_only_when_enabled() -> None:
    enabled = create_kyc_provider(make_settings(public_base_url="http://localhost:8000"))
    assert isinstance(enabled, FakeKycProvider)
    assert enabled._consent_base_url == "http://localhost:8000/api/v1/kyc/dev/digilocker"

    disabled = create_kyc_provider(make_settings(debug=False))
    assert isinstance(disabled, FakeKycProvider)
    assert disabled._consent_base_url == "https://digilocker.fake.local/consent"


async def test_page_is_not_mounted_without_debug() -> None:
    app = create_app(make_settings(debug=False))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(path.startswith(DEV) for path in paths)


async def test_page_shows_the_choices(client: AsyncClient, headers: Headers) -> None:
    session = await start(client, headers)
    response = await client.get(page_path(session["consent_url"]))

    assert response.status_code == 200
    assert "Fake DigiLocker" in response.text
    assert "Development only" in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_allow_completes_digilocker_with_pan(client: AsyncClient, headers: Headers) -> None:
    session = await start(client, headers)

    link = await follow_to_app(client, page_path(session["consent_url"]), "allow")

    assert link == "dhansetu://kyc/digilocker?status=completed"
    kyc = (await client.get(KYC, headers=headers)).json()
    assert kyc["status"] == "in_progress"
    assert kyc["next_step"] == "selfie"
    assert kyc["details"]["pan_verified"] is True


async def test_allow_without_pan_asks_for_the_pan(client: AsyncClient, headers: Headers) -> None:
    session = await start(client, headers)
    page = (await client.get(page_path(session["consent_url"]))).text
    pan = re.search(r"use <strong>(ASHPV\d{4}[A-Z])</strong>", page)
    assert pan is not None

    link = await follow_to_app(client, page_path(session["consent_url"]), "allow_without_pan")

    assert link == "dhansetu://kyc/digilocker?status=completed"
    kyc = (await client.get(KYC, headers=headers)).json()
    assert kyc["next_step"] == "pan"
    assert kyc["details"]["pan_issue"] == "pan_not_in_digilocker"
    # The PAN the page showed is known to the fake Income Tax records.
    typed = await client.post(f"{KYC}/pan", headers=headers, json={"pan": pan.group(1)})
    assert typed.json()["details"]["pan_verified"] is True


async def test_each_account_gets_its_own_test_pan(client: AsyncClient, headers: Headers) -> None:
    session = await start(client, headers)
    await follow_to_app(client, page_path(session["consent_url"]), "allow")
    other = bearer(
        (await register(client, email="ravi@example.com", phone="9123456780"))["tokens"][
            "access_token"
        ]
    )
    other_session = await start(client, other)
    await follow_to_app(client, page_path(other_session["consent_url"]), "allow")

    first = (await client.get(KYC, headers=headers)).json()["details"]
    second = (await client.get(KYC, headers=other)).json()["details"]
    assert first["pan_verified"] is True
    assert second["pan_verified"] is True
    assert first["pan"] != second["pan"]


async def test_deny_reports_the_refusal(client: AsyncClient, headers: Headers) -> None:
    session = await start(client, headers)

    link = await follow_to_app(client, page_path(session["consent_url"]), "deny")

    assert link == "dhansetu://kyc/digilocker?status=denied"
    polled = (await client.get(f"{KYC}/digilocker/{session['id']}", headers=headers)).json()
    assert polled["failure_reason"] == "consent_denied"


async def test_unknown_session_or_choice_is_refused(client: AsyncClient) -> None:
    assert (await client.get(f"{DEV}/dls_doesnotexist0000")).status_code == 404
    assert (await client.get(f"{DEV}/not a session")).status_code == 422


async def test_bad_choice_is_refused(client: AsyncClient, headers: Headers) -> None:
    session = await start(client, headers)
    response = await client.post(page_path(session["consent_url"]), params={"consent": "maybe"})
    assert response.status_code == 422


async def test_real_provider_has_no_dev_page(app: FastAPI, client: AsyncClient) -> None:
    app.state.kyc_provider = object()
    assert (await client.get(f"{DEV}/dls_doesnotexist0000")).status_code == 404


# ---- Selfie stand-in


async def ready_for_selfie(client: AsyncClient, headers: Headers) -> dict[str, Any]:
    """DigiLocker (with PAN) done through the dev page; returns the started selfie."""
    session = await start(client, headers)
    await follow_to_app(client, page_path(session["consent_url"]), "allow")
    response = await client.post(f"{KYC}/selfie", headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def selfie_outcome(client: AsyncClient, headers: Headers, outcome: str) -> dict[str, Any]:
    selfie = await ready_for_selfie(client, headers)
    chosen = await client.post(
        f"{KYC}/dev/selfie/{selfie['sdk_token']}", params={"outcome": outcome}
    )
    assert chosen.status_code == 204, chosen.text
    body: dict[str, Any] = (
        await client.get(f"{KYC}/selfie/{selfie['id']}", headers=headers)
    ).json()
    return body


async def test_selfie_pass_moves_kyc_to_submit(client: AsyncClient, headers: Headers) -> None:
    result = await selfie_outcome(client, headers, "pass")

    assert result["status"] == "completed"
    kyc = (await client.get(KYC, headers=headers)).json()
    assert kyc["next_step"] == "submit"
    assert kyc["details"]["selfie_verified"] is True


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("low_match", "face_mismatch"),
        ("not_live", "selfie_not_live"),
        ("no_face", "face_not_detected"),
    ],
)
async def test_selfie_failures(
    client: AsyncClient, headers: Headers, outcome: str, reason: str
) -> None:
    result = await selfie_outcome(client, headers, outcome)

    assert result["status"] == "failed"
    assert result["failure_reason"] == reason
    assert result["failure_message"]


async def test_selfie_needs_a_known_token(client: AsyncClient) -> None:
    unknown = await client.post(f"{KYC}/dev/selfie/{'x' * 43}", params={"outcome": "pass"})
    assert unknown.status_code == 404
    short = await client.post(f"{KYC}/dev/selfie/abc", params={"outcome": "pass"})
    assert short.status_code == 422
