from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, get_order_service, idempotency_key
from app.schemas.order import (
    CancelOrderRequest,
    CartInput,
    CreateOrderRequest,
    OrderView,
    Quote,
    UpdateOrderAddressRequest,
)
from app.services.order_service import OrderService

router = APIRouter(tags=["orders"])

Orders = Annotated[OrderService, Depends(get_order_service)]


@router.post("/cart/quote", response_model=Quote, summary="Price and check a cart")
async def quote_cart(body: CartInput, svc: Orders) -> Quote:
    """Open to signed-out users so the cart totals work before login."""
    return await svc.quote(body.lines)


@router.post("/orders", response_model=OrderView, status_code=201)
async def create_order(
    body: CreateOrderRequest,
    principal: CurrentUser,
    svc: Orders,
    key: Annotated[str | None, Depends(idempotency_key)] = None,
) -> OrderView:
    """Place an order.

    Send an `Idempotency-Key` header — a UUID generated once per checkout
    attempt and kept across retries. Without it, a dropped response on a flaky
    connection can produce a duplicate order.
    """
    return await svc.create_order(user_id=principal.user_id, request=body, idempotency_key=key)


@router.get("/orders", response_model=list[OrderView])
async def list_orders(
    principal: CurrentUser,
    svc: Orders,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[OrderView]:
    return await svc.list_for_user(principal.user_id, limit=limit)


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
    """Only while the order is still in CUSTOMER_CANCELLABLE — see
    OrderService.update_address and OrderView.can_edit_address."""
    return await svc.update_address(
        order_id=order_id, user_id=principal.user_id, address=body.address
    )
