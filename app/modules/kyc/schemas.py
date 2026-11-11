from app.common.enums import KycStatus
from app.common.schemas import ResponseModel


class KycRead(ResponseModel):
    status: KycStatus
    # True when the user can start or continue KYC (not while it's under review).
    can_start: bool
