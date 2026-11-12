"""Admin review of submitted KYC: the queue, one user's details, approve and reject.

Every decision goes through KycService.transition (row lock, allowed changes, kyc_events
with the admin's id). Every opening of a user's decrypted details is recorded in
kyc_access_logs.
"""

import logging
import uuid

from app.common.enums import KycStatus
from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.config import Settings
from app.modules.kyc.documents import KycDocumentService
from app.modules.kyc.matching import names_match
from app.modules.kyc.models import KycAccessLog
from app.modules.kyc.provider.base import mask_aadhaar
from app.modules.kyc.repository import KycRepository
from app.modules.kyc.schemas import (
    KycAccount,
    KycEventRead,
    KycReject,
    KycReviewDetail,
    KycReviewItem,
    KycReviewPage,
    KycSelfieSummary,
)
from app.modules.kyc.service import KycService
from app.modules.users.models import User

logger = logging.getLogger(__name__)


class CannotReviewOwnKycError(ForbiddenError):
    code = "cannot_review_own_kyc"
    message = "Another admin has to review your own KYC."


class KycReviewService:
    def __init__(
        self,
        repository: KycRepository,
        status: KycService,
        documents: KycDocumentService,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._status = status
        self._documents = documents
        self._settings = settings

    async def queue(self, status: KycStatus, *, limit: int, offset: int) -> KycReviewPage:
        rows = await self._repository.list_for_review(status, limit=limit, offset=offset)
        items = [
            KycReviewItem(
                user_id=user.id,
                full_name=user.full_name,
                phone=user.phone,
                kyc_status=user.kyc_status,
                submitted_at=submitted_at,
                aadhaar=mask_aadhaar(profile.aadhaar_last4) if profile else None,
                pan=profile.pan_masked if profile else None,
                selfie_match_score=selfie.match_score if selfie else None,
            )
            for user, profile, selfie, submitted_at in rows
        ]
        total = await self._repository.count_by_status(status)
        return KycReviewPage(items=items, total=total, limit=limit, offset=offset)

    async def detail(self, admin: User, user_id: uuid.UUID) -> KycReviewDetail:
        user = await self._repository.get_user(user_id)
        if user is None:
            raise NotFoundError("User not found", code="user_not_found")
        profile = await self._repository.get_profile(user_id)
        selfie = (
            await self._repository.get_selfie_by_id(profile.selfie_session_id)
            if profile and profile.selfie_session_id
            else None
        )
        details = self._documents.details(profile) if profile else None
        self._repository.add_access_log(KycAccessLog(admin_id=admin.id, user_id=user_id))
        await self._repository.flush()
        logger.info("Admin %s opened the KYC details of user %s", admin.id, user_id)

        return KycReviewDetail(
            account=KycAccount.model_validate(user),
            kyc_status=user.kyc_status,
            submitted_at=await self._repository.submitted_at(user_id),
            details=details,
            account_name_matches=names_match(user.full_name, details.name) if details else None,
            pan_source=profile.pan_source if profile else None,
            selfie=KycSelfieSummary(
                is_live=selfie.is_live,
                match_score=selfie.match_score,
                min_match_score=self._settings.kyc_selfie_min_match_score,
                completed_at=selfie.completed_at,
            )
            if selfie
            else None,
            can_decide=user.kyc_status is KycStatus.PENDING_REVIEW and user.id != admin.id,
            events=[
                KycEventRead.model_validate(event)
                for event in await self._repository.events(user_id)
            ],
        )

    async def approve(self, admin: User, user_id: uuid.UUID) -> KycReviewDetail:
        await self._lock_for_decision(admin, user_id, complete=True)
        await self._status.transition(
            user_id, KycStatus.VERIFIED, reason="approved", actor_id=admin.id
        )
        logger.info("Admin %s approved the KYC of user %s", admin.id, user_id)
        return await self.detail(admin, user_id)

    async def reject(self, admin: User, user_id: uuid.UUID, body: KycReject) -> KycReviewDetail:
        await self._lock_for_decision(admin, user_id, complete=False)
        profile = await self._repository.get_profile(user_id, for_update=True)
        if profile is not None:
            # Free the PAN: if it belongs to someone else, its owner must not be
            # blocked. A new attempt checks and claims it again.
            profile.pan_hash = None
            profile.pan_verified = False
            profile.pan_issue = None
            await self._repository.flush()
        await self._status.transition(
            user_id,
            KycStatus.REJECTED,
            reason=body.reason,
            actor_id=admin.id,
            note=body.note.strip() if body.note else None,
        )
        logger.info("Admin %s rejected the KYC of user %s (%s)", admin.id, user_id, body.reason)
        return await self.detail(admin, user_id)

    async def _lock_for_decision(self, admin: User, user_id: uuid.UUID, *, complete: bool) -> None:
        if admin.id == user_id:
            raise CannotReviewOwnKycError
        user = await self._repository.lock_user(user_id)
        if user is None:
            raise NotFoundError("User not found", code="user_not_found")
        if user.kyc_status is not KycStatus.PENDING_REVIEW:
            raise ConflictError(
                "This KYC isn't waiting for review (someone may have decided already).",
                code="kyc_not_pending_review",
            )
        if complete:
            profile = await self._repository.get_profile(user_id)
            if profile is None or not profile.pan_verified or profile.selfie_session_id is None:
                raise ConflictError(
                    "This KYC is missing a verified PAN or selfie.", code="kyc_incomplete"
                )
