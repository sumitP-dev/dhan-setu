"""A stand-in for DigiLocker's consent page, for local development only.

With KYC_PROVIDER=fake nobody's identity is checked, and the fake's consent links lead
nowhere. When `fake_kyc_pages_enabled` (fake provider, DEBUG on, not production), the
fake's links point here instead: the page plays the user in DigiLocker (share, share
without PAN, refuse) and then sends the browser to the real callback, exactly as
DigiLocker would. app/main.py only mounts this router in that case.
"""

from enum import StrEnum
from html import escape

from fastapi import APIRouter, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.modules.kyc.dependencies import KycProviderDep
from app.modules.kyc.provider.base import DigiLockerStatus, KycSessionNotFoundError
from app.modules.kyc.provider.fake import FakeKycProvider

router = APIRouter(prefix="/kyc/dev/digilocker", tags=["kyc (development)"])


class FakeConsent(StrEnum):
    ALLOW = "allow"  # shares Aadhaar and PAN (Asha Verma, ABCPV1234F)
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
  <p>Session {session_id}</p>
</main>
</body>
</html>
"""


def _fake(provider: object) -> FakeKycProvider:
    if not isinstance(provider, FakeKycProvider):
        raise KycSessionNotFoundError
    return provider


@router.get("/{session_id}", response_class=HTMLResponse, include_in_schema=False)
async def fake_consent_page(session_id: str, provider: KycProviderDep) -> HTMLResponse:
    # Raises (404/422) unless the fake knows this session.
    _fake(provider).digilocker_redirect_url(session_id)
    return HTMLResponse(
        _PAGE.format(action=escape(session_id), session_id=escape(session_id)),
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/{session_id}",
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
        fake.complete_digilocker(session_id, pan=None)
    else:
        fake.complete_digilocker(session_id)
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)
