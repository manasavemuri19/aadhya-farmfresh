"""Application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1.router import API_V1_PREFIX, api_router
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db import base as db

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 120

# AAD-REL-003: a non-blocking, transaction-scoped advisory lock — released
# automatically at the sweep's own commit/rollback, no manual unlock needed.
# Every replica still runs this loop on its own timer; only the one that
# wins the lock in a given iteration does the work, so concurrent replicas
# no longer collide on the same expired rows. The key is an arbitrary
# constant unique to this job (distinct from alembic/env.py's migration
# lock key).
_SWEEP_ADVISORY_LOCK_KEY = 88230001


async def _run_sweep_once() -> None:
    """One housekeeping pass — reclaim stock held by abandoned checkouts,
    process any refunds queued against the gateway (AAD-PAY-003 — cancel and
    force-refund commit the order state change immediately but never call
    Razorpay inline, so this is where that call actually happens;
    AAD-PAY-005's amount-mismatch refunds are the same deferred-gateway-call
    shape), delete expired idempotency keys and refresh-token rows
    (AAD-SEC-002 — once a row's `expires_at` has passed it has no further
    purpose, not even for reuse detection), and redact webhook payloads past
    retention (AAD-DATA-011 — the `(provider, event_id)` dedupe row stays
    forever, only the raw gateway payload is cleared). Postgres has no TTL
    index, so that cleanup is explicit rather than automatic.

    Split out from the loop in `_housekeeping` below so it can run
    immediately at startup as well as on the timer, and so a test can call
    it directly without needing to run (or cancel) an infinite loop.
    """
    from sqlalchemy import text

    from app.db.base import session_scope
    from app.payments import get_payment_provider
    from app.repositories.idempotency import IdempotencyRepository
    from app.repositories.orders import OrderRepository
    from app.repositories.products import ProductRepository
    from app.repositories.refresh_tokens import RefreshTokenRepository
    from app.repositories.support import SupportRepository
    from app.services.order_service import OrderService

    async with session_scope() as session:
        got_lock = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": _SWEEP_ADVISORY_LOCK_KEY},
            )
        ).scalar_one()
        if not got_lock:
            log.info("housekeeping sweep skipped: another replica holds the lock")
            return

        service = OrderService(
            ProductRepository(session),
            OrderRepository(session),
            IdempotencyRepository(session),
            get_payment_provider(),
            support=SupportRepository(session),
        )
        released = await service.release_expired_holds()
        refunded = await service.process_pending_refunds()
        mismatched = await service.process_amount_mismatches()
        keys_deleted = await IdempotencyRepository(session).delete_expired()
        tokens_deleted = await RefreshTokenRepository(session).delete_expired()
        payloads_redacted = await service.orders.redact_expired_webhook_payloads()

    # AAD-REL-003: there's no metrics system anywhere in this codebase to
    # emit an actual gauge to (confirmed — no prometheus_client or
    # equivalent in pyproject.toml), so this structured line is the
    # equivalent that fits how this app is actually observed: a log-based
    # alert on "no 'housekeeping sweep completed' line in N minutes" gives
    # you the same "has this gone stale" signal the finding asks for,
    # without adding a metrics dependency this batch didn't otherwise need.
    log.info(
        "housekeeping sweep completed",
        extra={
            "holds_released": released,
            "refunds_processed": refunded,
            "mismatches_processed": mismatched,
            "idempotency_keys_deleted": keys_deleted,
            "refresh_tokens_deleted": tokens_deleted,
            "webhook_payloads_redacted": payloads_redacted,
        },
    )


async def _housekeeping(app: FastAPI) -> None:
    """Runs `_run_sweep_once` on a timer for the life of the process.

    AAD-REL-003: this used to `await asyncio.sleep(SWEEP_INTERVAL_SECONDS)`
    *before* the first sweep, so nothing was swept until 2 minutes after
    boot — and the timer reset on every deploy and restart, so deploying
    more often than that during an incident meant expired stock holds were
    never released at all. The first pass now runs immediately; the sleep
    moves to the end of the loop, after the work, so it only ever delays the
    *next* sweep, never the first one.
    """
    while True:
        try:
            await _run_sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("housekeeping iteration failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


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
# AAD-SEC-013: added last so it is the outermost middleware (Starlette wraps
# in reverse registration order) — the body is rejected before CORS, request
# context, or anything else touches it. Pure ASGI, not BaseHTTPMiddleware:
# by the time a BaseHTTPMiddleware's dispatch() runs, Starlette may already
# be consuming the body, so this wraps `receive` directly instead.
app.add_middleware(BodySizeLimitMiddleware)


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code, content=exc.to_payload(), headers=exc.headers or None
    )


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


app.include_router(api_router, prefix=API_V1_PREFIX)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "aadhya-api", "version": app.version}
