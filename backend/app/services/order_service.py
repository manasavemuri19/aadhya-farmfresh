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

If step 4 fails, the gateway order is orphaned. That is a strictly better
failure than the alternative, where a gateway timeout leaves stock reserved
for an order that does not exist — but "orphaned" used to just mean "left to
expire unpaid on its own", which is wrong: it is still live and payable for
however long its hold lasts, with nothing in this app pointing at it. If it
were paid in that window, the money would be captured against an order that
was never written (AAD-PAY-016). So step 4's failure path now also cancels
the gateway order it orphaned, best-effort, before letting the original
error propagate — see `PaymentProvider.cancel_order`. That narrows the
exposure to the (smaller, but real) case where the cancel call itself can't
reach the gateway, or loses a race against the customer paying in the same
instant; either way, the order still expires on its own at its `expire_by`
as the last backstop.

Stock is still reserved before payment completes. For fresh dairy that is the
right trade — overselling the last two litres costs a phone call and a refund,
while briefly holding stock that then expires costs nothing. Abandoned
checkouts are swept back onto the shelf after fifteen minutes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.errors import (
    CodUnavailableError,
    Conflict,
    Forbidden,
    NotFound,
    OutOfStock,
    PriceChanged,
    UpstreamError,
)
from app.core.ids import new_id, new_order_id, new_payment_id
from app.core.outbox import defer_until_commit
from app.core.security import hash_secret, verify_secret
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus, StockPolicy
from app.domain.order_state import (
    CANCEL_OR_REFUND_TARGETS,
    CUSTOMER_CANCELLABLE,
    assert_transition,
    releases_stock,
)
from app.payments.base import PaymentProvider, WebhookEvent
from app.repositories.cash import CashRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.orders import OrderRepository
from app.repositories.products import ProductRepository
from app.repositories.support import SupportRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.catalog import Variant
from app.schemas.common import Page
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

# AAD-SEC-027: in-app, phone-number-free proof-of-delivery. 4 digits is
# plenty of entropy for a code that's shown once on a screen and typed once
# by a delivery agent standing in front of the customer — the real defence
# against brute force is DELIVERY_OTP_MAX_ATTEMPTS, not the digit count. A
# 24-hour TTL bounds how long a code sits usable without the code otherwise
# sitting around for days per the fix's own spec; a well-behaved delivery
# still completes in minutes to hours, and 24h comfortably covers a stuck
# or overnight delivery without needing staff to intervene for anything
# routine.
DELIVERY_OTP_LENGTH = 4
DELIVERY_OTP_TTL = timedelta(hours=24)
DELIVERY_OTP_MAX_ATTEMPTS = 5

# What update_status writes into an order's delivery_otp_* columns the
# moment it stops being usable — success (order delivered), or the order
# leaving out_for_delivery any other way (cancelled/refunded, via _cancel's
# own extra_set below). Kept as one shared constant so "cleared" always
# means exactly these four values everywhere it's written.
_CLEARED_DELIVERY_OTP: dict[str, Any] = {
    "delivery_otp_hash": None,
    "delivery_otp_plain": None,
    "delivery_otp_expires_at": None,
    "delivery_otp_attempts": 0,
}

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
        support: SupportRepository | None = None,
        cash: CashRepository | None = None,
    ) -> None:
        self.products = products
        self.orders = orders
        self.idempotency = idempotency
        self.payments = payments
        # All optional, all default None: see get_order_service in deps.py
        # for why (the background housekeeping sweeper builds this class
        # directly and may not wire every one of them).
        self.users = users
        self.push = push
        # AAD-PAY-005: the ticket an amount-mismatch webhook opens. A plain
        # local insert (session.add + flush), not a gateway call, so — unlike
        # the refund itself — it's safe to do inline, in the same request/
        # sweep transaction as everything else in apply_webhook.
        self.support = support
        # AAD-BIZ-004: records a COD collection the instant verify_delivery_code
        # succeeds for a COD order — see that method below. None is a real,
        # supported state (e.g. the sweeper, which never verifies deliveries),
        # not just a fallback: verify_delivery_code checks for it explicitly
        # rather than assuming it's always wired.
        self.cash = cash

    # ---------- notifications ----------
    # Best-effort side effects on top of the state changes above — never
    # allowed to affect whether a transition itself succeeds.
    #
    # AAD-REL-004: this section's comment used to claim "every call site
    # below is *after* the database write already committed" — false.
    # `OrderRepository.transition()` ends in `session.flush()`, not
    # `commit()`; the actual commit happens in the request's commit-owning
    # boundary (`TransactionalRoute`, or `session_scope()` for the sweeper),
    # which runs *after* these methods return. Both DB reads here (the
    # order dict already in hand; `list_delivery_agent_ids()` below) stay
    # inline — cheap, same-session, no network call, no timing hazard. Only
    # the outbound push itself is deferred via `defer_until_commit`, which
    # queues it on a per-request/per-sweep-pass batch that the commit
    # boundary drains *after* `commit()` actually succeeds, and never at
    # all if it doesn't (see core/outbox.py). That closes both problems the
    # finding named: a push for an order that turned out not to exist, and
    # an outbound HTTP call made while still holding this transaction's row
    # locks (the same class of bug `AAD-PAY-003` fixed for gateway calls).

    async def _notify_customer(self, order: dict[str, Any], new_status: OrderStatus) -> None:
        if not self.push:
            return
        copy = _STATUS_PUSH_COPY.get(new_status)
        if not copy:
            return
        title, body_template = copy
        push = self.push
        user_id = order["user_id"]
        body = body_template.format(order_number=order["order_number"])
        data = {"order_id": order["id"], "status": new_status.value}
        await defer_until_commit(
            lambda: push.notify_users([user_id], title=title, body=body, data=data)
        )

    async def _notify_agents_new_order(self, order: dict[str, Any]) -> None:
        if not self.push or not self.users:
            return
        agent_ids = await self.users.list_delivery_agent_ids()
        if not agent_ids:
            return
        push = self.push
        body = f"Order {order['order_number']} is ready for pickup."
        data = {"order_id": order["id"]}
        await defer_until_commit(
            lambda: push.notify_users(
                agent_ids, title="New order available", body=body, data=data
            )
        )

    # ---------- quoting ----------

    async def quote(self, lines: list[CartLineInput]) -> Quote:
        priced, products_by_sku, _stock_policy_by_sku = await self._price(lines)
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
                    max_qty=line.max_qty,
                    adjustment_reason=line.adjustment_reason,
                )
                for line in cart.lines
            ],
            subtotal_paise=cart.subtotal_paise,
            delivery_fee_paise=cart.delivery_fee_paise,
            total_paise=cart.total_paise,
            currency=settings.currency,
            free_delivery_threshold_paise=settings.free_delivery_threshold_paise,
            min_order_paise=settings.min_order_paise,
            meets_minimum=cart.meets_minimum,
            eta_minutes=cart.eta_minutes,
            has_adjustments=cart.has_adjustments,
        )

    async def _price(
        self, lines: list[CartLineInput]
    ) -> tuple[list[PricedLine], dict, dict[str, StockPolicy]]:
        # Collapse duplicate SKUs so two taps on the same item price as one line.
        merged: dict[str, int] = {}
        for line in lines:
            merged[line.sku] = merged.get(line.sku, 0) + line.qty

        resolved = await self.products.find_variants(list(merged))
        priced: list[PricedLine] = []
        products_by_sku: dict[str, Any] = {}
        # AAD-DATA-004: handed back so a caller that reserves/releases stock
        # (only _create_order_inner today) can tell which lines are
        # MADE_TO_ORDER — reserve_stock/release_stock never actually move
        # those variants' stock_qty, so the ledger entry for them must say
        # so too, rather than recording a movement that never happened.
        stock_policy_by_sku: dict[str, StockPolicy] = {}

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
            variant = Variant(**variant_doc)
            stock_policy_by_sku[sku] = variant.stock_policy
            priced.append(price_line(product, variant, qty))

        return priced, products_by_sku, stock_policy_by_sku

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

    async def _check_cod_eligibility(self, user_id: str, total_paise: int) -> None:
        """AAD-BIZ-002: COD used to confirm an order, reserve stock and push
        it to every delivery agent with nothing paid up front, no limit on
        how many of those a single account could have running at once, no
        cap on order value, and no history check at all — an account made
        in seconds via Google sign-in could place unlimited high-value COD
        orders to arbitrary addresses. Fresh dairy would be prepared,
        dispatched, refused, and thrown away.

        Two checks are always on:

          * a value cap (`cod_max_order_value_paise`), so one order can't
            put an unbounded amount of prepared stock at risk;
          * a concurrency cap (`cod_max_active_orders_per_user`), so the
            same account can't have several unfulfilled COD orders in
            flight at once — see `OrderRepository.count_active_cod_orders`.

        A third — `cod_requires_prior_delivery` — exists and is tested but
        defaults **off**: it blocks a brand-new account from using COD at
        all until one of its orders has actually been delivered, which
        trades away first-order conversion for fraud resistance. That's a
        product call, not an engineering one, and it's sitting in the
        end-of-engagement business-decision list rather than being turned
        on unilaterally.

        What this does NOT do, disclosed rather than silently skipped: the
        finding also asks for a per-user/per-address *refusal rate* that
        disables COD past a threshold. There is no structured signal
        anywhere in this codebase for *why* an order was cancelled —
        `cancel_reason` is free text truncated from whatever note the
        canceller happened to type, and a delivery agent cannot cancel an
        order at all today (only staff can, through that same reason-free
        path every other cancellation uses — see `delivery.py`). Building
        the refusal check properly means first adding a real cancellation-
        reason taxonomy to the shared cancel path every actor uses
        (customer, staff, gateway, sweep), which is a larger, cross-cutting
        change and not something to bolt on as a string match here.
        """
        if total_paise > settings.cod_max_order_value_paise:
            raise CodUnavailableError(
                "Cash on delivery isn't available for orders this large. "
                "Pay online to place this order.",
                details={
                    "limit_paise": settings.cod_max_order_value_paise,
                    "total_paise": total_paise,
                },
            )

        active = await self.orders.count_active_cod_orders(user_id)
        if active >= settings.cod_max_active_orders_per_user:
            raise CodUnavailableError(
                "You already have a cash-on-delivery order in progress. "
                "Pay online for this one, or wait for the other to be delivered.",
                details={"limit": settings.cod_max_active_orders_per_user},
            )

        if settings.cod_requires_prior_delivery and not await self.orders.has_delivered_order(
            user_id
        ):
            raise CodUnavailableError(
                "Cash on delivery unlocks after your first completed order. "
                "Pay online for this one."
            )

    async def _create_order_inner(
        self, user_id: str, request: CreateOrderRequest
    ) -> OrderView:
        priced, products_by_sku, stock_policy_by_sku = await self._price(request.lines)
        cart = build_cart(priced, products_by_sku)
        sellable = [line for line in cart.lines if line.is_sellable]

        if not sellable:
            raise OutOfStock("Nothing in your cart is available right now.")
        if cart.has_adjustments:
            # AAD-QUAL-013: this used to say "ran out" unconditionally, even
            # when every affected line was perfectly in stock and only over
            # the per-order limit — a permanent, self-inflicted dead end,
            # since re-quoting the identical cart failed identically with no
            # clue what to change. `reason` below is now the true one
            # (price_line already worked out which constraint bound), and
            # the headline message reflects it too: a customer over the
            # limit needs "reduce the quantity", not "try again later".
            affected = [
                line for line in cart.lines
                if line.adjusted_from_qty is not None or line.unavailable_reason
            ]
            reasons = {line.unavailable_reason or line.adjustment_reason for line in affected}
            message = (
                "One or more items are over the limit you can order per item. "
                "Reduce the quantity and try again."
                if reasons == {"quantity_limit"}
                else "Some items ran out while you were checking out. Review your cart."
            )
            raise OutOfStock(
                message,
                details={
                    "lines": [
                        {
                            "sku": line.sku,
                            "requested": line.adjusted_from_qty,
                            "available": line.qty,
                            "max_qty": line.max_qty,
                            "reason": line.unavailable_reason or line.adjustment_reason,
                        }
                        for line in affected
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
        if is_cod:
            await self._check_cod_eligibility(user_id, cart.total_paise)
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

        try:
            # --- everything below is one transaction, committed by the caller ---
            # AAD-PERF-008: used to call reserve_stock once per line in a serial
            # await loop — up to 2 round trips per line, holding row locks on
            # every SKU reserved so far for that much longer while later lines
            # were still being checked one at a time. reserve_stock_bulk does
            # the same compare-and-swap for every line in 2 round trips total,
            # regardless of cart size. Still reported as a single "X just sold
            # out" on the first failing line, same as before — rollback unwinds
            # every reservation in the (single) bulk statement automatically,
            # same as it unwound N separate statements before.
            reservations = await self.products.reserve_stock_bulk(
                [(line.sku, line.qty) for line in sellable]
            )
            for line in sellable:
                if not reservations.get(line.sku):
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

            # Reserved only now that stock is held and the order is actually going
            # to be written — next_order_number() increments a real counter row
            # (AAD-DATA-001), so calling it any earlier would burn numbers on
            # orders that end up rejected for being out of stock.
            order_number = await self.orders.next_order_number()

            await self.orders.insert(
                {
                    "id": order_id,
                    "order_number": order_number,
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
                # AAD-DATA-004: reserve_stock() above never actually decrements a
                # MADE_TO_ORDER variant's stock_qty — that's correct, there's no
                # shelf count to hold — but this loop used to record
                # `delta=-line.qty` for it anyway, writing a movement for stock
                # that never moved. `SUM(delta) GROUP BY sku` is the ledger's one
                # real purpose (reconstructing a discrepancy against
                # `variants.stock_qty`), and a phantom entry breaks it for every
                # made-to-order SKU, permanently. delta=0 keeps that sum correct;
                # the distinct reason keeps the entry itself honest about why.
                tracked = stock_policy_by_sku.get(line.sku) == StockPolicy.TRACKED
                await self.products.record_stock_movement(
                    sku=line.sku,
                    delta=-line.qty if tracked else 0,
                    reason="order_reserved" if tracked else "order_reserved_made_to_order",
                    order_id=order_id, actor=user_id,
                )

            order = await self.orders.get(order_id)
            if order is None:
                # AAD-QUAL-020: was `assert order is not None`. Same reasoning as
                # AAD-QUAL-005 — this order was created in this same transaction
                # a few lines above, so re-reading it here should be impossible
                # to come back empty; `python -O` would otherwise silently strip
                # the check and let the next line's `.total_paise` (etc.) fail
                # with an opaque AttributeError instead of this explicit one.
                raise RuntimeError(
                    f"order {order_id} vanished immediately after being created "
                    "inside this same transaction — should be impossible"
                )

            if is_cod:
                # A COD order is confirmed the instant it's placed — nothing to
                # wait on. Online orders get their "confirmed" notification later,
                # from apply_webhook, once payment actually clears.
                await self._notify_agents_new_order(order)

            return await self._to_view(
                order,
                checkout_payload=provider_order.checkout_payload if provider_order else None,
            )
        except Exception:
            # AAD-PAY-016: everything above this point is the one local
            # transaction — if any of it fails, the caller rolls it back and
            # no order is ever written. But the gateway call that created
            # `provider_order` (above, outside this transaction on purpose —
            # see this module's own docstring) already happened by then: a
            # failure here leaves a real, live, payable gateway order behind
            # with nothing in this app pointing at it at all. Best-effort:
            # cancel it now rather than letting it simply sit payable until
            # its own `expire_by` — see `PaymentProvider.cancel_order`.
            if provider_order is not None:
                await self.payments.cancel_order(
                    provider_order_id=provider_order.provider_order_id
                )
            raise

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
                payment_status = (order.get("payment") or {}).get("status")
                if payment_status in (
                    PaymentStatus.AMOUNT_MISMATCH.value,
                    PaymentStatus.REFUNDED.value,
                ):
                    # Already flagged (or already refunded by the sweep) by
                    # an earlier delivery of this same event — most likely
                    # `release_expired_holds` re-polling before a human has
                    # gotten to the ticket this already raised. Nothing new
                    # to do, and re-flagging would re-queue a refund against
                    # a payment that may already be refunded.
                    log.info(
                        "duplicate amount-mismatch event ignored",
                        extra={"order": order_id},
                    )
                    return
                # AAD-PAY-005: refusing to confirm is right, but a log line
                # alone leaves the money stranded — it's already captured at
                # the gateway, against a payment row that still says
                # CREATED, so `_maybe_refund` never picks it up. Flag it
                # (queues the refund for the sweep, since a gateway call
                # doesn't belong in this transaction — AAD-PAY-003) and open
                # a ticket so a human sees it: a mismatch is either a
                # gateway bug or an attack, and both need one.
                await self.orders.flag_amount_mismatch(
                    order_id,
                    provider_payment_id=event.provider_payment_id or "",
                    received_amount_paise=event.amount_paise,
                )
                log.error(
                    "webhook amount mismatch — refund queued, ticket opened",
                    extra={
                        "order": order_id,
                        "expected": order["total_paise"],
                        "received": event.amount_paise,
                    },
                )
                if self.support:
                    await self.support.create(
                        user_id=order["user_id"],
                        message=(
                            f"Automated: payment for order {order['order_number']} "
                            f"captured {event.amount_paise} paise, expected "
                            f"{order['total_paise']} paise. A refund of the "
                            "received amount has been queued automatically — "
                            "please confirm it completes and decide what, if "
                            "anything, the order itself needs."
                        ),
                        context_node_id="payment_amount_mismatch",
                    )
                return

            # AAD-QUAL-023: found while adding cross-channel duplicate-
            # delivery regression tests, not by the finding's own original
            # text. A capture event that reaches here after the order has
            # already been refunded — through this exact backstop, a
            # dashboard refund, or any other path — used to fall all the
            # way through to the unconditional `set_payment_status(...,
            # CAPTURED, ...)` below regardless of `current`, silently
            # overwriting `refund_pending`/`refunded` back to `captured`.
            # That's not cosmetic: `process_pending_refunds` selects its
            # work by `payment.status == refund_pending`
            # (`find_pending_refunds`), so a stray duplicate capture landing
            # in the window between `_maybe_refund` queuing the refund and
            # the next sweep actually calling the gateway would silently
            # remove the order from that list — no exception, no log
            # anyone would think to look for, just a refund that will now
            # never be attempted while the order itself still shows
            # REFUNDED. The exact-amount-match twin of the
            # already-existing `AMOUNT_MISMATCH`/`REFUNDED` guard a few
            # lines up, for the same reason.
            if current is OrderStatus.REFUNDED:
                log.info(
                    "duplicate capture event for an already-refunded order ignored",
                    extra={"order": order_id},
                )
                return

            await self.orders.set_payment_status(
                order_id, PaymentStatus.CAPTURED,
                provider_payment_id=event.provider_payment_id,
            )
            if current is OrderStatus.PENDING_PAYMENT:
                won = await self.orders.transition(
                    order_id,
                    expected_status=OrderStatus.PENDING_PAYMENT,
                    new_status=OrderStatus.CONFIRMED,
                    note="Payment received",
                    actor="payment_gateway",
                    extra_set={"hold_expires_at": None},
                )
                if won:
                    updated = await self.orders.get(order_id)
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
            # AAD-PAY-002: a refund issued straight from the gateway
            # dashboard (outside our own cancel flow) used to just flip the
            # payment status and stop — the order itself stayed CONFIRMED/
            # PACKED/OUT_FOR_DELIVERY/DELIVERED, so the goods still went
            # out (or had already gone out) for an order the customer was
            # never going to pay for.
            if current in CANCEL_OR_REFUND_TARGETS:
                # Already CANCELLED or REFUNDED — a replay of this event, or
                # a refund that arrived after some other path (our own
                # cancel/refund flow) already got here first. Nothing to do.
                log.info(
                    "refund event on an already-cancelled/refunded order ignored",
                    extra={"order": order_id},
                )
                return

            payment = order.get("payment") or {}
            if payment.get("status") != PaymentStatus.CAPTURED.value:
                # Nothing was ever captured (most likely still
                # PENDING_PAYMENT) — there is no route to REFUNDED for this
                # (see order_state.py) and nothing on our side was charged,
                # so a refund event here can't be acted on automatically.
                log.warning(
                    "refund webhook for an order with no captured payment",
                    extra={"order": order_id, "status": current.value},
                )
                return

            total = order["total_paise"]
            if event.amount_paise is None or event.amount_paise != total:
                # A partial (or amount-unknown) refund must never
                # auto-cancel the order — the customer keeps the goods and
                # any remaining balance is still owed, so this needs a
                # human, not an automatic state change.
                log.error(
                    "partial or amount-unknown refund received — order left "
                    "as is, needs manual review",
                    extra={
                        "order": order_id,
                        "status": current.value,
                        "refunded_amount": event.amount_paise,
                        "order_total": total,
                    },
                )
                return

            # A full refund from the gateway. Mark the payment refunded up
            # front so `_cancel`'s `_maybe_refund` (AAD-PAY-003) sees a
            # payment that is no longer CAPTURED and does not queue a
            # second, redundant refund call against a payment the gateway
            # has already refunded.
            await self.orders.set_payment_status(order_id, PaymentStatus.REFUNDED)
            refreshed = await self.orders.get(order_id)
            if refreshed is not None:
                await self._cancel(
                    refreshed,
                    note="Refunded via the payment gateway",
                    actor="payment_gateway",
                    target_status=OrderStatus.REFUNDED,
                )

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
        return await self._to_view(updated)

    async def update_status(
        self, *, order_id: str, new_status: OrderStatus, note: str, actor: str
    ) -> OrderView:
        order = await self.orders.get(order_id)
        if not order:
            raise NotFound("We could not find that order.")

        current = OrderStatus(order["status"])
        assert_transition(current, new_status)

        if new_status in CANCEL_OR_REFUND_TARGETS:
            cancelled = await self._cancel(
                order, note=note, actor=actor, target_status=new_status
            )
            return await self._to_view(cancelled)

        # AAD-SEC-027: the delivery-verification code's whole lifecycle
        # hangs off this one CAS transition, atomically with the status
        # change itself — the same extra_set mechanism _cancel already uses
        # for hold_expires_at/cancel_reason. Generated the moment an order
        # actually goes out_for_delivery (not at order creation — see
        # _generate_delivery_otp), cleared the moment it becomes delivered,
        # however it got there: normally via verify_delivery_code below,
        # but a staff override through /admin/orders/{id}/status is
        # deliberately still allowed to set DELIVERED directly (e.g. a lost
        # phone, a customer who never opens the app) and must clear the
        # code just the same.
        extra_set: dict[str, Any] | None = None
        if new_status is OrderStatus.OUT_FOR_DELIVERY:
            extra_set = self._generate_delivery_otp()
        elif new_status is OrderStatus.DELIVERED:
            extra_set = _CLEARED_DELIVERY_OTP

        won = await self.orders.transition(
            order_id,
            expected_status=current,
            new_status=new_status,
            note=note,
            actor=actor,
            extra_set=extra_set,
        )
        if won is None:
            # AAD-QUAL-019: distinguishable now from "someone else moved it"
            # below — this order was read successfully a few lines up, in
            # this same transaction, so this is only reachable if it's
            # somehow gone by the time the CAS runs (nothing in this
            # codebase deletes orders; kept as the honest response to a
            # state this defensive check can now actually tell apart).
            raise NotFound("We could not find that order.")
        if not won:
            raise Conflict("That order changed while you were updating it. Refresh and retry.")
        updated = await self.orders.get(order_id)
        if updated is None:
            raise NotFound("We could not find that order.")
        await self._notify_customer(updated, new_status)
        if new_status is OrderStatus.CONFIRMED:
            # Reachable when staff manually confirm an order rather than it
            # arriving via checkout/webhook (e.g. a COD edge case handled by
            # hand) — the delivery pool still needs to hear about it either way.
            await self._notify_agents_new_order(updated)
        return await self._to_view(updated)

    def _generate_delivery_otp(self) -> dict[str, Any]:
        """AAD-SEC-027: a fresh 4-digit delivery-verification code, generated
        the moment an order becomes out_for_delivery (never at order
        creation — see DELIVERY_OTP_TTL's own comment) and never derived
        from anything about the order itself.

        `secrets.randbelow`, not `random` — this is a short-lived
        credential, and using a non-cryptographic PRNG here would be a
        needless weakening even though DELIVERY_OTP_MAX_ATTEMPTS is the
        real defence against brute force, not the digit count.

        Returns the extra_set dict for the CAS transition to write. Both a
        hash and a short-lived plaintext copy are kept — see the
        introducing migration's own docstring for why: verification (below)
        never trusts the plaintext column, only the Argon2 hash, so a
        stale/compromised read of `delivery_otp_plain` alone can't forge a
        delivery confirmation. The plaintext exists only so the customer's
        own authenticated `GET /orders/{id}` can keep re-showing their code
        on repeat app visits for the rest of the (bounded) delivery window.
        """
        code = f"{secrets.randbelow(10**DELIVERY_OTP_LENGTH):0{DELIVERY_OTP_LENGTH}d}"
        return {
            "delivery_otp_hash": hash_secret(code),
            "delivery_otp_plain": code,
            "delivery_otp_expires_at": datetime.now(UTC) + DELIVERY_OTP_TTL,
            "delivery_otp_attempts": 0,
        }

    async def verify_delivery_code(
        self, *, order_id: str, code: str, actor: str
    ) -> OrderView:
        """AAD-SEC-027: the delivery agent's half of in-app proof-of-delivery.

        Whether this order is even assigned to the agent submitting the
        code is DeliveryService's concern (same split as update_status
        above, which also doesn't know about agent ownership) — this only
        knows about the order's own lifecycle state and its stored code.

        Verification always re-checks the submitted code against
        `delivery_otp_hash` via `verify_secret` (Argon2) — it never trusts
        `delivery_otp_plain`, even though that column holds the same value,
        so a compromised read path alone is never enough to fake a
        delivery. A wrong guess costs an attempt (best-effort counted, see
        OrderRepository.record_delivery_otp_attempt) rather than raising
        immediately at the transition layer, so the agent's app can show
        "2 attempts left" instead of a bare failure.
        """
        order = await self.orders.get(order_id)
        if not order:
            raise NotFound("We could not find that order.")

        current = OrderStatus(order["status"])
        if current is not OrderStatus.OUT_FOR_DELIVERY:
            raise Conflict("This order isn't out for delivery right now.")

        otp_hash = order.get("delivery_otp_hash")
        expires_at = order.get("delivery_otp_expires_at")
        attempts = order.get("delivery_otp_attempts") or 0

        if (
            not otp_hash
            or expires_at is None
            or datetime.now(UTC) >= expires_at
            or attempts >= DELIVERY_OTP_MAX_ATTEMPTS
        ):
            raise Conflict(
                "This delivery code has expired or is locked. Ask the customer to "
                "reopen the app, or ask staff to reissue it."
            )

        if not verify_secret(code, otp_hash):
            new_count = await self.orders.record_delivery_otp_attempt(order_id)
            used = new_count if new_count is not None else attempts + 1
            remaining = max(0, DELIVERY_OTP_MAX_ATTEMPTS - used)
            if remaining:
                raise Conflict(f"That code doesn't match. {remaining} attempt(s) left.")
            raise Conflict("That code doesn't match. No attempts left — ask staff to reissue it.")

        updated_view = await self.update_status(
            order_id=order_id,
            new_status=OrderStatus.DELIVERED,
            note="Delivery verified by code",
            actor=actor,
        )

        # AAD-BIZ-004: recorded off a genuine code verification specifically
        # — not any path to DELIVERED. A staff override through
        # /admin/orders/{id}/status still bypasses this deliberately (see
        # update_status's own extra_set comment): that's an exception path
        # (lost phone, customer never opens the app) with no guarantee cash
        # actually changed hands, so it stays a manual reconciliation matter
        # rather than an automatic "collected" record. This path, entering
        # the customer's own in-app code, is the one place the agent is
        # provably standing in front of the customer right now — if their
        # payment method is COD, that cash is in the agent's hand at this
        # exact moment.
        payment = order.get("payment") or {}
        if self.cash is not None and payment.get("method") == PaymentMethod.COD.value:
            collection = await self.cash.record_collection(
                collection_id=new_id("codc", 20),
                order_id=order_id,
                agent_id=actor,
                amount_paise=payment["amount_paise"],
            )
            if collection is not None:
                # Closes the other half of AAD-BIZ-004: a delivered COD
                # order used to sit at payment.status = 'created' forever,
                # so revenue figures computed from status = 'captured' read
                # zero for the entire COD book. Only flipped when a new
                # collection row was actually written (not on the
                # structural-defence-in-depth conflict path above) — a
                # payment that's somehow already captured is left alone
                # rather than re-stamped.
                await self.orders.set_payment_status(order_id, PaymentStatus.CAPTURED)

        return updated_view

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

        won = await self.orders.transition(
            order_id,
            expected_status=current,
            new_status=target_status,
            note=note,
            actor=actor,
            extra_set={
                "hold_expires_at": None,
                "cancel_reason": note[:200],
                # AAD-SEC-027: OUT_FOR_DELIVERY can route straight into
                # cancelled/refunded (a bike accident, a gateway-initiated
                # refund) without ever passing through DELIVERED — this is
                # the terminal-state hygiene for that path, so a stale code
                # never outlives the order it was issued for.
                **_CLEARED_DELIVERY_OTP,
            },
        )
        if won is None:
            # AAD-QUAL-019: previously indistinguishable from the "someone
            # already moved it" branch below. `_cancel` is called from
            # several places (webhook failure, hold-expiry sweep, a
            # customer or staff cancel) with the order read moments earlier
            # each time, and nothing in this codebase ever deletes an order
            # row — so, like `update_status` above, this branch shouldn't
            # be reachable in practice today. It's here because the
            # distinction is now free to make, not because a real path to
            # it is known.
            raise NotFound("We could not find that order.")
        if not won:
            # Lost the race — someone already moved it. Re-read and report.
            latest = await self.orders.get(order_id)
            if latest is None:
                raise NotFound("We could not find that order.")
            return latest

        # Resolve the stock outcome exactly once, whatever combination of
        # cancel and refund paths ran (`mark_stock_released` guards this
        # regardless of which of the two branches below actually runs).
        if await self.orders.mark_stock_released(order_id):
            if releases_stock(current, target_status):
                for line in order["lines"]:
                    # AAD-DATA-004: release_stock() reports whether it
                    # actually credited a row back — False for a
                    # MADE_TO_ORDER SKU, which reserve_stock() never
                    # decremented at order time either. The ledger entry now
                    # reflects that instead of recording a delta=+qty credit
                    # for stock that was never taken from in the first place.
                    moved = await self.products.release_stock(line["sku"], line["qty"])
                    await self.products.record_stock_movement(
                        sku=line["sku"],
                        delta=line["qty"] if moved else 0,
                        reason=(
                            f"order_{target_status.value}"
                            if moved else f"order_{target_status.value}_made_to_order"
                        ),
                        order_id=order_id, actor=actor,
                    )
            else:
                # AAD-PAY-004: the goods already left the building
                # (OUT_FOR_DELIVERY or DELIVERED) — this is a write-off,
                # not a restock. No stock_qty change, but still a ledger
                # entry (delta=0) so the loss is visible for a human to
                # reconcile, rather than just vanishing.
                for line in order["lines"]:
                    await self.products.record_stock_movement(
                        sku=line["sku"], delta=0,
                        reason=f"order_{target_status.value}_write_off",
                        order_id=order_id, actor=actor,
                    )

        # The CAS above only ever lets one caller past it for a given order
        # (a concurrent second cancel/refund gets `won = False` above and
        # returns before reaching here), so this runs at most once per order —
        # no separate idempotency guard needed for the refund itself.
        #
        # AAD-PERF-007: the view this method returns has to reflect whatever
        # `_maybe_refund` just did to the payment row (a plain UPDATE, so it
        # won't show up on `order`, the snapshot from before any of this
        # ran), which means a reload is unavoidable — but it only needs to
        # happen once, after that write, not once right after the CAS above
        # and then again here. Fetching it here, last, is what makes it one.
        await self._maybe_refund(order, target_status=target_status)
        updated = await self.orders.get(order_id)
        if updated is None:
            raise NotFound("We could not find that order.")

        await self._notify_customer(updated, target_status)
        return updated

    async def _maybe_refund(self, order: dict[str, Any], *, target_status: OrderStatus) -> bool:
        """Queue a refund for a captured online payment when its order is
        cancelled or force-refunded (AAD-PAY-003).

        This used to call the gateway right here — inside the same request
        transaction that just transitioned the order and released stock,
        holding write locks on your hottest inventory rows for the full
        HTTPS round trip to Razorpay, and with any exception other than
        `UpstreamError` rolling back a refund that may have already
        succeeded at the gateway. It no longer touches the network at all:
        it only flags the payment `refund_pending` (a plain DB write, same
        transaction, no round trip) and returns. `process_pending_refunds`,
        run by the periodic sweeper (`app/main.py`) — the same place
        `release_expired_holds` already makes its own gateway calls outside
        any customer-facing request — is what actually calls
        `payments.refund` and marks the payment `refunded`, with its own
        retry on the next sweep if the gateway is unreachable.

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

        if not payment.get("provider_payment_id"):
            log.error(
                "captured payment has no provider_payment_id on file; cannot refund",
                extra={"order": order["id"]},
            )
            return False

        await self.orders.set_payment_status(order["id"], PaymentStatus.REFUND_PENDING)
        log.info(
            "refund queued for the next sweep",
            extra={"order": order["id"], "target_status": target_status.value},
        )
        return True

    async def process_pending_refunds(self, *, limit: int = 50) -> int:
        """Actually call the gateway for refunds `_maybe_refund` queued
        (AAD-PAY-003). Runs from the periodic sweeper, never from a
        customer-facing request — a slow or hung Razorpay round trip here
        costs sweep latency, not an inventory lock, a request worker, or a
        customer waiting on it.

        Each order is refunded independently, and a failure here is caught
        broadly — not just `UpstreamError` — because this runs inside the
        sweeper's own shared transaction (`session_scope`, same as
        `release_expired_holds`): an exception this function lets escape
        would roll back every write the whole sweep iteration made,
        including other orders' refunds that already succeeded in the same
        pass. Whatever goes wrong, the payment simply stays `refund_pending`
        and is retried on the next sweep instead of being lost.
        """
        pending = await self.orders.find_pending_refunds(limit=limit)
        refunded = 0
        for order in pending:
            payment = order["payment"] or {}
            provider_payment_id = payment.get("provider_payment_id")
            if not provider_payment_id:
                log.error(
                    "payment stuck refund_pending with no provider_payment_id on file",
                    extra={"order": order["id"]},
                )
                continue
            try:
                await self.payments.refund(
                    provider_payment_id=provider_payment_id,
                    amount_paise=payment["amount_paise"],
                    notes={"order_id": order["id"], "reason": order["status"]},
                )
            except Exception:
                log.exception(
                    "refund attempt failed; left refund_pending for the next sweep",
                    extra={"order": order["id"]},
                )
                continue
            await self.orders.set_payment_status(order["id"], PaymentStatus.REFUNDED)
            refunded += 1
        if refunded:
            log.info("pending refunds processed", extra={"count": refunded})
        return refunded

    async def process_amount_mismatches(self, *, limit: int = 50) -> int:
        """Refund the amount a capture webhook actually reported when it
        didn't match the order total (AAD-PAY-005). Same shape as
        `process_pending_refunds` and for the same reason: the gateway call
        happens from the sweeper, not the request that received the
        webhook, and one order's failure is caught broadly so it can never
        roll back another order's refund that already succeeded in the same
        pass.

        Refunds the amount actually captured (`received_amount_paise`), not
        the order's expected total — the two are different by definition
        here.
        """
        mismatched = await self.orders.find_amount_mismatches(limit=limit)
        refunded = 0
        for order in mismatched:
            payment = order["payment"] or {}
            provider_payment_id = payment.get("provider_payment_id")
            received = payment.get("received_amount_paise")
            if not provider_payment_id or received is None:
                log.error(
                    "payment stuck amount_mismatch with no provider_payment_id "
                    "or received amount on file",
                    extra={"order": order["id"]},
                )
                continue
            try:
                await self.payments.refund(
                    provider_payment_id=provider_payment_id,
                    amount_paise=received,
                    notes={"order_id": order["id"], "reason": "amount_mismatch"},
                )
            except Exception:
                log.exception(
                    "amount-mismatch refund attempt failed; left flagged for "
                    "the next sweep",
                    extra={"order": order["id"]},
                )
                continue
            await self.orders.set_payment_status(order["id"], PaymentStatus.REFUNDED)
            refunded += 1
        if refunded:
            log.info("amount-mismatch refunds processed", extra={"count": refunded})
        return refunded

    async def update_address(
        self, *, order_id: str, user_id: str, address: Address
    ) -> OrderView:
        """Self-serve address edits on an existing order are disabled by
        product decision, not by accident: once an order is placed, its
        address no longer changes through this endpoint at all, at any
        status — including AAD-OPS-014's own docstring-vs-code ambiguity
        (this used to reuse CUSTOMER_CANCELLABLE, the *cancel* window, which
        left PACKED orders editable despite this method's own prose saying
        otherwise). `expected_statuses=[]` below means the compare-and-swap
        in `OrderRepository.update_address` never matches any row, so this
        always reports the same "can't change it here" outcome a real
        customer would get today — deliberately left wired up rather than
        deleted, in case that decision changes; nothing here needs
        `CUSTOMER_CANCELLABLE` (cancellation itself is untouched) or any
        other status set.
        """
        updated = await self.orders.update_address(
            order_id,
            user_id,
            address.model_dump(mode="json"),
            expected_statuses=[],
        )
        if updated is None:
            existing = await self.orders.get_for_user(order_id, user_id)
            if existing is None:
                raise NotFound("We could not find that order.")
            raise Forbidden(
                "The delivery address can't be changed once an order is placed. "
                "Call us and we will sort it out."
            )
        return await self._to_view(updated)

    async def retry_payment(self, *, order_id: str, user_id: str) -> OrderView:
        """AAD-DATA-005: a customer whose payment attempt expired, or whose
        first attempt failed, used to have no way to try again against the
        *same* order — `payments.order_id` was unique and the gateway
        order/Payment Link is a one-shot artefact, so there was nowhere to
        record a second attempt. The only path was cancelling and rebuilding
        the cart from scratch, re-reserving stock that might not still be
        there. This gets the existing order a fresh gateway order instead:
        same order, same reservation, a new Payment Link — no cancel, no
        rebuild.

        Deliberately narrower than the finding's own suggested fix (a full
        one-to-many `payment_attempts` table with a derived paid/unpaid
        order state). That reshape would ripple through every payment read
        in this codebase — `_to_dict`, every `order["payment"]` access in
        this file, `OrderView.payment`, the mobile app's own types and
        screens — to support a capability (partial payments, multiple
        *simultaneous* attempts) nothing here actually needs yet. What the
        finding's own text actually complains about is narrower: "the
        system has no way to create a new gateway order against the
        existing order." This closes exactly that, in place, at a fraction
        of the risk — real money-handling code is not where I want to be
        making the largest possible change when a smaller one closes the
        same gap. If partial/split payments become a real requirement
        later, that's the point to revisit the fuller redesign.

        Only a still-`PENDING_PAYMENT` online order is retryable, and only
        while its payment sits in `CREATED` or `FAILED` — a made-to-order
        item can be retried; cash-on-delivery has nothing to retry; a
        payment already `CAPTURED` (or beyond) means money already moved,
        so this must refuse rather than risk a second charge; and
        `AMOUNT_MISMATCH` needs a human (AAD-PAY-005), not an automatic
        retry.

        The new Payment Link is created before the compare-and-swap below
        that actually attaches it to this order, for the same reason
        `_create_order_inner` creates its own before opening a write — see
        this module's own docstring. If that swap loses the race (or
        anything else after it fails), the freshly created link is
        best-effort cancelled before the error propagates, same as there
        (AAD-PAY-016).
        """
        order = await self.orders.get_for_user(order_id, user_id)
        if not order:
            raise NotFound("We could not find that order.")
        if OrderStatus(order["status"]) is not OrderStatus.PENDING_PAYMENT:
            raise Forbidden(
                "This order is no longer waiting on payment, so there's nothing to retry."
            )
        payment = order.get("payment") or {}
        if payment.get("method") != PaymentMethod.ONLINE.value:
            raise Forbidden("Cash-on-delivery orders have no payment to retry.")
        if payment.get("status") not in (
            PaymentStatus.CREATED.value, PaymentStatus.FAILED.value,
        ):
            raise Forbidden(
                "This order's payment needs a closer look before it can be retried. "
                "Call us and we will sort it out."
            )

        hold_expires_at = datetime.now(UTC) + PAYMENT_HOLD
        provider_order = await self.payments.create_order(
            amount_paise=order["total_paise"],
            currency=order["currency"],
            # A fresh, unique receipt per attempt — Razorpay Payment Links'
            # own reference_id (which `receipt` becomes; see
            # RazorpayProvider.create_order) must be unique per link, so
            # reusing order_id here a second time would be rejected by the
            # gateway itself. The real order_id still travels in `notes`
            # for reconciliation, and apply_webhook's fallback match on
            # `provider_order_id` (AAD-PAY-007's docstring) is exactly what
            # finds the order when the fast-path reference_id lookup misses.
            receipt=new_id("retry", 14),
            notes={"order_id": order_id, "user_id": user_id, "retry": "true"},
            expires_at=hold_expires_at,
        )

        try:
            won = await self.orders.retry_payment(
                order_id,
                provider=provider_order.provider,
                provider_order_id=provider_order.provider_order_id,
                checkout_payload=provider_order.checkout_payload,
                hold_expires_at=hold_expires_at,
            )
            if not won:
                raise Conflict(
                    "This order changed while you were retrying payment. "
                    "Refresh and check its status."
                )

            updated = await self.orders.get_for_user(order_id, user_id)
            if updated is None:
                raise NotFound("We could not find that order.")
            # AAD-PERF-009: pass the fresh payload explicitly, same as order
            # creation does — see _to_view's docstring for why this is no
            # longer implicit via the persisted column.
            return await self._to_view(
                updated, checkout_payload=provider_order.checkout_payload
            )
        except Exception:
            # AAD-PAY-016: same reasoning as _create_order_inner's own
            # matching except block — provider_order already exists, live
            # and payable, at the gateway by this point. If the CAS above
            # lost (order changed underneath this retry — cancelled,
            # expired, or another retry won first) or anything else here
            # fails, nothing in this app ends up pointing at that link.
            # Best-effort cancel before the original error propagates.
            await self.payments.cancel_order(
                provider_order_id=provider_order.provider_order_id
            )
            raise

    async def release_expired_holds(self, *, limit: int = 100) -> int:
        """Sweep abandoned checkouts back onto the shelf.

        Before cancelling anything, ask the gateway directly whether the
        payment actually went through (AAD-PAY-006). Neither the webhook nor
        the redirect callback is guaranteed to arrive — the whole point of
        this finding — so cancelling on the timer alone risks cancelling an
        order that was, in fact, paid for. Only orders the gateway also
        confirms as unpaid are cancelled here; an order the gateway reports
        as paid is confirmed instead, exactly as a capture webhook would.

        AAD-REL-005 — two changes from the original all-in-one-sweep shape:

        1. Rows are claimed one at a time with `FOR UPDATE SKIP LOCKED`
           (`claim_expired_hold`), not selected in bulk with no lock at all.
           Two replicas racing the same sweep now partition the work instead
           of both doing it and contending on the same order/inventory rows.
        2. Each order's processing runs inside its own SAVEPOINT
           (`session.begin_nested()`). A failure on any one order rolls back
           only that order's own writes, not the whole sweep — including
           whatever this same pass already committed for earlier orders.
           `except Exception` below is deliberately broad: an order this
           sweep can't process is logged and skipped, not allowed to abort
           the housekeeping run that's also doing idempotency-key and
           refresh-token cleanup in the same outer transaction
           (`main.py`'s `_run_sweep_once`).

        The gateway/push calls this loop makes were already out of the
        transaction before this fix, so neither needed to move again here:
        `_maybe_refund` only flags `refund_pending`, a plain DB write
        (AAD-PAY-003); `_notify_customer` only queues via
        `defer_until_commit`, fired after commit (AAD-REL-004).

        What this does NOT do: give each order its own database
        *connection*. That would let inventory-row locks release
        incrementally over a long sweep instead of all at once when the
        whole sweep transaction commits — genuinely better under high
        volume, but it means this method opening its own `session_scope()`
        per order instead of reusing the session it was constructed with,
        which the sweeper (`main.py`) also uses for the cleanup steps
        that run alongside this one. Every test exercising this method
        today drives it against a single, uncommitted `session` fixture,
        which that shape is incompatible with. Left as a further step if
        sweep duration or lock contention actually becomes a problem at
        this shop's order volume — SKIP LOCKED and the savepoint above
        already remove the two failure modes the finding actually
        reported (duplicate work across replicas, and one bad order
        undoing every earlier success in the same pass).
        """
        candidate_ids = await self.orders.list_expired_hold_ids(limit=limit)
        released = 0
        skipped_locked = 0
        failed = 0
        for order_id in candidate_ids:
            order = await self.orders.claim_expired_hold(order_id)
            if order is None:
                # Already claimed by another replica (or another concurrent
                # claim), or no longer eligible since the candidate list was
                # read — either way, a normal, silent skip, not an error.
                skipped_locked += 1
                continue
            try:
                async with self.orders.session.begin_nested():
                    provider_order_id = (order.get("payment") or {}).get("provider_order_id")
                    if provider_order_id:
                        try:
                            event = await self.payments.poll_status(
                                provider_order_id=provider_order_id
                            )
                        except UpstreamError:
                            # Can't confirm either way right now — leaving the
                            # hold in place for the next sweep is safer than
                            # guessing. Nothing was written this iteration, so
                            # releasing the (empty) savepoint here is a no-op.
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
            except Exception:
                # This order's savepoint has already been rolled back by the
                # `async with` above — every earlier order's already-released
                # savepoint in this same pass is untouched.
                failed += 1
                log.exception(
                    "failed to process one expired hold — continuing with the rest of the sweep",
                    extra={"order": order_id},
                )
        if released or skipped_locked or failed:
            log.info(
                "expired payment hold sweep finished",
                extra={
                    "candidates": len(candidate_ids),
                    "released": released,
                    "skipped_locked": skipped_locked,
                    "failed": failed,
                },
            )
        return released

    # ---------- reads ----------

    async def get_for_user(self, order_id: str, user_id: str) -> OrderView:
        order = await self.orders.get_for_user(order_id, user_id)
        if not order:
            raise NotFound("We could not find that order.")
        return await self._to_view(order)

    # AAD-SEC-033: the position shown to the customer used to be the agent's
    # last reported location *anywhere*, with no freshness bound — a fix
    # reported once at the agent's home before they left for the shift would
    # be served, unchanged, to every customer's out-for-delivery order all
    # day. `updated_at` was already returned so the client *could* judge
    # staleness, but nothing on the server actually did. Refuse to serve
    # anything older than this.
    _AGENT_LOCATION_MAX_AGE = timedelta(minutes=5)

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
        location = await self.users.get_agent_location_with_time(agent_id)
        if location is None:
            return None
        _, _, reported_at = location
        if datetime.now(UTC) - reported_at > self._AGENT_LOCATION_MAX_AGE:
            return None
        return location

    @staticmethod
    def _delivery_code_if_usable(order: dict[str, Any]) -> str | None:
        """AAD-SEC-027: what lets the customer's own `GET /orders/{id}` keep
        re-showing their code on repeat app visits during the delivery
        window — reads `delivery_otp_plain` (never used for verification
        itself, only for display; see verify_delivery_code), gated on the
        same expiry check that verification enforces, so a code the backend
        would already reject as expired is never shown as if it still
        worked. No status check beyond that is needed: `delivery_otp_plain`
        is only ever non-null while the order is genuinely out_for_delivery
        — every other transition clears it in the same CAS write that
        changes the status (update_status/_cancel's own extra_set).
        """
        plain = order.get("delivery_otp_plain")
        expires_at = order.get("delivery_otp_expires_at")
        if not plain or expires_at is None or datetime.now(UTC) >= expires_at:
            return None
        return plain

    async def list_for_user(
        self, user_id: str, *, limit: int = 20, before: datetime | None = None
    ) -> Page[OrderView]:
        """AAD-API-004: a customer used to be physically unable to see past
        their 20 most recent orders — `limit` was the only knob exposed and
        the repository's own `before` cursor never got wired up. `before`
        walks further back in time; `next_cursor` is that page's oldest
        order's `created_at`, ready to hand straight back as the next
        request's `before`."""
        rows = await self.orders.list_for_user(user_id, limit=limit, before=before)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1]["created_at"].isoformat() if has_more and rows else None
        # AAD-QUAL-021: sequential, not `asyncio.gather` — every repository
        # call in this class shares one request-scoped `AsyncSession`, which
        # SQLAlchemy's async engine does not support using concurrently.
        # Each `_to_view` is a cheap no-op past its own status/agent-id
        # guard for every order that isn't actually out for delivery, which
        # is the common case for a full page of a customer's order history.
        items = [await self._to_view(o) for o in rows]
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def list_queue_for_staff(
        self,
        statuses: list[OrderStatus],
        *,
        limit: int = 50,
        after: datetime | None = None,
    ) -> Page[OrderView]:
        """AAD-API-004: the staff work queue used to have a hard cap with no
        cursor at all — orders past it weren't an error, they simply never
        appeared to the people packing them. `after` walks the oldest-first
        queue forward. Also closes AAD-QUAL-028: this is the public
        `list_for_staff`-shaped entry point that finding asked for, so
        `routes/admin.py` no longer reaches into `_to_view` directly."""
        rows = await self.orders.list_by_status(statuses, limit=limit, after=after)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1]["created_at"].isoformat() if has_more and rows else None
        items = [await self._to_view(o) for o in rows]
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    # ---------- helpers ----------

    @staticmethod
    def _fingerprint(request: CreateOrderRequest) -> str:
        # AAD-QUAL-017: this used to hash the *entire* request body —
        # address and free-text notes included. A same-cart retry that
        # re-geocodes to a marginally different lat/lng, or re-types a note
        # with a trailing space, produced a different fingerprint and was
        # rejected with `Conflict("already used for a different order")`
        # even though nothing that actually defines the order — what's
        # being bought, how it's paid for, what it costs — changed.
        # Fingerprints now cover only that semantically meaningful subset:
        # SKUs and quantities in a deterministic order (a client could
        # plausibly resend the same cart with its lines reordered),
        # payment method, and the client's own expected total.
        lines = sorted(
            ((line.sku, line.qty) for line in request.lines), key=lambda pair: pair[0]
        )
        payload = {
            "lines": lines,
            "payment_method": request.payment_method.value,
            "expected_total_paise": request.expected_total_paise,
        }
        body = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    async def _to_view(
        self,
        order: dict[str, Any],
        *,
        checkout_payload: dict | None = None,
    ) -> OrderView:
        """AAD-QUAL-021: `agent_location` used to be a parameter the caller
        had to remember to compute and pass — `get_for_user` did, nothing
        else did, so a customer's order *list* (`list_for_user`) and the
        staff queue (`list_queue_for_staff`) silently never showed live
        tracking, not because it didn't apply to them but because nothing
        called `_agent_location_if_visible` on their behalf. `_to_view` is
        genuinely the one place every view-construction path already goes
        through, so it's now the one place that decides this too — no
        longer a `@staticmethod`, and every caller just awaits it.
        `_agent_location_if_visible`'s own guards (no `self.users`, wrong
        status, no assigned agent) make this a cheap no-op query for every
        order that isn't actually out for delivery, which is the common
        case everywhere but a single customer order mid-delivery.

        AAD-PERF-009: `order["payment"]` (straight from the repository) has
        always carried `checkout_payload` from the persisted column, so the
        old `if checkout_payload is not None: ...` here only ever *added*
        to whatever the DB dict already had — it never had a way to *not*
        show it. That meant every `GET /orders/{id}`, forever, echoed back
        the gateway's one-time-use payment-sheet payload from however long
        ago the order was created. `checkout_payload` is now always set
        explicitly from the parameter (default `None`), which discards the
        persisted value on every call site that doesn't pass a fresh one.
        Only order creation and payment retry — the two moments a client
        actually needs to open the payment sheet — pass it through.
        """
        agent_location = await self._agent_location_if_visible(order)
        payment = dict(order["payment"] or {})
        payment["checkout_payload"] = checkout_payload
        payment.pop("provider_payment_id", None)  # internal; not for the client
        payment.pop("received_amount_paise", None)  # internal; not for the client

        status = OrderStatus(order["status"])
        return OrderView(
            id=order["id"],
            order_number=order["order_number"],
            status=status,
            lines=order["lines"],
            subtotal_paise=order["subtotal_paise"],
            delivery_fee_paise=order["delivery_fee_paise"],
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
            # Product decision: address edits are off entirely, at every
            # status, not just tied to the cancel window (which stays as
            # CUSTOMER_CANCELLABLE above, unaffected). See update_address's
            # own docstring for the full reasoning.
            can_edit_address=False,
            delivery_agent_location=(
                {
                    "latitude": agent_location[0],
                    "longitude": agent_location[1],
                    "updated_at": agent_location[2],
                }
                if agent_location
                else None
            ),
            delivery_code=self._delivery_code_if_usable(order),
        )
