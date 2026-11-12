from fastapi import APIRouter, status

from app.common.schemas import error_responses
from app.modules.auth.router import router as auth_router
from app.modules.cards.router import router as cards_router
from app.modules.health.router import router as health_router
from app.modules.kyc.admin_router import router as kyc_admin_router
from app.modules.kyc.router import router as kyc_router
from app.modules.payments.router import router as payments_router
from app.modules.users.router import router as users_router
from app.modules.webhooks.router import router as webhooks_router

# Mounted under settings.api_v1_prefix (/api/v1) in app.main.
# Any route can fail unexpectedly, so 500 is documented everywhere.
api_router = APIRouter(responses=error_responses(status.HTTP_500_INTERNAL_SERVER_ERROR))
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(kyc_router)
api_router.include_router(kyc_admin_router)
api_router.include_router(cards_router)
api_router.include_router(payments_router)
api_router.include_router(webhooks_router)
