"""Order orchestration.

The checkout path is materially simpler than the document-store version was,
and the reason is worth stating: **the whole write is one database
transaction.** Reserving stock for five SKUs, inserting the order, its lines,
its first event, its payment row and five ledger entries either all happen or
none do. If the third reservation fails, the first two vanish on rollback —
there is no compensation code to get wrong.

The one thing that cannot join that transaction is the payment gateway, since
it is a network call to somebody else's system. So the order of operations is:

  1. Price the cart from the catalog (read-only; the client's numbers are
     never trusted).
  2. Validate: availability, order minimum, and the total the customer saw.
  3. Ask the gateway to create a payment order — *before* opening the write.
     A slow gateway must never hold row locks on inventory.
  4. One transaction: reserve stock, write everything, commit.

If step 4 fails, the gateway order is orphaned and simply expires unpaid. That
is a strictly better failure than the alternative, where a gateway timeout
leaves stock reserved for an order that does not exist.

Stock is still reserved before payment completes. For fresh dairy that is the
right trade — overselling the last two litres costs a phone call and a refund,
while briefly holding stock that then expires costs nothing. Abandoned
checkouts are swept back onto the shelf after fifteen minutes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    OutOfStock,
    PriceChanged,
    UpstreamError,
)
from app.core.ids import human_order_number, new_order_id, new_payment_id
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.domain.order_state import CUSTOMER_CANCELLABLE, RELEASES_STOCK, assert_transition
from app.payments.base import PaymentProvider, WebhookEvent
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.orders import OrderRepository
from app.repositories.products import ProductRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.catalog import Variant
from app.schemas.order import (
    CartLineInput,
    CreateOrderRequest,
    OrderView,
    Quote,
    QuoteLine,
)
from app.services.pricing import PricedLine, build_cart, price_line
from app.services.push_service import PushService

log = logging.getLogger(__name__)

PAYMENT_HOLD = timedelta(minutes=15)

# Push copy for the customer-facing statuses worth interrupting someone's day
# for. Statuses not in this map (pending_payment) simply don't push — nothing
# useful to tell anyone about a cart that hasn't paid yet.
_STATUS_PUSH_COPY: dict[OrderStatus, tuple[str, str]] = {
    OrderStatus.CONFIRMED: ("Order confirmed", "The farm is preparing order {order_number}."),
    OrderStatus.PACKED: ("Order packed", "Order {order_number} is packed and ready to leave."),
    OrderStatus.OUT_FOR_DELIVERY: ("On the way", "Order {order_number} is on its way to you."),
    OrderStatus.DELIVERED: ("Delivered", "Order {order_number} has been delivered. Enjoy!"),
    OrderStatus.CANCELLED: ("Order cancelled", "Order {order_number} was cancelled."),
    OrderStatus.REFUNDED: ("Order refunded", "Order {order_number} has been refunded."),
}


class OrderService:
    def __init__(
        self,
        products: ProductRepository,
        orders: OrderRepository,
        idempotency: IdempotencyRepository,
        payments: PaymentProvider,
        *,
        users: UserRepository | None = None,
        push: PushService | None = None,
    ) -> None:
        self.products = products
        self.orders = orders
        self.idempotency = idempotency
        self.payments = payments
        # Both optional, both default None: see get_order_service in deps.py
        # for why (the background housekeeping sweeper builds this class
        # directly, without either).
        self.users = users
        self.push = push

    # ---------- notifications ----------
    # Best-effort side effects on top of the state changes above — never
    # allowed to affect whether a transition itself succeeds. Every call site
    # below is *after* the database write already committed.

    async def _notify_customer(self, order: dict[str, Any], new_status: OrderStatus) -> None:
        if not self.push:
            return
        copy = _STATUS_PUSH_COPY.get(new_status)
        if not copy:
            return
        title, body_template = copy
        await self.push.notify_users(
            [order["user_id"]],
            title=title,
            body=body_template.format(order_number=order["order_number"]),
            data={"order_id": order["id"], "status": new_status.value},
        )

    async def _notify_agents_new_order(self, order: dict[str, Any]) -> None:
        if not self.push or not self.users:
            return
        agent_ids = await self.users.list_delivery_agent_ids()
        if not agent_ids:
            return
        await self.push.notify_users(
            agent_ids,
            title="New order available",
            body=f"Order {order['order_number']} is ready for pickup.",
            data={"order_id": order["id"]},
        )

    # ---------- quoting ----------

    async def quote(self, lines: list[CartLineInput]) -> Quote:
        priced, products_by_sku = await self._price(lines)
        cart = build_cart(priced, products_by_sku)
        return Quote(
            lines=[
                QuoteLine(
                    sku=line.sku,
                    product_id=line.product_id,
                    product_name=line.product_name,
                    variant_label=line.variant_label,
                    image_url=line.image_url,
                    qty=line.qty,
                    unit_price_paise=line.unit_price_paise,
                    line_total_paise=line.line_total_paise,
                    adjusted_from_qty=line.adjusted_from_qty,
                    unavailable_reason=line.unavailable_reason,
                )
                for line in cart.lines
            ],
            subtotal_paise=cart.subtotal_paise,
            delivery_fee_paise=cart.delivery_fee_paise,
            discount_paise=cart.discount_paise,
            total_paise=cart.total_paise,
            currency=settings.currency,
            free_delivery_threshold_paise=settings.free_delivery_threshold_paise,
            min_order_paise=settings.min_order_paise,
            meets_minimum=cart.meets_minimum,
            eta_minutes=cart.eta_minutes,
            has_adjustments=cart.has_adjustments,
        )

    async def _price(self, lines: list[CartLineInput]) -> tuple[list[PricedLine], dict]:
        # Collapse duplicate SKUs so two taps on the same item price as one line.
        merged: dict[str, int] = {}
        for line in lines:
            merged[line.sku] = merged.get(line.sku, 0) + line.qty

        resolved = await self.products.find_variants(list(merged))
        priced: list[PricedLine] = []
        products_by_sku: dict[str, Any] = {}

        for sku, qty in merged.items():
            match = resolved.get(sku)
            if match is None:
                priced.append(
                    PricedLine(
                        sku=sku, product_id="", product_name="Unavailable item",
                        variant_label="", image_url="", qty=0, unit_price_paise=0,
                        line_total_paise=0, adjusted_from_qty=qty,
                        unavailable_reason="not_found",
                    )
                )
                continue
            product, variant_doc = match
            products_by_sku[sku] = product
            priced.append(price_line(product, Variant(**variant_doc), qty))

        return priced, products_by_sku

    # ---------- checkout ----------

    async def create_order(
        self, *, user_id: str, request: CreateOrderRequest, idempotency_key: str | None
    ) -> OrderView:
        fingerprint = self._fingerprint(request)

        if idempotency_key:
            existing = await self.idempotency.claim(user_id, idempotency_key, fingerprint)
            if existing is not None:
                # Fingerprint is checked *before* replaying any cached response.
                # The other order would hand a client the wrong order whenever
                # it reused a key with a different cart — silent and expensive.
                if existing.get("fingerprint") not in (None, fingerprint):
                    raise Conflict("This request was already used for a different order.")
                if existing.get("status") == "completed" and existing.get("response"):
                    return OrderView(**existing["response"])
                raise Conflict("That order is still being placed. Give it a moment.")

        order_view = await self._create_order_inner(user_id, request)

        if idempotency_key:
            await self.idempotency.complete(
                user_id, idempotency_key, order_view.model_dump(mode="json")
            )
        return order_view

    async def _create_order_inner(
        self, user_id: str, request: CreateOrderRequest
    ) -> OrderView:
        priced, products_by_sku = await self._price(request.lines)
        cart = build_cart(priced, products_by_sku)
        sellable = [line for line in cart.lines if line.is_sellable]

        if not sellable:
            raise OutOfStock("Nothing in your cart is available right now.")
        if cart.has_adjustments:
            raise OutOfStock(
                "Some items ran out while you were checking out. Review your cart.",
                details={
                    "lines": [
                        {
                            "sku": line.sku,
                            "requested": line.adjusted_from_qty,
                            "available": line.qty,
                            "reason": line.unavailable_reason,
                        }
                        for line in cart.lines
                        if line.adjusted_from_qty is not None or line.unavailable_reason
                    ]
                },
            )
        if (
            request.expected_total_paise is not None
            and request.expected_total_paise != cart.total_paise
        ):
            raise PriceChanged(
                "Prices changed since you opened the cart. Check the new total.",
                details={
                    "expected_total_paise": request.expected_total_paise,
                    "actual_total_paise": cart.total_paise,
                },
            )

        order_id = new_order_id()
        now = datetime.now(UTC)
        is_cod = request.payment_method is PaymentMethod.COD
        hold_expires_at = None if is_cod else now + PAYMENT_HOLD

        # Gateway call happens before the write, so a slow gateway never holds
        # locks on inventory rows. The same hold_expires_at is handed to the
        # gateway as its own expiry (AAD-PAY-006 / AAD-PAY-014), so this app
        # and the gateway can never disagree about when the payment is dead.
        provider_order = None
        if not is_cod:
            provider_order = await self.payments.create_order(
                amount_paise=cart.total_paise,
                currency=settings.currency,
                receipt=order_id,
                notes={"order_id": order_id, "user_id": user_id},
                expires_at=hold_expires_at,
            )

        # --- everything below is one transaction, committed by the caller ---
        for line in sellable:
            if not await self.products.reserve_stock(line.sku, line.qty):
                # Rollback unwinds every earlier reservation automatically.
                raise OutOfStock(
                    f"{line.product_name} ({line.variant_label}) just sold out.",
                    details={"sku": line.sku},
                )

        status = OrderStatus.CONFIRMED if is_cod else OrderStatus.PENDING_PAYMENT
        timeline = [
            {
                "status": OrderStatus.PENDING_PAYMENT.value,
                "at": now,
                "note": "Order placed",
                "by": "customer",
            }
        ]
        if is_cod:
            timeline.append(
                {
                    "status": OrderStatus.CONFIRMED.value,
                    "at": now,
                    "note": "Cash on delivery",
                    "by": "system",
                }
            )

        await self.orders.insert(
            {
                "id": order_id,
                "order_number": human_order_number(),
                "user_id": user_id,
                "status": status.value,
                "lines": [
                    {
                        "sku": line.sku,
                        "product_id": line.product_id,
                        "product_name": line.product_name,
                        "variant_label": line.variant_label,
                        "image_url": line.image_url,
                        "qty": line.qty,
                        "unit_price_paise": line.unit_price_paise,
                        "line_total_paise": line.line_total_paise,
                    }
                    for line in sellable
                ],
                "subtotal_paise": cart.subtotal_paise,
                "delivery_fee_paise": cart.delivery_fee_paise,
                "discount_paise": cart.discount_paise,
                "total_paise": cart.total_paise,
                "currency": settings.currency,
                "address": request.address.model_dump(mode="json"),
                "notes": request.notes,
                "eta_minutes": cart.eta_minutes,
                "stock_released": False,
                "hold_expires_at": hold_expires_at,
                "timeline": timeline,
                "payment": {
                    "id": new_payment_id(),
                    "method": request.payment_method.value,
                    "status": PaymentStatus.CREATED.value,
                    "amount_paise": cart.total_paise,
                    "provider": provider_order.provider if provider_order else None,
                    "provider_order_id": (
                        provider_order.provider_order_id if provider_order else None
                    ),
                    "checkout_payload": (
                        provider_order.checkout_payload if provider_order else None
                    ),
                },
            }
        )

        for line in sellable:
            await self.products.record_stock_movement(
                sku=line.sku, delta=-line.qty, reason="order_reserved",
                order_id=order_id, actor=user_id,
            )

        order = await self.orders.get(order_id)
        assert order is not None

        if is_cod:
            # A COD order is confirmed the instant it's placed — nothing to
            # wait on. Online orders get their "confirmed" notification later,
            # from apply_webhook, once payment actually clears.
            await self._notify_agents_new_order(order)

        return self._to_view(
            order,
            checkout_payload=provider_order.checkout_payload if provider_order else None,
        )

    # ---------- payment outcomes ----------

    async def apply_webhook(self, event: WebhookEvent) -> None:
        """Move an order based on a verified gateway event.

        Called only after signature verification and replay dedupe. Every branch
        is a no-op when the order already sits in the target state, because
        gateways deliver the same event more than once as a matter of routine.
        """
        # Prefer the id the event told us directly is our own order (e.g. a
        # Payment Link's reference_id) over matching on a gateway-assigned
        # id — see WebhookEvent.order_id and AAD-PAY-007.
        order = await self.orders.get(event.order_id) if event.order_id else None

        if order is None:
            if not event.provider_order_id:
                log.warning(
                    "webhook without provider order id", extra={"event": event.event_type}
                )
                return
            order = await self.orders.get_by_provider_order_id(event.provider_order_id)

        if not order:
            log.warning(
                "webhook for unknown order",
                extra={"order_id": event.order_id, "po": event.provider_order_id},
            )
            return

        order_id = order["id"]
        current = OrderStatus(order["status"])

        if event.is_capture:
            if event.amount_paise is not None and event.amount_paise != order["total_paise"]:
                # An amount mismatch is never routine. Do not confirm; alert.
                log.error(
                    "webhook amount mismatch",
                    extra={
                        "order": order_id,
                        "expected": order["total_paise"],
                        "received": event.amount_paise,
                    },
                )
                return
            await self.orders.set_payment_status(
                order_id, PaymentStatus.CAPTURED,
                provider_payment_id=event.provider_payment_id,
            )
            if current is OrderStatus.PENDING_PAYMENT:
                updated = await self.orders.transition(
                    order_id,
                    expected_status=OrderStatus.PENDING_PAYMENT,
                    new_status=OrderStatus.CONFIRMED,
                    note="Payment received",
                    actor="payment_gateway",
                    extra_set={"hold_expires_at": None},
                )
                if updated:
                    await self._notify_customer(updated, OrderStatus.CONFIRMED)
                    await self._notify_agents_new_order(updated)
            elif current is OrderStatus.CANCELLED:
                # A capture landing after the order was already cancelled
                # (AAD-PAY-001) — the payment hold's 15 minutes were shorter
                # than this payment actually took to clear. The money is
                # real and the order is not coming back, so the correct
                # outcome is an automatic refund, not a payment silently
                # left CAPTURED against a CANCELLED order forever. This is a
                # backstop for the case the poll-and-reconcile check in
                # `release_expired_holds` (AAD-PAY-006) already narrowed to
                # a brief race window, not a replacement for it.
                log.error(
                    "payment captured after the order was already cancelled — "
                    "refunding automatically",
                    extra={"order": order_id},
                )
                # Re-fetch so the refund path sees the CAPTURED status and
                # provider_payment_id just written above, not the stale
                # snapshot fetched at the top of this method.
                refreshed = await self.orders.get(order_id)
                if refreshed is not None:
                    await self._cancel(
                        refreshed,
                        note="Payment captured after the order was cancelled — "
                        "refunded automatically",
                        actor="payment_gateway",
                        target_status=OrderStatus.REFUNDED,
                    )
            # Any other current status (CONFIRMED, PACKED, OUT_FOR_DELIVERY,
            # DELIVERED, REFUNDED) is a routine duplicate delivery of a
            # capture already applied — payment status is now accurate and
            # there is nothing else to do.
            return

        if event.is_failure:
            await self.orders.set_payment_status(order_id, PaymentStatus.FAILED)
            if current is OrderStatus.PENDING_PAYMENT:
                await self._cancel(order, note="Payment failed", actor="payment_gateway")
            return

        if event.is_refund:
            await self.orders.set_payment_status(order_id, PaymentStatus.REFUNDED)
            log.info("refund recorded", extra={"order": order_id})

    # ---------- lifecycle ----------

    async def cancel(self, *, order_id: str, user_id: str, reason: str) -> OrderView:
        order = await self.orders.get_for_user(order_id, user_id)
        if not order:
            raise NotFound("We could not find that order.")

        current = OrderStatus(order["status"])
        if current not in CUSTOMER_CANCELLABLE:
            raise Forbidden(
                "This order has already left the farm. Call us and we will sort it out."
            )
        updated = await self._cancel(
            order, note=reason or "Cancelled by customer", actor=user_id
        )
        return self._to_view(updated)

    async def update_status(
        self, *, order_id: str, new_status: OrderStatus, note: str, actor: str
    ) -> OrderView:
        order = await self.orders.get(order_id)
        if not order:
            raise NotFound("We could not find that order.")

        current = OrderStatus(order["status"])
        assert_transition(current, new_status)

        if new_status in RELEASES_STOCK:
            updated = await self._cancel(
                order, note=note, actor=actor, target_status=new_status
            )
            return self._to_view(updated)

        updated = await self.orders.transition(
            order_id, expected_status=current, new_status=new_status, note=note, actor=actor
        )
        if not updated:
            raise Conflict("That order changed while you were updating it. Refresh and retry.")
        await self._notify_customer(updated, new_status)
        if new_status is OrderStatus.CONFIRMED:
            # Reachable when staff manually confirm an order rather than it
            # arriving via checkout/webhook (e.g. a COD edge case handled by
            # hand) — the delivery pool still needs to hear about it either way.
            await self._notify_agents_new_order(updated)
        return self._to_view(updated)

    async def _cancel(
        self,
        order: dict[str, Any],
        *,
        note: str,
        actor: str,
        target_status: OrderStatus = OrderStatus.CANCELLED,
    ) -> dict[str, Any]:
        order_id = order["id"]
        current = OrderStatus(order["status"])

        updated = await self.orders.transition(
            order_id,
            expected_status=current,
            new_status=target_status,
            note=note,
            actor=actor,
            extra_set={"hold_expires_at": None, "cancel_reason": note[:200]},
        )
        if not updated:
            # Lost the race — someone already moved it. Re-read and report.
            latest = await self.orders.get(order_id)
            if latest is None:
                raise NotFound("We could not find that order.")
            return latest

        # Return inventory exactly once, whatever combination of cancel and
        # refund paths ran.
        if await self.orders.mark_stock_released(order_id):
            for line in order["lines"]:
                await self.products.release_stock(line["sku"], line["qty"])
                await self.products.record_stock_movement(
                    sku=line["sku"], delta=line["qty"],
                    reason=f"order_{target_status.value}",
                    order_id=order_id, actor=actor,
                )

        # The CAS above only ever lets one caller past it for a given order
        # (a concurrent second cancel/refund gets `updated = None` above and
        # returns before reaching here), so this runs at most once per order —
        # no separate idempotency guard needed for the refund itself.
        if await self._maybe_refund(order, target_status=target_status):
            refreshed = await self.orders.get(order_id)
            if refreshed is not None:
                updated = refreshed

        await self._notify_customer(updated, target_status)
        return updated

    async def _maybe_refund(self, order: dict[str, Any], *, target_status: OrderStatus) -> bool:
        """Refund a captured online payment when its order is cancelled or
        force-refunded.

        Cash-on-delivery orders never reach here with anything to refund —
        nothing was ever charged. An online order that never got past
        `pending_payment` also has nothing captured, so this is a no-op for
        the far more common "changed my mind before paying" cancel too.
        """
        payment = order.get("payment") or {}
        if payment.get("method") != PaymentMethod.ONLINE.value:
            return False
        if payment.get("status") != PaymentStatus.CAPTURED.value:
            return False

        provider_payment_id = payment.get("provider_payment_id")
        if not provider_payment_id:
            log.error(
                "captured payment has no provider_payment_id on file; cannot refund",
                extra={"order": order["id"]},
            )
            return False

        try:
            await self.payments.refund(
                provider_payment_id=provider_payment_id,
                amount_paise=payment["amount_paise"],
                notes={"order_id": order["id"], "reason": target_status.value},
            )
        except UpstreamError:
            # The cancel itself already went through — stock is released and
            # the order is cancelled either way. A refund that fails here
            # needs a human to retry it against the gateway directly; it
            # should never block or unwind the cancel that already happened.
            log.exception("refund failed while cancelling order", extra={"order": order["id"]})
            return False

        await self.orders.set_payment_status(order["id"], PaymentStatus.REFUNDED)
        log.info("refund issued on cancel", extra={"order": order["id"]})
        return True

    async def update_address(
        self, *, order_id: str, user_id: str, address: Address
    ) -> OrderView:
        """Change the delivery address on an order still in the same window
        the customer can cancel from — once it's packed for pickup, changing
        where it's headed needs a person, not a form; see CUSTOMER_CANCELLABLE.
        """
        updated = await self.orders.update_address(
            order_id,
            user_id,
            address.model_dump(mode="json"),
            expected_statuses=[s.value for s in CUSTOMER_CANCELLABLE],
        )
        if updated is None:
            existing = await self.orders.get_for_user(order_id, user_id)
            if existing is None:
                raise NotFound("We could not find that order.")
            raise Forbidden(
                "This order is already being prepared for delivery, so the address can no "
                "longer be changed here. Call us and we will sort it out."
            )
        return self._to_view(updated)

    async def release_expired_holds(self, *, limit: int = 100) -> int:
        """Sweep abandoned checkouts back onto the shelf.

        Before cancelling anything, ask the gateway directly whether the
        payment actually went through (AAD-PAY-006). Neither the webhook nor
        the redirect callback is guaranteed to arrive — the whole point of
        this finding — so cancelling on the timer alone risks cancelling an
        order that was, in fact, paid for. Only orders the gateway also
        confirms as unpaid are cancelled here; an order the gateway reports
        as paid is confirmed instead, exactly as a capture webhook would.
        """
        stale = await self.orders.find_expired_holds(limit=limit)
        released = 0
        for order in stale:
            provider_order_id = (order.get("payment") or {}).get("provider_order_id")
            if provider_order_id:
                try:
                    event = await self.payments.poll_status(provider_order_id=provider_order_id)
                except UpstreamError:
                    # Can't confirm either way right now — leaving the hold in
                    # place for the next sweep is safer than guessing.
                    log.warning(
                        "could not reach gateway to reconcile an expiring hold",
                        extra={"order": order["id"]},
                    )
                    continue
                if event is not None:
                    await self.apply_webhook(event)
                    continue
            await self._cancel(order, note="Payment not completed in time", actor="system")
            released += 1
        if released:
            log.info("expired payment holds released", extra={"count": released})
        return released

    # ---------- reads ----------

    async def get_for_user(self, order_id: str, user_id: str) -> OrderView:
        order = await self.orders.get_for_user(order_id, user_id)
        if not order:
            raise NotFound("We could not find that order.")
        agent_location = await self._agent_location_if_visible(order)
        return self._to_view(order, agent_location=agent_location)

    async def _agent_location_if_visible(
        self, order: dict[str, Any]
    ) -> tuple[float, float, datetime] | None:
        """Only ever shown to the customer while the order is genuinely out
        for delivery — nothing useful (or appropriate) to show before pickup
        or after drop-off. `self.users` is None on paths that don't need this
        (e.g. background jobs), same guard shape as the push helpers above.
        """
        if not self.users:
            return None
        if OrderStatus(order["status"]) is not OrderStatus.OUT_FOR_DELIVERY:
            return None
        agent_id = order.get("delivery_agent_id")
        if not agent_id:
            return None
        return await self.users.get_agent_location_with_time(agent_id)

    async def list_for_user(self, user_id: str, *, limit: int = 20) -> list[OrderView]:
        return [self._to_view(o) for o in await self.orders.list_for_user(user_id, limit=limit)]

    # ---------- helpers ----------

    @staticmethod
    def _fingerprint(request: CreateOrderRequest) -> str:
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    @staticmethod
    def _to_view(
        order: dict[str, Any],
        *,
        checkout_payload: dict | None = None,
        agent_location: tuple[float, float, datetime] | None = None,
    ) -> OrderView:
        payment = dict(order["payment"] or {})
        if checkout_payload is not None:
            payment["checkout_payload"] = checkout_payload
        payment.pop("provider_payment_id", None)  # internal; not for the client

        status = OrderStatus(order["status"])
        return OrderView(
            id=order["id"],
            order_number=order["order_number"],
            status=status,
            lines=order["lines"],
            subtotal_paise=order["subtotal_paise"],
            delivery_fee_paise=order["delivery_fee_paise"],
            discount_paise=order.get("discount_paise", 0),
            total_paise=order["total_paise"],
            currency=order.get("currency", "INR"),
            address=order["address"],
            notes=order.get("notes", ""),
            payment=payment,
            eta_minutes=order.get("eta_minutes", 0),
            timeline=order.get("timeline", []),
            created_at=order["created_at"],
            updated_at=order["updated_at"],
            can_cancel=status in CUSTOMER_CANCELLABLE,
            can_edit_address=status in CUSTOMER_CANCELLABLE,
            delivery_agent_location=(
                {
                    "latitude": agent_location[0],
                    "longitude": agent_location[1],
                    "updated_at": agent_location[2],
                }
                if agent_location
                else None
            ),
        )
