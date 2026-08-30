"""Relational schema.

Notes on the modelling choices that matter:

* **Money is `BigInteger` paise.** No NUMERIC, no float. Integers are exact and
  arithmetic is unambiguous.
* **Order lines snapshot the product.** Name, variant label and unit price are
  copied onto the line at checkout. If the farm renames a product or changes a
  price next week, a six-month-old receipt still shows what the customer
  actually bought and paid. Never join a historic order back to live prices.
* **`stock_qty` carries a CHECK constraint.** Overselling is impossible at the
  database level, not merely prevented by application code. Even a buggy
  future query cannot drive stock below zero.
* **Order status transitions are enforced in code**, but every transition is
  also appended to `order_events`, giving a complete audit trail.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Enum values are stored as short strings rather than native PG enums: adding a
# new order status should be a code change and a data migration, not an
# ALTER TYPE that locks the table.
_STATUS = String(32)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Nullable now: Google sign-in is the primary login path and does not
    # supply a phone number. Phone is still collected, separately, as a
    # plain delivery-contact field — see UpdateProfile — but no longer gates
    # who can sign in, and is deliberately NOT unique: it is contact
    # information now, not an identity, and forcing uniqueness on it would
    # break registration for two unrelated households that happen to share
    # a number (a shared landline, a typo, a reused old number).
    phone: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # Google's stable per-user identifier (the JWT `sub` claim). Unique
    # whenever present; null for any account that predates Google sign-in.
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="customer", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    addresses: Mapped[list[Address]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class Address(Base, TimestampMixin):
    __tablename__ = "addresses"
    __table_args__ = (
        # One address per label per user, so saving "Home" twice updates it.
        UniqueConstraint("user_id", "label", name="uq_address_user_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(32), default="Home", nullable=False)
    line1: Mapped[str] = mapped_column(String(160), nullable=False)
    line2: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    landmark: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    city: Mapped[str] = mapped_column(String(80), default="Hyderabad", nullable=False)
    pincode: Mapped[str] = mapped_column(String(6), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    user: Mapped[User] = relationship(back_populates="addresses")


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    slug: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    products: Mapped[list[Product]] = relationship(back_populates="category_ref")


class Product(Base, TimestampMixin):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_product_category_sort", "category", "sort_order"),
        Index("ix_product_active", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    category: Mapped[str] = mapped_column(
        ForeignKey("categories.slug", ondelete="RESTRICT"), nullable=False
    )
    image_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prep_minutes: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    category_ref: Mapped[Category] = relationship(back_populates="products")
    variants: Mapped[list[Variant]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        lazy="selectin",          # avoids N+1 when listing the catalog
        order_by="Variant.sort_order",
    )


class Variant(Base, TimestampMixin):
    """A buyable SKU. Price and stock live here, never on the product."""

    __tablename__ = "variants"
    __table_args__ = (
        # The database itself refuses to hold negative stock.
        CheckConstraint("stock_qty >= 0", name="ck_variant_stock_non_negative"),
        CheckConstraint("price_paise >= 0", name="ck_variant_price_non_negative"),
        CheckConstraint(
            "mrp_paise IS NULL OR mrp_paise > price_paise",
            name="ck_variant_mrp_above_price",
        ),
        Index("ix_variant_product", "product_id"),
    )

    sku: Mapped[str] = mapped_column(String(48), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(48), nullable=False)
    pack_value: Mapped[float] = mapped_column(Float, nullable=False)
    pack_unit: Mapped[str] = mapped_column(String(8), nullable=False)

    price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mrp_paise: Mapped[int | None] = mapped_column(BigInteger)

    stock_policy: Mapped[str] = mapped_column(String(16), default="tracked", nullable=False)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_per_order: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped[Product] = relationship(back_populates="variants")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_order_user_created", "user_id", "created_at"),
        Index("ix_order_status_created", "status", "created_at"),
        CheckConstraint("total_paise >= 0", name="ck_order_total_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_number: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(_STATUS, nullable=False)

    subtotal_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_fee_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    discount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    # The delivery address is snapshotted, not referenced: the customer may
    # edit or delete their saved address, and a past delivery must still record
    # where it actually went.
    address: Mapped[dict] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str] = mapped_column(String(280), default="", nullable=False)

    eta_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stock_released: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list[OrderEvent]] = relationship(
        back_populates="order", cascade="all, delete-orphan",
        lazy="selectin", order_by="OrderEvent.at",
    )
    payment: Mapped[Payment] = relationship(
        back_populates="order", cascade="all, delete-orphan",
        uselist=False, lazy="selectin",
    )


class OrderLine(Base):
    """A snapshot of what was bought, at the price it was bought for."""

    __tablename__ = "order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "sku", name="uq_order_line_sku"),
        CheckConstraint("qty > 0", name="ck_order_line_qty_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Deliberately not a foreign key to variants: a discontinued SKU may be
    # deleted, and that must never break a historic order.
    sku: Mapped[str] = mapped_column(String(48), nullable=False)
    product_id: Mapped[str] = mapped_column(String(40), nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    variant_label: Mapped[str] = mapped_column(String(48), nullable=False)
    image_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)

    order: Mapped[Order] = relationship(back_populates="lines")


class OrderEvent(Base):
    """Append-only status history."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(_STATUS, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    note: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    by: Mapped[str] = mapped_column(String(40), default="system", nullable=False)

    order: Mapped[Order] = relationship(back_populates="events")


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payment_provider_order", "provider_order_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_order_id: Mapped[str | None] = mapped_column(String(80))
    provider_payment_id: Mapped[str | None] = mapped_column(String(80))
    # The hosted checkout URL (e.g. Razorpay's Payment Link `short_url`) is
    # only ever produced once, at order-creation time. Without persisting it
    # here, every later GET /orders/{id} — which is what the app's payment
    # screen actually polls — has no way to get it back, and "Open payment
    # page" is stuck disabled forever.
    checkout_payload: Mapped[dict | None] = mapped_column(JSONB)

    order: Mapped[Order] = relationship(back_populates="payment")


class StockLedger(Base):
    """Append-only record of every stock movement."""

    __tablename__ = "stock_ledger"
    __table_args__ = (Index("ix_ledger_sku_created", "sku", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(48), nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(40), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"
    __table_args__ = (Index("ix_otp_expires", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set on first successful match. The row is kept (not deleted) for a short
    # grace window after that — see verify_and_consume for why.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),
        Index("ix_idempotency_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="in_progress", nullable=False)
    response: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebhookEvent(Base):
    """Replay guard. The unique constraint is the whole point of this table."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),
        Index("ix_webhook_received", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
