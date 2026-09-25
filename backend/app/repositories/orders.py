"""Order persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order as OrderRow
from app.db.models import OrderEvent, OrderLine, OrderNumberCounter, Payment, WebhookEvent
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus

# AAD-BIZ-002: statuses a COD order sits in before it's either delivered
# (paid, done) or cancelled/refunded (nothing at stake any more) — these are
# the ones that mean stock is committed and a delivery agent may already be
# holding the goods, which is exactly the exposure the per-user cap limits.
_ACTIVE_COD_STATUSES = [
    OrderStatus.CONFIRMED.value,
    OrderStatus.PACKED.value,
    OrderStatus.OUT_FOR_DELIVERY.value,
]

# AAD-DATA-011: 60 days — long enough to cover any realistic post-incident
# investigation window (a chargeback dispute, a "the app charged me twice"
# support ticket), short enough that this doesn't become an unbounded,
# unretained store of payment data under the DPDP Act 2023. Deliberately in
# the middle of the fix's own suggested 30-90 day range; revisit if the farm
# settles on a different investigation SLA.
WEBHOOK_PAYLOAD_RETENTION = timedelta(days=60)

# AAD-DATA-006: what an erased account's past orders carry in `address`
# afterward. Shaped to still satisfy the `Address` schema's own validators
# (a real 6-digit pincode, coordinates left null rather than some fake pair
# that would need to fall inside AAD-SEC-020's serviceability bounds, and —
# since AAD-MOB-022's multi-address support gave `label` its own
# `min_length=1` — a non-empty placeholder label too) so an OrderView for
# one of these orders keeps deserializing cleanly.
_ERASED_ADDRESS: dict[str, Any] = {
    "label": "Removed", "line1": "Address removed at the customer's request",
    "line2": "", "landmark": "", "city": "", "pincode": "000000",
    "latitude": None, "longitude": None,
}


def _to_dict(row: OrderRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "order_number": row.order_number,
        "user_id": row.user_id,
        "status": row.status,
        "subtotal_paise": row.subtotal_paise,
        "delivery_fee_paise": row.delivery_fee_paise,
        "total_paise": row.total_paise,
        "currency": row.currency,
        "address": row.address,
        "notes": row.notes,
        "eta_minutes": row.eta_minutes,
        "stock_released": row.stock_released,
        "hold_expires_at": row.hold_expires_at,
        "cancel_reason": row.cancel_reason,
        "delivery_agent_id": row.delivery_agent_id,
        "delivery_otp_hash": row.delivery_otp_hash,
        "delivery_otp_plain": row.delivery_otp_plain,
        "delivery_otp_expires_at": row.delivery_otp_expires_at,
        "delivery_otp_attempts": row.delivery_otp_attempts,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
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
            for line in row.lines
        ],
        "timeline": [
            {"status": e.status, "at": e.at, "note": e.note, "by": e.by}
            for e in row.events
        ],
        "payment": {
            "method": row.payment.method,
            "status": row.payment.status,
            "amount_paise": row.payment.amount_paise,
            "provider": row.payment.provider,
            "provider_order_id": row.payment.provider_order_id,
            "provider_payment_id": row.payment.provider_payment_id,
            "checkout_payload": row.payment.checkout_payload,
            "received_amount_paise": row.payment.received_amount_paise,
        }
        if row.payment
        else None,
    }


def _loaded(stmt):
    """Refresh anything already in memory for this order.

    `populate_existing` matters more than it looks: status changes are applied
    with bulk UPDATE statements, which do not sync SQLAlchemy's identity map.
    Without this, a read following a transition in the same session would
    return the stale, pre-update object — the order would look unchanged and
    its new event would be missing.

    AAD-PERF-007: this used to also carry an explicit
    `.options(selectinload(OrderRow.lines), ...)` for lines/events/payment —
    but all three are already declared `lazy="selectin"` on `Order` itself
    (app/db/models.py), which is the *default* loader strategy for every
    query against this model, options or not. The explicit call was doing
    nothing a plain `select(OrderRow)` wasn't already going to do; it just
    looked like it was buying something.
    """
    return stmt.execution_options(populate_existing=True)


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_order_number(self, *, today: date | None = None) -> str:
        """Atomically reserve the next human order number: `AD-YYMMDD-NNNN`.

        Backed by one row per calendar date in `order_number_counters`.
        `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` is one round trip and
        Postgres serializes concurrent callers on that row's lock, so two
        orders placed in the same instant still get distinct numbers — no
        read-then-write race, and no separate uniqueness check needed. This
        replaces AAD-DATA-001's six-random-digits scheme.

        Uses UTC dates, matching every other timestamp in this codebase — the
        rollover therefore lands at 5:30am IST rather than local midnight,
        a known, deliberately small simplification rather than an oversight.
        """
        order_date = today or datetime.now(UTC).date()
        stmt = (
            insert(OrderNumberCounter)
            .values(order_date=order_date, last_value=1)
            .on_conflict_do_update(
                index_elements=[OrderNumberCounter.order_date],
                set_={"last_value": OrderNumberCounter.last_value + 1},
            )
            .returning(OrderNumberCounter.last_value)
        )
        seq = (await self.session.execute(stmt)).scalar_one()
        return f"AD-{order_date:%y%m%d}-{seq:04d}"

    async def insert(self, order: dict[str, Any]) -> None:
        """Persist an order, its lines, its first event and its payment row.

        All of it lands in the caller's transaction, so an order can never
        exist without its lines.
        """
        row = OrderRow(
            id=order["id"],
            order_number=order["order_number"],
            user_id=order["user_id"],
            status=order["status"],
            subtotal_paise=order["subtotal_paise"],
            delivery_fee_paise=order["delivery_fee_paise"],
            total_paise=order["total_paise"],
            currency=order["currency"],
            address=order["address"],
            notes=order["notes"],
            eta_minutes=order["eta_minutes"],
            stock_released=order["stock_released"],
            hold_expires_at=order["hold_expires_at"],
        )
        row.lines = [OrderLine(**line) for line in order["lines"]]
        row.events = [
            OrderEvent(
                status=event["status"], at=event["at"], note=event["note"], by=event["by"]
            )
            for event in order["timeline"]
        ]
        payment = order["payment"]
        row.payment = Payment(
            id=payment["id"],
            method=payment["method"],
            status=payment["status"],
            amount_paise=payment["amount_paise"],
            provider=payment["provider"],
            provider_order_id=payment["provider_order_id"],
            provider_payment_id=None,
            checkout_payload=payment.get("checkout_payload"),
        )
        self.session.add(row)
        await self.session.flush()

    async def get(self, order_id: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(_loaded(select(OrderRow)).where(OrderRow.id == order_id))
        ).scalars().first()
        return _to_dict(row) if row else None

    async def get_for_user(self, order_id: str, user_id: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                _loaded(select(OrderRow)).where(
                    OrderRow.id == order_id, OrderRow.user_id == user_id
                )
            )
        ).scalars().first()
        return _to_dict(row) if row else None

    async def get_by_provider_order_id(self, provider_order_id: str) -> dict[str, Any] | None:
        # AAD-DATA-007: `.first()` used to be resolving a genuine ambiguity
        # — nothing stopped two payment rows from sharing a provider order
        # id, so this could silently pick an arbitrary one of them. A
        # unique index on `payments.provider_order_id` (migration
        # 0016_payment_provider_order_unique) now makes that structurally
        # impossible for any non-null value, so `.first()` here is just
        # "the one match, if any" rather than "pick one arbitrarily".
        row = (
            await self.session.execute(
                _loaded(select(OrderRow))
                .join(Payment, Payment.order_id == OrderRow.id)
                .where(Payment.provider_order_id == provider_order_id)
            )
        ).scalars().first()
        return _to_dict(row) if row else None

    async def list_for_user(
        self, user_id: str, *, limit: int = 20, before: datetime | None = None
    ) -> list[dict[str, Any]]:
        """AAD-API-004: fetches one extra row past `limit` so the caller can
        tell "there are more" from "that was everything" without a second
        COUNT query — see OrderService.list_for_user, which trims it back
        off before returning."""
        stmt = (
            _loaded(select(OrderRow))
            .where(OrderRow.user_id == user_id)
            .order_by(OrderRow.created_at.desc())
            .limit(limit + 1)
        )
        if before:
            stmt = stmt.where(OrderRow.created_at < before)
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_dict(r) for r in rows]

    async def list_by_status(
        self,
        statuses: list[OrderStatus],
        *,
        limit: int = 50,
        after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """AAD-API-004: this backs the staff work queue. It used to have no
        cursor at all, so a busy morning with more than `limit` open orders
        made everything past the cap silently invisible to the people who
        pack them — not an error, just absent. `after` walks the oldest-first
        queue forward the same way `before` walks a customer's history
        backward; same one-extra-row trick as list_for_user, for the same
        reason."""
        stmt = (
            _loaded(select(OrderRow))
            .where(OrderRow.status.in_([s.value for s in statuses]))
            .order_by(OrderRow.created_at)          # a work queue: oldest first
            .limit(limit + 1)
        )
        if after:
            stmt = stmt.where(OrderRow.created_at > after)
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_dict(r) for r in rows]

    async def transition(
        self,
        order_id: str,
        *,
        expected_status: OrderStatus,
        new_status: OrderStatus,
        note: str = "",
        actor: str = "system",
        extra_set: dict[str, Any] | None = None,
    ) -> bool | None:
        """Compare-and-swap on status.

        `expected_status` sits in the WHERE clause, so a concurrent transition
        loses and reports failure rather than silently overwriting. This is
        what stops a payment webhook and a staff tap from clobbering each
        other.

        AAD-QUAL-019: returns `True` when the CAS won, `False` when a row
        with this id exists but its status didn't match `expected_status`
        (lost the race — someone else transitioned it first), and `None`
        when no order with this id exists at all. Previously both failure
        cases reported identically, so a caller couldn't tell "someone beat
        you to it" from "that order id was never real" without a second
        query of its own — every caller either did that extra query anyway
        (`_cancel`, below) or just assumed the row still existed (`update_status`,
        `apply_webhook`), which happens to be true today only because each of
        them already re-reads the order immediately before calling this. The
        extra `SELECT` here only ever runs on the failure path (`rowcount == 1`
        is still a single UPDATE on the success path, unchanged), and every
        current caller's `if won:` / `if not won:` check still works exactly
        as before, since both `False` and `None` are falsy.

        AAD-PERF-007: this used to end with `return await self.get(order_id)`
        — a full 4-query eager-loaded reload of the order, on every single
        status change, whether or not the caller actually wanted a fresh
        view back. It now just reports whether the CAS won, and it's up to
        each caller to `get()` (or `get_for_user()`) a view for itself,
        exactly once, only where it actually needs one. `_cancel` is the
        case that mattered most: it used to take that reload here and then
        immediately throw it away in favour of a *second* one once
        `_maybe_refund` finished, because the first one couldn't see the
        payment-status write that hadn't happened yet. It now defers its one
        real reload to after that write instead of doing both.
        """
        values: dict[str, Any] = {"status": new_status.value, **(extra_set or {})}
        result = await self.session.execute(
            update(OrderRow)
            .where(OrderRow.id == order_id, OrderRow.status == expected_status.value)
            .values(**values)
        )
        if result.rowcount != 1:
            exists = (
                await self.session.execute(select(OrderRow.id).where(OrderRow.id == order_id))
            ).scalar_one_or_none()
            return False if exists is not None else None

        self.session.add(
            OrderEvent(
                order_id=order_id,
                status=new_status.value,
                at=datetime.now(UTC),
                note=note,
                by=actor,
            )
        )
        await self.session.flush()
        return True

    async def record_delivery_otp_attempt(self, order_id: str) -> int | None:
        """AAD-SEC-027: bump the failed-verification counter for a bad
        delivery code, and hand back the new count.

        Not CAS-protected, unlike `transition` — this is a plain
        `UPDATE ... SET delivery_otp_attempts = delivery_otp_attempts + 1`.
        Same reasoning as `UserRepository.upsert_address`'s address-cap
        check: this is an anti-abuse limit (stop a stranger from
        brute-forcing a 4-digit space), not a correctness invariant, so a
        rare race under concurrent wrong guesses costing one undercounted
        attempt is an acceptable trade against the complexity of a
        second CAS loop here. Returns the row's new attempt count, or
        `None` if no such order exists (mirrors `transition`'s own
        None-means-no-row convention).
        """
        result = await self.session.execute(
            update(OrderRow)
            .where(OrderRow.id == order_id)
            .values(delivery_otp_attempts=OrderRow.delivery_otp_attempts + 1)
            .returning(OrderRow.delivery_otp_attempts)
        )
        await self.session.flush()
        return result.scalar_one_or_none()

    async def set_payment_status(
        self,
        order_id: str,
        status: PaymentStatus,
        *,
        provider_payment_id: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status.value}
        if provider_payment_id:
            values["provider_payment_id"] = provider_payment_id
        await self.session.execute(
            update(Payment).where(Payment.order_id == order_id).values(**values)
        )

    async def retry_payment(
        self,
        order_id: str,
        *,
        provider: str | None,
        provider_order_id: str | None,
        checkout_payload: dict[str, Any] | None,
        hold_expires_at: datetime,
    ) -> bool:
        """AAD-DATA-005: give an order's payment a fresh gateway order in
        place, without touching the order's own status or its stock
        reservation. `payments.order_id` is still unique — this replaces
        the existing row's gateway-facing fields rather than adding a
        second row — which is deliberately narrower than the finding's own
        "one-to-many payment_attempts table" suggestion; see
        OrderService.retry_payment's docstring for why that full
        redesign's blast radius (every payment read in this codebase, plus
        the mobile app, assumes exactly one payment per order) was scoped
        down to the part that actually closes the customer-facing gap:
        getting a dead order a live Payment Link again.

        Two guards, checked in this order on purpose: the order itself
        must still be `PENDING_PAYMENT` (never resurrect a payment link for
        an order the sweep or a webhook already moved on, in the instant
        between the caller's own check and this write — the money-unsafe
        direction), and only then is the payment row updated, guarded on
        its own status still being retryable. If the second guard loses a
        race the first one didn't (a captured/refunded webhook lands in
        that same instant), the order's hold is renewed but the stale
        payment row is left alone — a retry that silently doesn't fully
        take effect, not a live link on money that already moved.
        """
        order_result = await self.session.execute(
            update(OrderRow)
            .where(OrderRow.id == order_id, OrderRow.status == OrderStatus.PENDING_PAYMENT.value)
            .values(hold_expires_at=hold_expires_at)
        )
        if order_result.rowcount != 1:
            return False

        await self.session.execute(
            update(Payment)
            .where(
                Payment.order_id == order_id,
                Payment.status.in_([PaymentStatus.CREATED.value, PaymentStatus.FAILED.value]),
            )
            .values(
                status=PaymentStatus.CREATED.value,
                provider=provider,
                provider_order_id=provider_order_id,
                provider_payment_id=None,
                checkout_payload=checkout_payload,
            )
        )
        return True

    async def flag_amount_mismatch(
        self, order_id: str, *, provider_payment_id: str, received_amount_paise: int
    ) -> None:
        """AAD-PAY-005: record that the gateway captured a different amount
        than the order total. `received_amount_paise` is what the sweep in
        `process_amount_mismatches` refunds — the actual amount taken, not
        `amount_paise` (the order total we expected).

        Also clears `hold_expires_at`: the order this payment belongs to is
        (in the reachable case) still PENDING_PAYMENT, and leaving its hold
        in place would put it right back in front of `release_expired_holds`
        every sweep — which polls the gateway, gets the same mismatched
        amount again, and would re-flag and re-ticket this order forever.
        Clearing it takes the order out of that sweep for good; it now waits
        on the ticket this flag raised, same as any other case that needs a
        human rather than a timer.
        """
        await self.session.execute(
            update(Payment)
            .where(Payment.order_id == order_id)
            .values(
                status=PaymentStatus.AMOUNT_MISMATCH.value,
                provider_payment_id=provider_payment_id,
                received_amount_paise=received_amount_paise,
            )
        )
        await self.session.execute(
            update(OrderRow).where(OrderRow.id == order_id).values(hold_expires_at=None)
        )

    async def anonymize_addresses_for_user(self, user_id: str) -> int:
        """AAD-DATA-006 / AAD-API-002 (DPDP Act 2023 right to erasure): the
        one piece of this user's personal data that lives outside
        `UserRepository`'s reach — `orders.address` is a JSONB snapshot
        taken at checkout time, on every order, not a foreign key to the
        (already-erasable) saved-addresses table. The order rows themselves
        are kept — they're tax and accounting records — only the address
        each one is carrying gets replaced.

        `_ERASED_ADDRESS` deliberately still satisfies every constraint the
        `Address` schema would enforce if this were ever read back through
        it (a 6-digit pincode, no out-of-range coordinates) — an OrderView
        for one of this user's past orders must still deserialize cleanly
        after this runs, it just won't say where the order actually went
        anymore. Returns the number of orders touched, purely for the
        caller to log.
        """
        result = await self.session.execute(
            update(OrderRow).where(OrderRow.user_id == user_id).values(address=_ERASED_ADDRESS)
        )
        return result.rowcount

    async def update_address(
        self,
        order_id: str,
        user_id: str,
        address: dict[str, Any],
        *,
        expected_statuses: list[str],
    ) -> dict[str, Any] | None:
        """Compare-and-swap on status being one still worth calling
        "editable" — the same idea as `transition`, just guarding a field
        instead of moving one. A status change racing in between (staff
        marks it packed the instant before this lands) means this simply
        finds zero rows and reports "too late" rather than silently
        overwriting an address the farm has already dispatched against.
        """
        result = await self.session.execute(
            update(OrderRow)
            .where(
                OrderRow.id == order_id,
                OrderRow.user_id == user_id,
                OrderRow.status.in_(expected_statuses),
            )
            .values(address=address)
        )
        if result.rowcount != 1:
            return None
        return await self.get_for_user(order_id, user_id)

    async def mark_stock_released(self, order_id: str) -> bool:
        """Flip the release flag exactly once.

        `stock_released == False` in the WHERE clause is what stops inventory
        being credited twice when a cancel and a refund arrive together.
        """
        result = await self.session.execute(
            update(OrderRow)
            .where(OrderRow.id == order_id, OrderRow.stock_released.is_(False))
            .values(stock_released=True)
        )
        return result.rowcount == 1

    async def list_expired_hold_ids(self, *, limit: int = 100) -> list[str]:
        """AAD-REL-005: a cheap, unlocked candidate list for the sweep to
        iterate — no `FOR UPDATE`, so it never blocks on and never
        contends with another replica's own sweep. The actual claim (and
        re-check that the row is still eligible) happens per-id in
        `claim_expired_hold`, so a stale id here — claimed by another
        replica, or paid in the meantime — is a normal, silent no-op
        rather than a race. Backed by `ix_order_hold_expires_pending`, a
        partial index on exactly this predicate."""
        stmt = (
            select(OrderRow.id)
            .where(
                OrderRow.status == OrderStatus.PENDING_PAYMENT.value,
                OrderRow.hold_expires_at.is_not(None),
                OrderRow.hold_expires_at < datetime.now(UTC),
            )
            .order_by(OrderRow.hold_expires_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def claim_expired_hold(self, order_id: str) -> dict[str, Any] | None:
        """AAD-REL-005: claims exactly one candidate row for the sweep to
        process. `FOR UPDATE SKIP LOCKED` means a row another replica (or
        another concurrent claim) already has locked is skipped instead of
        blocked on or double-processed — that's what lets replicas
        partition the sweep naturally instead of colliding on the same
        100 rows. The WHERE re-checks eligibility at claim time, not just
        at list time, so an order that got paid (or was already cancelled)
        between `list_expired_hold_ids` and this call is correctly skipped
        too. Returns None on either outcome — the caller doesn't need to
        (and can't cheaply) tell them apart."""
        stmt = (
            _loaded(select(OrderRow))
            .where(
                OrderRow.id == order_id,
                OrderRow.status == OrderStatus.PENDING_PAYMENT.value,
                OrderRow.hold_expires_at.is_not(None),
                OrderRow.hold_expires_at < datetime.now(UTC),
            )
            .with_for_update(skip_locked=True)
        )
        row = (await self.session.execute(stmt)).scalars().unique().one_or_none()
        return _to_dict(row) if row is not None else None

    async def count_active_cod_orders(self, user_id: str) -> int:
        """AAD-BIZ-002: how many of this user's COD orders are still
        unfulfilled — confirmed, packed or out for delivery, but not yet
        delivered/cancelled/refunded. This is the number the per-user
        concurrency cap checks: several of these in flight at once is
        several prepared-and-dispatched orders this user has no payment
        obligation to actually accept."""
        stmt = (
            select(func.count())
            .select_from(OrderRow)
            .join(Payment, Payment.order_id == OrderRow.id)
            .where(
                OrderRow.user_id == user_id,
                OrderRow.status.in_(_ACTIVE_COD_STATUSES),
                Payment.method == PaymentMethod.COD.value,
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def has_delivered_order(self, user_id: str) -> bool:
        """AAD-BIZ-002: whether this account has ever had an order actually
        reach the customer — the signal behind the "COD unlocks after your
        first completed order" gate (`settings.cod_requires_prior_delivery`).
        Any prior DELIVERED order counts, COD or online; the point is
        real-world follow-through, not which payment method got there."""
        stmt = (
            select(OrderRow.id)
            .where(OrderRow.user_id == user_id, OrderRow.status == OrderStatus.DELIVERED.value)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first() is not None

    async def find_amount_mismatches(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Orders `flag_amount_mismatch` flagged, still waiting on the
        sweep's refund call (AAD-PAY-005)."""
        stmt = (
            _loaded(select(OrderRow))
            .join(Payment, Payment.order_id == OrderRow.id)
            .where(Payment.status == PaymentStatus.AMOUNT_MISMATCH.value)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_dict(r) for r in rows]

    async def find_pending_refunds(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Orders whose cancel/force-refund already committed but whose
        gateway refund call hasn't happened yet (AAD-PAY-003) — picked up by
        the periodic sweeper, never by a customer-facing request."""
        stmt = (
            _loaded(select(OrderRow))
            .join(Payment, Payment.order_id == OrderRow.id)
            .where(Payment.status == PaymentStatus.REFUND_PENDING.value)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_dict(r) for r in rows]

    # ---------- webhook replay protection ----------

    async def record_webhook_once(
        self, provider: str, event_id: str, payload: dict[str, Any]
    ) -> bool:
        """True the first time this event id is seen, False on a replay."""
        result = await self.session.execute(
            insert(WebhookEvent)
            .values(provider=provider, event_id=event_id, payload=payload)
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
        )
        await self.session.flush()
        return result.rowcount == 1

    async def redact_expired_webhook_payloads(self, *, batch_size: int = 500) -> int:
        """AAD-DATA-011: clears `payload` on rows past retention; the
        `(provider, event_id)` row itself — and its replay-guard unique
        constraint — is kept forever, since it's small and it's the whole
        point of this table. Only the raw gateway payload, which carries
        payment identifiers, amounts and payer contact details, is dropped.

        The `payload != '{}'` guard means an already-redacted row is never
        rewritten on a later sweep — same idea as WHERE-guarding any other
        idempotent cleanup.

        AAD-REL-003: batched like the other two sweeps — a LIMIT-bounded id
        subquery, looped until drained — so a large backlog of past-
        retention rows can't take one long lock across the whole table.
        `ix_webhook_received` keeps each batch's subquery cheap.
        """
        cutoff = datetime.now(UTC) - WEBHOOK_PAYLOAD_RETENTION
        total = 0
        while True:
            batch = select(WebhookEvent.id).where(
                WebhookEvent.received_at < cutoff, WebhookEvent.payload != {}
            ).limit(batch_size)
            result = await self.session.execute(
                update(WebhookEvent).where(WebhookEvent.id.in_(batch)).values(payload={})
            )
            updated = result.rowcount or 0
            total += updated
            if updated < batch_size:
                return total
