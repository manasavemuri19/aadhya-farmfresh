"""Farm-facing endpoints: the stock screen and the order queue.

This is what replaces the spreadsheet. Every stock change is written to the
append-only ledger with the staff member who made it, so a discrepancy at the
end of the day can be reconstructed rather than argued about.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query

from app.api.deps import (
    AdminUser,
    StaffUser,
    get_cash_service,
    get_delivery_service,
    get_idempotency_repo,
    get_order_service,
    get_product_repo,
    get_support_service,
)
from app.api.route import TransactionalRoute
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.domain.enums import OrderStatus, SupportTicketStatus
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.products import ProductRepository
from app.schemas.cash import RecordSettlementRequest, SettlementView
from app.schemas.catalog import Product
from app.schemas.common import Page
from app.schemas.delivery import ReassignDeliveryRequest
from app.schemas.order import (
    AdjustStockRequest,
    OrderView,
    SetAvailabilityRequest,
    SetPriceRequest,
    UpdateOrderStatusRequest,
)
from app.schemas.support import SupportTicketView
from app.services.cash_service import CashService
from app.services.delivery_service import DeliveryService
from app.services.order_service import OrderService
from app.services.support_service import SupportService

router = APIRouter(prefix="/admin", tags=["admin"], route_class=TransactionalRoute)

Products = Annotated[ProductRepository, Depends(get_product_repo)]
Orders = Annotated[OrderService, Depends(get_order_service)]
Deliveries = Annotated[DeliveryService, Depends(get_delivery_service)]
Support = Annotated[SupportService, Depends(get_support_service)]
Idempotency = Annotated[IdempotencyRepository, Depends(get_idempotency_repo)]
Cash = Annotated[CashService, Depends(get_cash_service)]


@router.get("/products", response_model=list[Product])
async def list_products(staff: StaffUser, products: Products) -> list[Product]:
    """Full product records, stock counts included — unlike the customer view."""
    return await products.list_products(active_only=False)


@router.post("/stock", response_model=dict)
async def adjust_stock(
    body: AdjustStockRequest,
    staff: StaffUser,
    products: Products,
    idempotency: Idempotency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
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

    AAD-SEC-026: `set_qty`'s compare-and-swap above already makes a
    double-tap safe on its own — a repeat with the same stale
    `expected_qty` just gets the same 409. `delta_qty` has no such
    self-protection (there's nothing to compare against: "+2" is a valid
    call every time), so a double-tap on a slow connection genuinely
    applies twice. `Idempotency-Key` is optional here, not required like
    order creation (`AAD-API-003`) — this route predates any staff client
    sending one, and making it mandatory would break that client rather
    than fix anything. When a caller does send one, the same
    `IdempotencyRepository` order creation already uses backs a claim
    keyed on `(sku, delta_qty, reason)`, so a genuine retry replays the
    cached result instead of re-applying the delta.
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
        fingerprint = None
        if idempotency_key:
            fingerprint = hashlib.sha256(
                f"{body.sku}:{body.delta_qty}:{body.reason}".encode()
            ).hexdigest()
            existing = await idempotency.claim(staff.user_id, idempotency_key, fingerprint)
            if existing is not None:
                if existing.get("fingerprint") not in (None, fingerprint):
                    raise Conflict(
                        "This idempotency key was already used for a "
                        "different adjustment."
                    )
                if existing.get("status") == "completed" and existing.get("response"):
                    return existing["response"]
                raise Conflict("That adjustment is still being applied. Give it a moment.")

        ok, failure = await products.adjust_stock(body.sku, body.delta_qty)
        if not ok:
            # AAD-API-005: these three used to all come back as the same
            # `False`, so a mistyped SKU and a genuine negative-stock
            # attempt got the identical, misleading message.
            if failure == "no_such_sku":
                raise NotFound("No such SKU.")
            if failure == "not_tracked":
                raise ValidationError(
                    "This item isn't stock-tracked (made to order) — "
                    "there's no shelf count to adjust."
                )
            raise ValidationError("That adjustment would take stock below zero.")
        delta = body.delta_qty
        reason = f"{body.reason}:delta"

    await products.record_stock_movement(
        sku=body.sku, delta=delta, reason=reason, actor=staff.user_id
    )
    result: dict[str, object] = {"sku": body.sku, "ok": True}
    if body.delta_qty is not None and idempotency_key:
        await idempotency.complete(staff.user_id, idempotency_key, result)
    return result


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
    sku: str, body: SetAvailabilityRequest, staff: StaffUser, products: Products
) -> dict[str, object]:
    """The 'sold out for today' switch, without touching stock counts.
    AAD-DATA-017: the change is now recorded in catalog_audit.

    AAD-API-007: `active` used to be an unannotated parameter, which FastAPI
    binds as a query string on a POST — `?active=false` carrying the state
    change instead of the body every other mutation route here uses. Now a
    proper `SetAvailabilityRequest` body, same shape as before on the wire
    (`{"active": false}`), just no longer in the URL.
    """
    if not await products.set_variant_active(sku, body.active, actor=staff.user_id):
        raise NotFound("No such SKU.")
    return {"sku": sku, "is_active": body.active}


@router.get("/orders", response_model=Page[OrderView])
async def order_queue(
    staff: StaffUser,
    svc: Orders,
    status: Annotated[list[OrderStatus] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[
        datetime | None,
        Query(description="AAD-API-004: pass the previous `next_cursor` to walk further in."),
    ] = None,
) -> Page[OrderView]:
    """Oldest first — this is a work queue, not a feed.

    AAD-API-004: used to have a hard cap and no cursor, so orders past
    `limit` on a busy morning simply never appeared here — not an error,
    just invisible to the people who pack them. `has_more`/`next_cursor`
    now say so explicitly instead of staying silent about it.

    Goes through OrderService.list_queue_for_staff rather than reading
    OrderRepository directly — also closes AAD-QUAL-028, which flagged this
    route reaching into the service's private `_to_view` to make up for
    that missing method.
    """
    wanted = status or [
        OrderStatus.CONFIRMED,
        OrderStatus.PACKED,
        OrderStatus.OUT_FOR_DELIVERY,
    ]
    return await svc.list_queue_for_staff(wanted, limit=limit, after=after)


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


@router.post("/cod/settlements", response_model=SettlementView)
async def settle_cod_cash(
    body: RecordSettlementRequest, admin: AdminUser, svc: Cash
) -> SettlementView:
    """AAD-BIZ-004: an admin records what a delivery agent physically handed
    back — nothing here touches a gateway or moves real money, it only
    compares that figure against what the agent's unclaimed COD deliveries
    (OrderService.verify_delivery_code) say is owed.

    Admin-only, not staff, matching the same owner-only bar refunds get
    (see update_order_status above) — this is the other side of the same
    real-cash-handling coin. An exact match settles cleanly; any difference
    is recorded as a discrepancy with `body.reason`, never silently marked
    settled — see CashService.settle_agent."""
    return await svc.settle_agent(
        agent_id=body.agent_id,
        actual_amount_paise=body.actual_amount_received_paise,
        reason=body.reason,
        actor_id=admin.user_id,
    )


@router.post("/maintenance/release-holds", response_model=dict)
async def release_holds(staff: StaffUser, svc: Orders) -> dict[str, int]:
    """Manual trigger for the abandoned-checkout sweeper. Also runs on a timer."""
    return {"released": await svc.release_expired_holds()}


@router.get("/support/tickets", response_model=Page[SupportTicketView])
async def list_support_tickets(
    staff: StaffUser,
    svc: Support,
    status: Annotated[SupportTicketStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[
        datetime | None,
        Query(description="Pass the previous next_cursor to walk further in."),
    ] = None,
) -> Page[SupportTicketView]:
    """AAD-BIZ-005: the read side of a mailbox that used to have none at
    all. Oldest first, same reasoning as the order queue above — this is a
    work queue, not a feed. Omit `status` for everything; pass `open` to
    see only what still needs attention."""
    return await svc.list_for_staff(status=status, limit=limit, after=after)


@router.post("/support/tickets/{ticket_id}/close", response_model=dict)
async def close_support_ticket(
    ticket_id: str, staff: StaffUser, svc: Support
) -> dict[str, object]:
    """Marks a ticket handled. 404s rather than silently no-op'ing on an
    unknown id or one that's already closed, so a staff app can't get stuck
    believing a click succeeded when it didn't."""
    if not await svc.close(ticket_id):
        raise NotFound("No such open ticket.")
    return {"id": ticket_id, "status": SupportTicketStatus.CLOSED.value}
