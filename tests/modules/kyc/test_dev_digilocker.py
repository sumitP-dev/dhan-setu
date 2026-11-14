"""The development-only stand-in for DigiLocker's consent page."""

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

    link = await follow_to_app(client, page_path(session["consent_url"]), "allow_without_pan")

    assert link == "dhansetu://kyc/digilocker?status=completed"
    kyc = (await client.get(KYC, headers=headers)).json()
    assert kyc["next_step"] == "pan"
    assert kyc["details"]["pan_issue"] == "pan_not_in_digilocker"


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
