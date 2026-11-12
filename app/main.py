from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.common.exceptions import register_exception_handlers
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.encryption import FieldCipher, decode_master_key
from app.core.logging import configure_logging
from app.core.redis import create_redis_client
from app.modules.kyc.provider import create_kyc_provider
from app.modules.notifications.service import ConsoleSmsSender
from app.modules.payments.gateway import create_payment_gateway


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await app.state.payment_gateway.aclose()
        await app.state.kyc_provider.aclose()
        await app.state.redis.aclose()
        await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory. Run with: uvicorn app.main:create_app --factory"""
    settings = settings or get_settings()
    configure_logging(debug=settings.debug)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=f"{settings.api_v1_prefix}/openapi.json" if settings.docs_enabled else None,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
    )

    # Clients are created here but connect lazily, so building the app never touches the network.
    app.state.settings = settings
    app.state.engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.redis = create_redis_client(settings)
    app.state.sms_sender = ConsoleSmsSender(log_codes=settings.environment != "production")
    app.state.payment_gateway = create_payment_gateway(settings)
    app.state.kyc_provider = create_kyc_provider(settings)
    kyc_key = settings.kyc_encryption_key
    app.state.field_cipher = (
        FieldCipher(decode_master_key(kyc_key.get_secret_value())) if kyc_key else None
    )

    browser_origins = list(dict.fromkeys([*settings.cors_origins, *settings.admin_origins]))
    if browser_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=browser_origins,
            # The admin portal sends its session cookie to /admin/auth/refresh.
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Admin-Client"],
        )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app
