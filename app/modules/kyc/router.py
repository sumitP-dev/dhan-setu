import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, status
from fastapi.responses import RedirectResponse

from app.common.schemas import error_responses
from app.core.config import AppSettings
from app.modules.auth.dependencies import CurrentUser
from app.modules.kyc.dependencies import KycDocumentServiceDep, KycSelfieServiceDep
from app.modules.kyc.documents import FAILURE_MESSAGES
from app.modules.kyc.models import KycDigiLockerSession, KycSelfieSession
from app.modules.kyc.schemas import (
    DigiLockerSessionRead,
    KycRead,
    PanSubmit,
    SelfieSessionRead,
    SelfieSessionStart,
)
from app.modules.kyc.selfie import SELFIE_FAILURE_MESSAGES

router = APIRouter(prefix="/kyc", tags=["kyc"])

_AUTH_ERRORS = (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
_PROVIDER_ERRORS = (status.HTTP_502_BAD_GATEWAY, status.HTTP_503_SERVICE_UNAVAILABLE)


def _session_read(session: KycDigiLockerSession) -> DigiLockerSessionRead:
    reason = session.failure_reason
    return DigiLockerSessionRead(
        id=session.id,
        status=session.status,
        consent_url=session.consent_url,
        expires_at=session.expires_at,
        failure_reason=reason,
        failure_message=FAILURE_MESSAGES.get(reason) if reason else None,
    )


def _selfie_read(selfie: KycSelfieSession) -> SelfieSessionRead:
    reason = selfie.failure_reason
    return SelfieSessionRead(
        id=selfie.id,
        status=selfie.status,
        failure_reason=reason,
        failure_message=SELFIE_FAILURE_MESSAGES.get(reason) if reason else None,
        expires_at=selfie.expires_at,
    )


@router.get("", responses=error_responses(*_AUTH_ERRORS))
async def get_kyc(user: CurrentUser, service: KycDocumentServiceDep) -> KycRead:
    """KYC status, what to do next, and the details from DigiLocker for the user to
    check. Cards, payments, UPI and bills need "verified"."""
    return await service.read(user)


@router.post(
    "/digilocker",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        *_AUTH_ERRORS,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        *_PROVIDER_ERRORS,
    ),
)
async def start_digilocker(
    user: CurrentUser, service: KycDocumentServiceDep
) -> DigiLockerSessionRead:
    """Start (or reuse the open) DigiLocker session. Open `consent_url` in a browser."""
    return _session_read(await service.start_digilocker(user))


@router.get(
    "/digilocker/callback",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def digilocker_callback(
    state: Annotated[str, Query(max_length=64)],
    service: KycDocumentServiceDep,
    settings: AppSettings,
) -> RedirectResponse:
    """DigiLocker sends the user's browser here. No sign-in: the one-time `state`
    identifies the session. Redirects to the app with only the outcome."""
    outcome = await service.handle_callback(state)
    return RedirectResponse(
        f"{settings.kyc_app_return_url}?{urlencode({'status': outcome})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/digilocker/{session_id}",
    responses=error_responses(
        *_AUTH_ERRORS,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        *_PROVIDER_ERRORS,
    ),
)
async def get_digilocker(
    session_id: uuid.UUID, user: CurrentUser, service: KycDocumentServiceDep
) -> DigiLockerSessionRead:
    """Session status; checks with the provider while it's open. Poll after the browser
    closes."""
    return _session_read(await service.get_digilocker(user, session_id))


@router.post(
    "/pan",
    responses=error_responses(
        *_AUTH_ERRORS,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        *_PROVIDER_ERRORS,
    ),
)
async def submit_pan(body: PanSubmit, user: CurrentUser, service: KycDocumentServiceDep) -> KycRead:
    """For users whose PAN isn't in DigiLocker (or didn't match). Checked against the
    Income Tax records and the Aadhaar name and date of birth."""
    return await service.submit_pan(user, body.pan)


@router.post(
    "/selfie",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        *_AUTH_ERRORS,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        *_PROVIDER_ERRORS,
    ),
)
async def start_selfie(user: CurrentUser, service: KycSelfieServiceDep) -> SelfieSessionStart:
    """Start (or reuse the open) selfie session after DigiLocker and PAN. Pass
    `sdk_token` to the provider's selfie SDK, then poll GET /kyc/selfie/{id}."""
    selfie, sdk_token = await service.start(user.id)
    return SelfieSessionStart(**_selfie_read(selfie).model_dump(), sdk_token=sdk_token)


@router.get(
    "/selfie/{selfie_id}",
    responses=error_responses(
        *_AUTH_ERRORS,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        *_PROVIDER_ERRORS,
    ),
)
async def get_selfie(
    selfie_id: uuid.UUID, user: CurrentUser, service: KycSelfieServiceDep
) -> SelfieSessionRead:
    """Selfie result; checks liveness and the face match with the provider while the
    session is open. Poll after the SDK closes."""
    return _selfie_read(await service.get(user.id, selfie_id))
