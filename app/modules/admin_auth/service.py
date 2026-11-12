"""Admin portal sign-in: password, then an OTP to the admin's phone.

Only active accounts with the admin role get past the password step; everyone else
sees the same "invalid credentials" error, so the endpoint doesn't reveal who is an
admin. Each sign-in is an AdminSession; access tokens name it, so ending the session
ends access at once.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import OtpPurpose, UserRole, UserStatus
from app.common.exceptions import ConflictError, ForbiddenError, UnauthorizedError
from app.core.config import Settings
from app.core.database import utcnow
from app.core.rate_limit import AttemptLimiter, Cooldown
from app.core.security import (
    ADMIN_AUDIENCE,
    burn_password_check,
    create_access_token,
    create_admin_challenge,
    decode_access_token,
    decode_admin_challenge,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from app.modules.admin_auth.models import MAX_USER_AGENT_LENGTH, AdminSession
from app.modules.admin_auth.repository import AdminSessionRepository
from app.modules.admin_auth.schemas import (
    AdminChallenge,
    AdminLoginRequest,
    AdminRead,
    AdminSessionResponse,
    AdminVerifyRequest,
)
from app.modules.auth.otp import OtpService
from app.modules.users.models import User
from app.modules.users.repository import UserRepository

logger = logging.getLogger(__name__)

# Two tabs refreshing with the same cookie at the same moment is normal; the slower
# one is told to retry rather than treated as a stolen cookie.
REFRESH_GRACE = timedelta(seconds=10)
_PHONE_HINT_DIGITS = 4


def _invalid_credentials() -> UnauthorizedError:
    return UnauthorizedError("Invalid email or password", code="invalid_credentials")


def session_ended() -> UnauthorizedError:
    return UnauthorizedError("Your session has ended. Sign in again.", code="session_expired")


def is_active_admin(user: User | None) -> bool:
    return user is not None and user.role is UserRole.ADMIN and user.status is UserStatus.ACTIVE


def phone_hint(phone: str) -> str:
    """+919876543210 -> +91 XXXXXX3210"""
    return f"{phone[:3]} XXXXXX{phone[-_PHONE_HINT_DIGITS:]}"


@dataclass(frozen=True, slots=True)
class SignedIn:
    response: AdminSessionResponse
    refresh_token: str
    expires_at: datetime


class AdminAuthService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: AdminSessionRepository,
        otp: OtpService,
        settings: Settings,
        login_limiter: AttemptLimiter,
        otp_cooldown: Cooldown,
    ) -> None:
        self._session = session
        self._users = users
        self._sessions = sessions
        self._otp = otp
        self._settings = settings
        self._login_limiter = login_limiter
        self._otp_cooldown = otp_cooldown

    # ---- Signing in

    async def login(self, data: AdminLoginRequest) -> AdminChallenge:
        user = await self._users.get_by_email(data.email)
        limiter_key = str(user.id) if user else data.email
        await self._login_limiter.ensure_allowed(limiter_key)

        if user is None:
            await burn_password_check(data.password, rounds=self._settings.bcrypt_rounds)
        password_ok = user is not None and await verify_password(data.password, user.password_hash)
        if user is None or not password_ok or not is_active_admin(user):
            await self._login_limiter.record_failure(limiter_key)
            logger.warning("Failed admin sign-in for %s", user.id if user else "unknown email")
            raise _invalid_credentials()

        await self._login_limiter.reset(limiter_key)
        await self._otp_cooldown.start(f"{OtpPurpose.ADMIN_LOGIN}:{user.phone}")
        code = await self._otp.issue(user.phone, OtpPurpose.ADMIN_LOGIN)
        ttl = timedelta(minutes=self._settings.otp_ttl_minutes)
        return AdminChallenge(
            challenge_token=create_admin_challenge(
                user.id, secret=self._settings.jwt_secret_key, ttl=ttl
            ),
            phone_hint=phone_hint(user.phone),
            expires_in=int(ttl.total_seconds()),
            resend_after=self._settings.otp_resend_cooldown_seconds,
            dev_otp=code if self._settings.expose_dev_otp else None,
        )

    async def verify(
        self, data: AdminVerifyRequest, *, ip_address: str | None, user_agent: str | None
    ) -> SignedIn:
        user_id = decode_admin_challenge(data.challenge_token, secret=self._settings.jwt_secret_key)
        user = await self._users.get_by_id(user_id)
        if user is None or not is_active_admin(user):
            raise _invalid_credentials()
        await self._otp.check(user.phone, OtpPurpose.ADMIN_LOGIN, data.code)

        now = utcnow()
        refresh_token = generate_refresh_token()
        admin_session = AdminSession(
            id=uuid.uuid4(),
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(refresh_token),
            rotated_at=now,
            expires_at=now + timedelta(hours=self._settings.admin_session_max_hours),
            idle_expires_at=now,
            ip_address=ip_address[:45] if ip_address else None,
            user_agent=user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None,
        )
        admin_session.idle_expires_at = self._idle_expiry(admin_session, now)
        self._sessions.add(admin_session)
        user.last_login_at = now
        await self._sessions.flush()
        logger.info("Admin %s signed in (session %s)", user.id, admin_session.id)
        return self._signed_in(user, admin_session, refresh_token)

    # ---- Keeping the session going

    async def refresh(self, refresh_token: str | None) -> SignedIn:
        if not refresh_token:
            raise session_ended()
        now = utcnow()
        token_hash = hash_refresh_token(refresh_token)
        admin_session = await self._sessions.get_by_refresh_hash(token_hash)
        if admin_session is None:
            await self._handle_old_token(token_hash, now)
            raise session_ended()
        if not admin_session.is_active(now):
            raise session_ended()
        user = await self._users.get_by_id(admin_session.user_id)
        if user is None or not is_active_admin(user):
            await self._end(admin_session.id, now, "not_admin")
            raise session_ended()

        new_token = generate_refresh_token()
        admin_session.previous_refresh_token_hash = admin_session.refresh_token_hash
        admin_session.refresh_token_hash = hash_refresh_token(new_token)
        admin_session.rotated_at = now
        admin_session.idle_expires_at = self._idle_expiry(admin_session, now)
        await self._sessions.flush()
        return self._signed_in(user, admin_session, new_token)

    async def logout(self, refresh_token: str | None) -> None:
        """Ends the session. Safe to retry; unknown cookies are ignored."""
        if not refresh_token:
            return
        token_hash = hash_refresh_token(refresh_token)
        admin_session = await self._sessions.get_by_refresh_hash(
            token_hash
        ) or await self._sessions.get_by_previous_hash(token_hash)
        if admin_session is not None:
            await self._sessions.revoke(admin_session.id, now=utcnow(), reason="logout")
            logger.info("Admin %s signed out (session %s)", admin_session.user_id, admin_session.id)

    # ---- Checking each request

    async def authenticate(self, access_token: str) -> tuple[User, AdminSession]:
        claims = decode_access_token(
            access_token, secret=self._settings.jwt_secret_key, audience=ADMIN_AUDIENCE
        )
        if claims.session_id is None:
            raise session_ended()
        admin_session = await self._sessions.get(claims.session_id)
        if (
            admin_session is None
            or admin_session.user_id != claims.user_id
            or not admin_session.is_active(utcnow())
        ):
            raise session_ended()
        user = await self._users.get_by_id(claims.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise session_ended()
        if user.role is not UserRole.ADMIN:
            raise ForbiddenError("This account is no longer an admin.", code="not_admin")
        return user, admin_session

    # ---- Helpers

    def _idle_expiry(self, admin_session: AdminSession, now: datetime) -> datetime:
        idle = now + timedelta(minutes=self._settings.admin_session_idle_minutes)
        return min(idle, admin_session.expires_at)

    async def _handle_old_token(self, token_hash: str, now: datetime) -> None:
        admin_session = await self._sessions.get_by_previous_hash(token_hash)
        if admin_session is None or admin_session.revoked_at is not None:
            return
        if now - admin_session.rotated_at <= REFRESH_GRACE:
            raise ConflictError(
                "Another tab just refreshed the session. Try again.", code="refresh_in_progress"
            )
        # An old refresh token used again: the cookie was probably copied.
        await self._end(admin_session.id, now, "reused_token")
        logger.warning(
            "Reused admin refresh token; ended session %s of admin %s",
            admin_session.id,
            admin_session.user_id,
        )

    async def _end(self, session_id: uuid.UUID, now: datetime, reason: str) -> None:
        await self._sessions.revoke(session_id, now=now, reason=reason)
        # Commit now: the request is about to fail, and a failing request is rolled back.
        await self._session.commit()

    def _signed_in(self, user: User, admin_session: AdminSession, refresh_token: str) -> SignedIn:
        ttl = timedelta(minutes=self._settings.admin_access_token_ttl_minutes)
        return SignedIn(
            response=AdminSessionResponse(
                admin=AdminRead.model_validate(user),
                access_token=create_access_token(
                    user.id,
                    secret=self._settings.jwt_secret_key,
                    ttl=ttl,
                    audience=ADMIN_AUDIENCE,
                    session_id=admin_session.id,
                ),
                expires_in=int(ttl.total_seconds()),
                session_expires_at=admin_session.expires_at,
            ),
            refresh_token=refresh_token,
            expires_at=admin_session.expires_at,
        )
