"""Order persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Order as OrderRow
from app.db.models import OrderEvent, OrderLine, Payment, WebhookEvent
from app.domain.enums import OrderStatus, PaymentStatus


def _to_dict(row: OrderRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "order_number": row.order_number,
        "user_id": row.user_id,
        "status": row.status,
        "subtotal_paise": row.subtotal_paise,
        "delivery_fee_paise": row.delivery_fee_paise,
        "discount_paise": row.discount_paise,
        "total_paise": row.total_paise,
        "currency": row.currency,
        "address": row.address,
        "notes": row.notes,
        "eta_minutes": row.eta_minutes,
        "stock_released": row.stock_released,
        "hold_expires_at": row.hold_expires_at,
        "cancel_reason": row.cancel_reason,
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
        }
        if row.payment
        else None,
    }


def _loaded(stmt):
    """Eager-load an order's children and refresh anything already in memory.

    `populate_existing` matters more than it looks: status changes are applied
    with bulk UPDATE statements, which do not sync SQLAlchemy's identity map.
    Without this, a read following a transition in the same session would
    return the stale, pre-update object — the order would look unchanged and
    its new event would be missing.
    """
    return stmt.options(
        selectinload(OrderRow.lines),
        selectinload(OrderRow.events),
        selectinload(OrderRow.payment),
    ).execution_options(populate_existing=True)


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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
            discount_paise=order["discount_paise"],
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
        stmt = (
            _loaded(select(OrderRow))
            .where(OrderRow.user_id == user_id)
            .order_by(OrderRow.created_at.desc())
            .limit(limit)
        )
        if before:
            stmt = stmt.where(OrderRow.created_at < before)
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_dict(r) for r in rows]

    async def list_by_status(
        self, statuses: list[OrderStatus], *, limit: int = 50
    ) -> list[dict[str, Any]]:
        stmt = (
            _loaded(select(OrderRow))
            .where(OrderRow.status.in_([s.value for s in statuses]))
            .order_by(OrderRow.created_at)          # a work queue: oldest first
            .limit(limit)
        )
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
    ) -> dict[str, Any] | None:
        """Compare-and-swap on status.

        `expected_status` sits in the WHERE clause, so a concurrent transition
        loses and gets None back rather than silently overwriting. This is what
        stops a payment webhook and a staff tap from clobbering each other.
        """
        values: dict[str, Any] = {"status": new_status.value, **(extra_set or {})}
        result = await self.session.execute(
            update(OrderRow)
            .where(OrderRow.id == order_id, OrderRow.status == expected_status.value)
            .values(**values)
        )
        if result.rowcount != 1:
            return None

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
        return await self.get(order_id)

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

    async def find_expired_holds(self, *, limit: int = 100) -> list[dict[str, Any]]:
        stmt = (
            _loaded(select(OrderRow))
            .where(
                OrderRow.status == OrderStatus.PENDING_PAYMENT.value,
                OrderRow.hold_expires_at.is_not(None),
                OrderRow.hold_expires_at < datetime.now(UTC),
            )
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
