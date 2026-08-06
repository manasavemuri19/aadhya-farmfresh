"""Application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db import base as db

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 120


async def _housekeeping(app: FastAPI) -> None:
    """Background maintenance.

    Two jobs on one timer: reclaim stock held by abandoned checkouts, and
    delete expired OTP challenges and idempotency keys. Postgres has no TTL
    index, so that cleanup is explicit rather than automatic.

    A single in-process loop is right for one or two instances. If this ever
    runs on many replicas, move it behind an advisory lock or a scheduled job
    so the work is not duplicated.
    """
    from app.db.base import session_scope
    from app.payments import get_payment_provider
    from app.repositories.idempotency import IdempotencyRepository
    from app.repositories.orders import OrderRepository
    from app.repositories.otp import OtpRepository
    from app.repositories.products import ProductRepository
    from app.services.order_service import OrderService

    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            async with session_scope() as session:
                service = OrderService(
                    ProductRepository(session),
                    OrderRepository(session),
                    IdempotencyRepository(session),
                    get_payment_provider(),
                )
                await service.release_expired_holds()
                await OtpRepository(session).delete_expired()
                await IdempotencyRepository(session).delete_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("housekeeping iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    log.info("starting api", extra={"env": settings.env})

    await db.connect()
    app.state.sweeper = asyncio.create_task(_housekeeping(app))

    try:
        yield
    finally:
        app.state.sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.sweeper
        await db.disconnect()
        log.info("api stopped")


app = FastAPI(
    title="Aadya Pickles & Dairy API",
    version="1.0.0",
    lifespan=lifespan,
    # Interactive docs are useful in development and an information leak in prod.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,   # tokens travel in the Authorization header
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
    expose_headers=["X-Request-Id"],
    max_age=600,
)


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Some of those details are not quite right.",
                "details": {"fields": exc.errors()},
            }
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a stack trace to a client. The request id ties the customer's
    screenshot to the full trace in the logs."""
    log.exception("unhandled error", extra={"path": request.url.path})
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Something went wrong on our side. Try again in a moment.",
                "request_id": getattr(request.state, "request_id", "-"),
            }
        },
    )


app.include_router(api_router, prefix="/v1")


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "aadhya-api", "version": app.version}
