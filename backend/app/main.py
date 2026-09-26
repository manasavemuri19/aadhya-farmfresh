"""Application entrypoint."""

from __future__ import annotations

# AAD-QUAL-007: `settings` and `configure_logging` are both leaf modules —
# neither imports anything else of ours — so they can be resolved and
# structured logging turned on before any of the imports below run. This
# used to happen inside `lifespan()`, which only executes once uvicorn
# actually starts serving: everything imported below (every route, service
# and repository this app has) previously ran its *module-level* code, if
# any ever logs during import, through Python's unstructured default
# logging config instead. The remaining imports are genuinely below code
# now, hence the noqa: E402s — that ordering is the fix, not an oversight.
from app.core.config import settings
from app.core.logging import configure_logging

configure_logging(settings.log_level)

import asyncio  # noqa: E402
import logging  # noqa: E402
from collections.abc import Awaitable, Callable  # noqa: E402
from contextlib import asynccontextmanager, suppress  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.api.middleware import (  # noqa: E402
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    TrustedHostExceptHealthMiddleware,
)
from app.api.v1.router import API_V1_PREFIX, api_router  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.outbox import defer_until_commit  # noqa: E402
from app.db import base as db  # noqa: E402
from app.db.base import session_scope  # noqa: E402
from app.payments import get_payment_provider  # noqa: E402
from app.repositories.idempotency import IdempotencyRepository  # noqa: E402
from app.repositories.orders import OrderRepository  # noqa: E402
from app.repositories.products import ProductRepository  # noqa: E402
from app.repositories.push_tokens import PushTokenRepository  # noqa: E402
from app.repositories.refresh_tokens import RefreshTokenRepository  # noqa: E402
from app.repositories.support import SupportRepository  # noqa: E402
from app.repositories.users import UserRepository  # noqa: E402
from app.services.order_service import OrderService  # noqa: E402
from app.services.push_service import PushService  # noqa: E402

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
    purpose, not even for reuse detection), redact webhook payloads past
    retention (AAD-DATA-011 — the `(provider, event_id)` dedupe row stays
    forever, only the raw gateway payload is cleared), and prune push
    tokens no device has touched in 90 days (AAD-SEC-032 — the other half
    of that fix is `DELETE /notifications/token` at sign-out, which only
    covers a customer who actually signs out; this covers the handset that
    never does). Postgres has no TTL index, so that cleanup is explicit
    rather than automatic. AAD-BIZ-003: also pushes staff/admin once a SKU
    dips into its own low-stock band, debounced by `low_stock_notified` so
    it fires once per dip rather than every 2 minutes it sits there.

    Split out from the loop in `_housekeeping` below so it can run
    immediately at startup as well as on the timer, and so a test can call
    it directly without needing to run (or cancel) an infinite loop.

    AAD-QUAL-009: these were all function-local imports, which usually means
    a circular dependency is being papered over. There isn't one here —
    confirmed by moving every one of them to module level and importing
    `app.main` cleanly — so they're hoisted to the top of the file with
    everything else now.
    """
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
        push_tokens_pruned = await PushTokenRepository(session).prune_stale()
        low_stock_notified = await _notify_low_stock(session)
        low_stock_rearmed = await service.products.clear_stale_low_stock_flags()

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
            "push_tokens_pruned": push_tokens_pruned,
            "low_stock_notified": low_stock_notified,
            "low_stock_rearmed": low_stock_rearmed,
        },
    )


async def _notify_low_stock(session: db.AsyncSession) -> int:
    """AAD-BIZ-003: push every staff/admin account once per SKU each time it
    dips into its own low-stock band. The threshold itself is per-variant
    (`low_stock_threshold`, already in use for the in-app "low stock" badge
    — this reuses that column rather than inventing a second one) and the
    owner hasn't given us a value to change it to yet, so this notifies off
    whatever each variant is already set to.

    Deferred via `defer_until_commit` rather than awaited inline, same
    reasoning as `SupportService._notify_staff` (AAD-BIZ-005) and
    `OrderService`'s own push call sites (AAD-REL-004): `session_scope()`
    wraps this whole sweep pass in an outbox batch, so the push only
    actually fires once `mark_low_stock_notified` below has committed —
    never before, and never for a dip a rollback would have undone.
    """
    products = ProductRepository(session)
    newly_low = await products.find_newly_low_stock()
    if not newly_low:
        return 0

    staff_ids = await UserRepository(session).list_staff_ids()
    skus = [row["sku"] for row in newly_low]
    if staff_ids:
        push = PushService(PushTokenRepository(session))
        for row in newly_low:
            await defer_until_commit(
                _low_stock_push_effect(
                    push, staff_ids, sku=row["sku"], label=row["label"], stock_qty=row["stock_qty"]
                )
            )

    await products.mark_low_stock_notified(skus)
    return len(skus)


def _low_stock_push_effect(
    push: PushService, staff_ids: list[str], *, sku: str, label: str, stock_qty: int
) -> Callable[[], Awaitable[None]]:
    """A small factory rather than a lambda defined inline in the loop
    above, for two reasons: it binds `sku`/`label`/`stock_qty` per call
    (avoiding the classic late-binding-closure-in-a-loop bug a bare
    `lambda: ...` referencing the loop variable would have), and mypy can
    actually infer this nested function's type against `Effect` — a
    default-argument lambda used for the same binding trick left it unable
    to."""

    async def _send() -> None:
        await push.notify_users(
            staff_ids,
            title="Low stock",
            body=f"{label} ({sku}) is down to {stock_qty} left — restock soon.",
            data={"sku": sku},
        )

    return _send


async def _housekeeping() -> None:
    """Runs `_run_sweep_once` on a timer for the life of the process.

    AAD-QUAL-009: took an `app: FastAPI` parameter it never used — the sweep
    itself opens its own session via `session_scope()` rather than anything
    hung off `app.state`. Dropped; `lifespan` below still stashes the task
    it returns on `app.state.sweeper` for its own shutdown handling, that
    just no longer means this function needs a reference to `app` itself.

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
    # AAD-QUAL-007: `configure_logging()` used to be called here, which only
    # ever ran once uvicorn's lifespan startup fired — after every module in
    # this app had already finished importing. It's called once, at the top
    # of this file, before those imports run, instead.
    log.info("starting api", extra={"env": settings.env})

    await db.connect()
    app.state.sweeper = asyncio.create_task(_housekeeping())

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
    # AAD-QUAL-011: was hardcoded "1.0.0" — never changed, so it told you
    # nothing about which build produced a given log line or `/` response,
    # which is exactly what you want during a rollback. Settings.app_version
    # picks up Railway's own injected git SHA with no deploy config needed;
    # see its default_factory in app/core/config.py.
    version=settings.app_version,
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
# AAD-SEC-017: no TrustedHostMiddleware existed at all — any Host header a
# client sent was trusted. Added even later than BodySizeLimitMiddleware, so
# it is the outermost of all of them: a forged Host is rejected with a plain
# 400 before this app spends any work — counting body bytes included — on
# the request at all. `settings.allowed_host_list` is empty-by-mistake-proof
# by construction (`assert_deploy_safe` refuses a wildcard or a local/test
# host in staging or production), so this can't silently degrade into an
# allow-everything no-op the way an unvalidated allowlist could.
#
# Wrapped rather than used directly (see TrustedHostExceptHealthMiddleware's
# own docstring): Railway's infrastructure health-check hits /v1/health/live
# over its private network with a Host header that will never be in this
# app's public allowlist, and a rejected health check reads as "unhealthy" —
# discovered as a real production crash-loop, not a hypothetical.
app.add_middleware(TrustedHostExceptHealthMiddleware, allowed_hosts=settings.allowed_host_list)


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code, content=exc.to_payload(), headers=exc.headers or None
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # AAD-QUAL-010: this path (FastAPI's own request-schema validation,
    # before a route body even runs) is outside AppError.to_payload()
    # entirely, so it needed the same request_id added separately here —
    # otherwise the one error response a customer hits most often (a bad
    # request body) was exactly the one still missing it.
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Some of those details are not quite right.",
                "request_id": getattr(request.state, "request_id", "-"),
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


# AAD-SEC-019: used to return {"service": "aadhya-api", "version": app.version}
# — free, unauthenticated reconnaissance for anyone who just hits the bare
# domain. Nothing in this app (no infra healthcheck, no client) actually
# reads this route; it exists only so a stray request to "/" gets a clean
# response instead of a 404. A 204 does that with nothing to leak.
@app.get("/", include_in_schema=False, status_code=204)
async def root() -> None:
    return None
