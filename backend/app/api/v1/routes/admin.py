"""Farm-facing endpoints: the stock screen and the order queue.

This is what replaces the spreadsheet. Every stock change is written to the
append-only ledger with the staff member who made it, so a discrepancy at the
end of the day can be reconstructed rather than argued about.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    AdminUser,
    StaffUser,
    get_delivery_service,
    get_order_repo,
    get_order_service,
    get_product_repo,
)
from app.api.route import TransactionalRoute
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.domain.enums import OrderStatus
from app.repositories.orders import OrderRepository
from app.repositories.products import ProductRepository
from app.schemas.catalog import Product
from app.schemas.delivery import ReassignDeliveryRequest
from app.schemas.order import (
    AdjustStockRequest,
    OrderView,
    SetPriceRequest,
    UpdateOrderStatusRequest,
)
from app.services.delivery_service import DeliveryService
from app.services.order_service import OrderService

router = APIRouter(prefix="/admin", tags=["admin"], route_class=TransactionalRoute)

Products = Annotated[ProductRepository, Depends(get_product_repo)]
Orders = Annotated[OrderService, Depends(get_order_service)]
OrderRepo = Annotated[OrderRepository, Depends(get_order_repo)]
Deliveries = Annotated[DeliveryService, Depends(get_delivery_service)]


@router.get("/products", response_model=list[Product])
async def list_products(staff: StaffUser, products: Products) -> list[Product]:
    """Full product records, stock counts included — unlike the customer view."""
    return await products.list_products(active_only=False)


@router.post("/stock", response_model=dict)
async def adjust_stock(
    body: AdjustStockRequest, staff: StaffUser, products: Products
) -> dict[str, object]:
    """Set or adjust stock for one SKU.

    `set_qty` is the morning routine ("we bottled 40 litres"). `delta_qty` is a
    correction ("two got broken"). Exactly one of the two, never both.

    AAD-DATA-016 / AAD-DATA-015: `set_qty` requires `expected_qty` — the
    value the staff screen was showing when it loaded — and the write is a
    compare-and-swap on it. A screen that's gone stale (an order reserved
    stock, or someone else already adjusted it, since it loaded) gets a 409
    with the current value, instead of silently overwriting a real
    reservation. Because the swap is guaranteed accurate when it succeeds,
    the ledger delta computed from it is the real delta, not the absolute
    value the ledger used to record verbatim.
    """
    if (body.set_qty is None) == (body.delta_qty is None):
        raise ValidationError("Send exactly one of set_qty or delta_qty.")

    if body.set_qty is not None:
        if body.expected_qty is None:
            raise ValidationError(
                "expected_qty is required with set_qty — send the value "
                "currently shown on screen."
            )
        ok, current = await products.set_stock(
            body.sku, body.set_qty, expected_qty=body.expected_qty
        )
        if not ok:
            if current is None:
                raise NotFound("No such SKU.")
            raise Conflict(
                f"Stock changed to {current} since this screen loaded. "
                "Refresh and try again."
            )
        delta = body.set_qty - body.expected_qty
        reason = f"{body.reason}:set"
    else:
        if not await products.adjust_stock(body.sku, body.delta_qty):
            raise ValidationError("That adjustment would take stock below zero.")
        delta = body.delta_qty
        reason = f"{body.reason}:delta"

    await products.record_stock_movement(
        sku=body.sku, delta=delta, reason=reason, actor=staff.user_id
    )
    return {"sku": body.sku, "ok": True}


@router.post("/price", response_model=dict)
async def set_price(
    body: SetPriceRequest, admin: AdminUser, products: Products
) -> dict[str, object]:
    """Direct price change. AAD-SEC-025: owner-only — a comment in the
    `Role` enum always reserved this ("owner: everything, including
    refunds"), but every route here used to accept a plain staff account.

    AAD-DATA-009: mrp_paise is optional — send it alongside price_paise to
    also move the MRP (e.g. when raising a price past what's currently
    stored); omit it to change price only.

    AAD-API-006: a change past ±50% of the current price needs
    confirm_large_change=true — see SetPriceRequest and set_price's own
    docstring for why. AAD-DATA-017: every actual change is now recorded
    (old value, new value, actor, timestamp) in catalog_audit.
    """
    if not await products.set_price(
        body.sku, body.price_paise, body.mrp_paise,
        confirm_large_change=body.confirm_large_change, actor=admin.user_id,
    ):
        raise NotFound("No such SKU.")
    response: dict[str, object] = {"sku": body.sku, "price_paise": body.price_paise, "ok": True}
    if body.mrp_paise is not None:
        response["mrp_paise"] = body.mrp_paise
    return response


@router.post("/products/{sku}/availability", response_model=dict)
async def set_availability(
    sku: str, active: bool, staff: StaffUser, products: Products
) -> dict[str, object]:
    """The 'sold out for today' switch, without touching stock counts.
    AAD-DATA-017: the change is now recorded in catalog_audit."""
    if not await products.set_variant_active(sku, active, actor=staff.user_id):
        raise NotFound("No such SKU.")
    return {"sku": sku, "is_active": active}


@router.get("/orders", response_model=list[OrderView])
async def order_queue(
    staff: StaffUser,
    svc: Orders,
    orders: OrderRepo,
    status: Annotated[list[OrderStatus] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[OrderView]:
    """Oldest first — this is a work queue, not a feed."""
    wanted = status or [
        OrderStatus.CONFIRMED,
        OrderStatus.PACKED,
        OrderStatus.OUT_FOR_DELIVERY,
    ]
    docs = await orders.list_by_status(wanted, limit=limit)
    return [svc._to_view(doc) for doc in docs]


@router.post("/orders/{order_id}/status", response_model=OrderView)
async def update_order_status(
    order_id: str, body: UpdateOrderStatusRequest, staff: StaffUser, svc: Orders
) -> OrderView:
    """AAD-SEC-025: staff drive every ordinary fulfilment transition — that's
    the role. A transition into REFUNDED is different: it triggers a real
    gateway refund call (`OrderService._maybe_refund`), which the `Role`
    enum's own comment always reserved for the owner. `staff` here already
    carries the caller's *freshly re-checked* role (StaffUser re-reads it
    from the database, per AAD-SEC-002), so this is an in-handler check
    rather than a second dependency — it doesn't need a second DB read.
    """
    if body.status is OrderStatus.REFUNDED and not staff.is_admin:
        raise Forbidden("Refunding an order needs an owner account.")
    return await svc.update_status(
        order_id=order_id, new_status=body.status, note=body.note, actor=staff.user_id
    )


@router.post("/orders/{order_id}/reassign", status_code=204)
async def reassign_delivery(
    order_id: str, body: ReassignDeliveryRequest, staff: StaffUser, svc: Deliveries
) -> None:
    """AAD-REL-006: move an order to a different delivery agent, or back to
    the unassigned pool (agent_id omitted/null) — a stuck agent, a no-show,
    a shift change. Staff-only: unlike an agent's own accept/release, this
    isn't scoped to CONFIRMED and doesn't check the agent's own concurrent-
    order cap (see DeliveryService.reassign for why)."""
    await svc.reassign(order_id, new_agent_id=body.agent_id, actor_id=staff.user_id, note=body.note)


@router.post("/maintenance/release-holds", response_model=dict)
async def release_holds(staff: StaffUser, svc: Orders) -> dict[str, int]:
    """Manual trigger for the abandoned-checkout sweeper. Also runs on a timer."""
    return {"released": await svc.release_expired_holds()}
