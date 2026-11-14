"""Stand-ins for DigiLocker's consent page and the selfie SDK, for local development only.

With KYC_PROVIDER=fake nobody's identity is checked, and the fake's consent links lead
nowhere. When `fake_kyc_pages_enabled` (fake provider, DEBUG on, not production), the
fake's links point here instead: the page plays the user in DigiLocker (share, share
without PAN, refuse) and then sends the browser to the real callback, exactly as
DigiLocker would. The app's development build shows its own stand-in for the selfie
SDK, which reports the chosen outcome to /kyc/dev/selfie/{sdk_token}, as the provider's
SDK would report to the provider. app/main.py only mounts this router in that case.
"""

import hashlib
import string
from enum import StrEnum
from html import escape
from typing import Annotated

from fastapi import APIRouter, Path, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.modules.kyc.dependencies import KycProviderDep
from app.modules.kyc.provider.base import DigiLockerStatus, KycSessionNotFoundError, PanDetails
from app.modules.kyc.provider.fake import DEFAULT_PAN, FakeKycProvider

router = APIRouter(prefix="/kyc/dev", tags=["kyc (development)"])


class FakeSelfie(StrEnum):
    PASSED = "pass"  # live, match score 92.5
    LOW_MATCH = "low_match"  # live, match score 40
    NOT_LIVE = "not_live"  # a photo held up to the camera
    NO_FACE = "no_face"  # the SDK couldn't find a face


class FakeConsent(StrEnum):
    ALLOW = "allow"  # shares Aadhaar (Asha Verma) and this session's test PAN
    ALLOW_WITHOUT_PAN = "allow_without_pan"  # Aadhaar only: the app asks for the PAN
    DENY = "deny"


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fake DigiLocker</title>
<style>
  body {{ font: 16px -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
         color: #1f2937; background: #f5f6f8; }}
  main {{ max-width: 420px; margin: 0 auto; background: #fff; border-radius: 12px;
          padding: 24px; border: 1px solid #e5e7eb; }}
  .tag {{ display: inline-block; font-size: 12px; font-weight: 600; color: #92400e;
          background: #fef3c7; border-radius: 6px; padding: 2px 8px; }}
  h1 {{ font-size: 20px; margin: 12px 0 8px; }}
  p {{ color: #4b5563; line-height: 1.45; }}
  button {{ display: block; width: 100%; font: inherit; font-weight: 600; padding: 14px;
            margin-top: 12px; border-radius: 10px; border: 1px solid #d1d5db;
            background: #fff; color: #1f2937; }}
  button.primary {{ background: #1e3a8a; border-color: #1e3a8a; color: #fff; }}
</style>
</head>
<body>
<main>
  <span class="tag">Development only</span>
  <h1>Fake DigiLocker</h1>
  <p>Dhan Setu wants your Aadhaar and PAN. Nothing is real here: choose what the user
  does in DigiLocker.</p>
  <form method="post" action="{action}?consent=allow">
    <button class="primary" type="submit">Allow (Aadhaar and PAN)</button>
  </form>
  <form method="post" action="{action}?consent=allow_without_pan">
    <button type="submit">Allow without PAN</button>
  </form>
  <form method="post" action="{action}?consent=deny">
    <button type="submit">Deny</button>
  </form>
  <p>If the app asks for a PAN, use <strong>{pan}</strong>.</p>
  <p>Session {session_id}</p>
</main>
</body>
</html>
"""


def _session_pan(fake: FakeKycProvider, session_id: str) -> str:
    """A test PAN for this session, known to the fake's Income Tax records. One PAN per
    account applies, so each local account needs its own (ASHPV + 4 digits + letter)."""
    digest = hashlib.sha256(session_id.encode()).digest()
    pan = f"ASHPV{int.from_bytes(digest[:4]) % 10_000:04d}{string.ascii_uppercase[digest[4] % 26]}"
    fake.pan_records[pan] = (DEFAULT_PAN.name, DEFAULT_PAN.date_of_birth)
    return pan


def _fake(provider: object) -> FakeKycProvider:
    if not isinstance(provider, FakeKycProvider):
        raise KycSessionNotFoundError
    return provider


@router.get("/digilocker/{session_id}", response_class=HTMLResponse, include_in_schema=False)
async def fake_consent_page(session_id: str, provider: KycProviderDep) -> HTMLResponse:
    fake = _fake(provider)
    # Raises (404/422) unless the fake knows this session.
    fake.digilocker_redirect_url(session_id)
    pan = _session_pan(fake, session_id)
    return HTMLResponse(
        _PAGE.format(action=escape(session_id), session_id=escape(session_id), pan=pan),
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/digilocker/{session_id}",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    include_in_schema=False,
)
async def fake_consent(
    session_id: str, consent: FakeConsent, provider: KycProviderDep
) -> RedirectResponse:
    fake = _fake(provider)
    redirect_url = fake.digilocker_redirect_url(session_id)
    if consent is FakeConsent.DENY:
        fake.end_digilocker(session_id, DigiLockerStatus.DENIED, "consent_denied")
    elif consent is FakeConsent.ALLOW_WITHOUT_PAN:
        _session_pan(fake, session_id)  # known to the fake, for typing in the app
        fake.complete_digilocker(session_id, pan=None)
    else:
        pan = _session_pan(fake, session_id)
        fake.complete_digilocker(
            session_id,
            pan=PanDetails(pan, DEFAULT_PAN.name, DEFAULT_PAN.date_of_birth),
        )
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/selfie/{sdk_token}", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def fake_selfie(
    sdk_token: Annotated[str, Path(min_length=16, max_length=128)],
    outcome: FakeSelfie,
    provider: KycProviderDep,
) -> None:
    fake = _fake(provider)
    session_id = fake.selfie_session_for_token(sdk_token)
    if outcome is FakeSelfie.PASSED:
        fake.complete_selfie(session_id)
    elif outcome is FakeSelfie.LOW_MATCH:
        fake.complete_selfie(session_id, match_score=40)
    elif outcome is FakeSelfie.NOT_LIVE:
        fake.complete_selfie(session_id, is_live=False)
    else:
        fake.fail_selfie(session_id, "face_not_detected")
