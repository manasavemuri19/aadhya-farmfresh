"""Farm-facing endpoints: the stock screen and the order queue.

This is what replaces the spreadsheet. Every stock change is written to the
append-only ledger with the staff member who made it, so a discrepancy at the
end of the day can be reconstructed rather than argued about.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import StaffUser, get_order_repo, get_order_service, get_product_repo
from app.core.errors import NotFound, ValidationError
from app.domain.enums import OrderStatus
from app.repositories.orders import OrderRepository
from app.repositories.products import ProductRepository
from app.schemas.catalog import Product
from app.schemas.order import AdjustStockRequest, OrderView, UpdateOrderStatusRequest
from app.services.order_service import OrderService

router = APIRouter(prefix="/admin", tags=["admin"])

Products = Annotated[ProductRepository, Depends(get_product_repo)]
Orders = Annotated[OrderService, Depends(get_order_service)]
OrderRepo = Annotated[OrderRepository, Depends(get_order_repo)]


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
    """
    if (body.set_qty is None) == (body.delta_qty is None):
        raise ValidationError("Send exactly one of set_qty or delta_qty.")

    if body.set_qty is not None:
        if not await products.set_stock(body.sku, body.set_qty):
            raise NotFound("No such SKU.")
        delta = body.set_qty
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


@router.post("/products/{sku}/availability", response_model=dict)
async def set_availability(
    sku: str, active: bool, staff: StaffUser, products: Products
) -> dict[str, object]:
    """The 'sold out for today' switch, without touching stock counts."""
    if not await products.set_variant_active(sku, active):
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
    return await svc.update_status(
        order_id=order_id, new_status=body.status, note=body.note, actor=staff.user_id
    )


@router.post("/maintenance/release-holds", response_model=dict)
async def release_holds(staff: StaffUser, svc: Orders) -> dict[str, int]:
    """Manual trigger for the abandoned-checkout sweeper. Also runs on a timer."""
    return {"released": await svc.release_expired_holds()}
