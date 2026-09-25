from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, get_order_service, idempotency_key
from app.api.route import TransactionalRoute
from app.core.rate_limit import IpRateLimiter
from app.schemas.common import Page
from app.schemas.order import (
    CancelOrderRequest,
    CartInput,
    CreateOrderRequest,
    OrderView,
    Quote,
    UpdateOrderAddressRequest,
)
from app.services.order_service import OrderService

router = APIRouter(tags=["orders"], route_class=TransactionalRoute)

Orders = Annotated[OrderService, Depends(get_order_service)]

# AAD-SEC-022: this route is deliberately open to signed-out callers ("so
# cart totals work before login"), which means AAD-SEC-004's blanket
# authenticated-user limiter never covers it — it needs its own IP-keyed
# one. 20/min is tighter than the catalog's 60/min (AAD-PERF-010): each call
# here accepts up to 50 SKUs and prices every one of them, so it is the more
# expensive of the two per request.
_quote_per_minute = IpRateLimiter(limit=20, seconds=60)


@router.post(
    "/cart/quote",
    response_model=Quote,
    summary="Price and check a cart",
    dependencies=[Depends(_quote_per_minute)],
)
async def quote_cart(body: CartInput, svc: Orders) -> Quote:
    """Open to signed-out users so the cart totals work before login.

    AAD-SEC-022 also flagged this endpoint for leaking exact stock levels to
    an anonymous caller who deliberately over-requests a SKU and reads the
    clamp back. That part turned out to already be moot, not fixed here:
    `GET /catalog`'s `VariantView.max_qty` (`schemas/catalog.py:107,137`) is
    `sellable_qty()` — the exact same number this endpoint would clamp to —
    and it is served to every unauthenticated catalog browse already, with
    no request-crafting needed. Redacting it here would not add any real
    confidentiality, only a discrepancy between two endpoints that agree
    today. The genuine part of this finding — 50 SKUs, a DB query each,
    fully unauthenticated and previously unthrottled — is what the rate
    limit above closes. If the exact `max_qty` figure itself is unwanted on
    the public catalog, that is a real but separate finding against
    `GET /catalog`, not this route.
    """
    return await svc.quote(body.lines)


@router.post("/orders", response_model=OrderView, status_code=201)
async def create_order(
    body: CreateOrderRequest,
    principal: CurrentUser,
    svc: Orders,
    key: Annotated[str, Depends(idempotency_key)],
) -> OrderView:
    """Place an order.

    Requires an `Idempotency-Key` header — a UUID generated once per
    checkout attempt and kept across retries (AAD-API-003). Without one, a
    dropped response on a flaky connection produces a duplicate order: a
    second stock reservation, a second Razorpay order, and on the COD path
    (which confirms instantly) a second dispatch to the same address.
    """
    return await svc.create_order(user_id=principal.user_id, request=body, idempotency_key=key)


@router.get("/orders", response_model=Page[OrderView])
async def list_orders(
    principal: CurrentUser,
    svc: Orders,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before: Annotated[
        datetime | None,
        Query(description="AAD-API-004: pass the previous `next_cursor` to go further back."),
    ] = None,
) -> Page[OrderView]:
    return await svc.list_for_user(principal.user_id, limit=limit, before=before)


@router.get("/orders/{order_id}", response_model=OrderView)
async def get_order(order_id: str, principal: CurrentUser, svc: Orders) -> OrderView:
    return await svc.get_for_user(order_id, principal.user_id)


@router.post("/orders/{order_id}/cancel", response_model=OrderView)
async def cancel_order(
    order_id: str, body: CancelOrderRequest, principal: CurrentUser, svc: Orders
) -> OrderView:
    return await svc.cancel(
        order_id=order_id, user_id=principal.user_id, reason=body.reason
    )


@router.patch("/orders/{order_id}/address", response_model=OrderView)
async def update_order_address(
    order_id: str, body: UpdateOrderAddressRequest, principal: CurrentUser, svc: Orders
) -> OrderView:
    """Disabled by product decision — every call reaches this and gets a
    Forbidden. See OrderService.update_address's own docstring for why it's
    kept wired up rather than removed."""
    return await svc.update_address(
        order_id=order_id, user_id=principal.user_id, address=body.address
    )


@router.post("/orders/{order_id}/retry-payment", response_model=OrderView)
async def retry_order_payment(
    order_id: str, principal: CurrentUser, svc: Orders
) -> OrderView:
    """AAD-DATA-005: a fresh gateway order/Payment Link for an order whose
    payment attempt expired or failed — see OrderService.retry_payment for
    what's retryable and why this doesn't need a request body."""
    return await svc.retry_payment(order_id=order_id, user_id=principal.user_id)
