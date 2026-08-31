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

log = logging.getLogger(__name__)

PAYMENT_HOLD = timedelta(minutes=15)


class OrderService:
    def __init__(
        self,
        products: ProductRepository,
        orders: OrderRepository,
        idempotency: IdempotencyRepository,
        payments: PaymentProvider,
    ) -> None:
        self.products = products
        self.orders = orders
        self.idempotency = idempotency
        self.payments = payments

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

        # Gateway call happens before the write, so a slow gateway never holds
        # locks on inventory rows.
        provider_order = None
        if not is_cod:
            provider_order = await self.payments.create_order(
                amount_paise=cart.total_paise,
                currency=settings.currency,
                receipt=order_id,
                notes={"order_id": order_id, "user_id": user_id},
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
                "hold_expires_at": None if is_cod else now + PAYMENT_HOLD,
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
        if not event.provider_order_id:
            log.warning("webhook without provider order id", extra={"event": event.event_type})
            return

        order = await self.orders.get_by_provider_order_id(event.provider_order_id)
        if not order:
            log.warning("webhook for unknown order", extra={"po": event.provider_order_id})
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
                await self.orders.transition(
                    order_id,
                    expected_status=OrderStatus.PENDING_PAYMENT,
                    new_status=OrderStatus.CONFIRMED,
                    note="Payment received",
                    actor="payment_gateway",
                    extra_set={"hold_expires_at": None},
                )
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
        """Sweep abandoned checkouts back onto the shelf."""
        stale = await self.orders.find_expired_holds(limit=limit)
        for order in stale:
            await self._cancel(order, note="Payment not completed in time", actor="system")
        if stale:
            log.info("expired payment holds released", extra={"count": len(stale)})
        return len(stale)

    # ---------- reads ----------

    async def get_for_user(self, order_id: str, user_id: str) -> OrderView:
        order = await self.orders.get_for_user(order_id, user_id)
        if not order:
            raise NotFound("We could not find that order.")
        return self._to_view(order)

    async def list_for_user(self, user_id: str, *, limit: int = 20) -> list[OrderView]:
        return [self._to_view(o) for o in await self.orders.list_for_user(user_id, limit=limit)]

    # ---------- helpers ----------

    @staticmethod
    def _fingerprint(request: CreateOrderRequest) -> str:
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    @staticmethod
    def _to_view(order: dict[str, Any], *, checkout_payload: dict | None = None) -> OrderView:
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
        )
