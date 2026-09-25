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
* **Primary-key strategy: aggregate roots get prefixed opaque strings, child
  rows get `SERIAL` integers.** `users`, `orders`, `products` and `payments`
  are `core/ids.py`'s prefixed ids (`usr_...`, `ord_...`) — rows a client
  ever sees a standalone id for, in a URL or a request body, get the opaque
  treatment that module's docstring describes. `addresses`, `order_lines`,
  `order_events`, `stock_ledger`, `webhook_events`, `idempotency_keys` and
  `otp_challenges` are plain `SERIAL` integers — rows that only ever exist
  scoped under their parent (an address is always read and written through
  `/auth/me/addresses`, keyed by `user_id` + `label`, never by its own id;
  same shape for every other row in this list). `addresses.id` is the one
  exception worth naming explicitly: nothing in the schemas or routes
  exposes it today (`schemas/auth.Address` has no `id` field at all,
  confirmed by reading it), so the SERIAL choice is fine as things stand —
  but it's also the row here closest to becoming independently addressable
  (an edit-this-specific-address flow, say), which is exactly when a
  sequential integer id would start mattering. AAD-QUAL-026 flagged this
  split as undocumented, contradicting `core/ids.py`'s own stated doctrine;
  it isn't a contradiction, just a rule that was never written down — this
  paragraph is that rule.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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

    __table_args__ = (
        # AAD-DATA-010: the app treats `role` as a closed set (Role enum) —
        # the database now refuses anything outside it too, so a typo from a
        # manual psql fix or a future write path can never silently grant
        # (or fail to recognise) a privileged role.
        CheckConstraint(
            "role IN ('customer', 'staff', 'admin', 'delivery_agent')",
            name="ck_user_role_valid",
        ),
    )

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
    google_sub: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="customer", nullable=False)
    # AAD-SEC-002: checked on refresh and by the privileged-role dependencies
    # (require_staff/require_admin/require_delivery_agent) — a suspended or
    # deleted account is refused even though its still-valid access token
    # would otherwise pass current_user unchanged for up to 30 minutes.
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Only meaningful for role == "delivery_agent": where the delivery app
    # last reported the device's GPS position, used to match nearby order
    # requests. Null until the agent's Requests tab has been opened at
    # least once and location permission granted.
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lng: Mapped[float | None] = mapped_column(Float)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AAD-SEC-028: set when a reported position implies a physically
    # implausible speed from the previous reading (see
    # UserRepository.update_agent_location). Flagged rather than rejected —
    # the update is still stored — since the app has no retry UX for a
    # refused location report today, and GPS noise after an idle period can
    # look identical to a spoofed jump.
    last_location_flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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
        # AAD-DATA-009: was strict `>`, which made selling at list price (no
        # active discount) a constraint violation. `>=` still rejects the
        # nonsense case — an MRP below the selling price — while permitting
        # a variant to simply not be discounted right now.
        CheckConstraint(
            "mrp_paise IS NULL OR mrp_paise >= price_paise",
            name="ck_variant_mrp_above_price",
        ),
        # AAD-DATA-010: `stock_policy` is a closed set (StockPolicy enum).
        CheckConstraint(
            "stock_policy IN ('tracked', 'made_to_order')",
            name="ck_variant_stock_policy_valid",
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
    # AAD-BIZ-003: whether the low-stock owner push has already gone out for
    # this SKU's current dip — see _notify_low_stock in order_service.py.
    # Cleared once stock next leaves the low-stock band, so it re-arms.
    low_stock_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_per_order: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped[Product] = relationship(back_populates="variants")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_order_user_created", "user_id", "created_at"),
        Index("ix_order_status_created", "status", "created_at"),
        Index("ix_order_delivery_agent", "delivery_agent_id"),
        # AAD-REL-005: the hold sweeper's own query — "still pending_payment,
        # and hold_expires_at in the past" — had no index at all;
        # ix_order_status_created doesn't help since it isn't on
        # hold_expires_at. Partial, not composite: only pending_payment rows
        # ever have a meaningful hold_expires_at, so indexing every other
        # status's (always-irrelevant) value would just be dead weight.
        Index(
            "ix_order_hold_expires_pending",
            "hold_expires_at",
            postgresql_where=text("status = 'pending_payment'"),
        ),
        CheckConstraint("total_paise >= 0", name="ck_order_total_non_negative"),
        # AAD-DATA-010: `status` is a closed set (OrderStatus enum) — see the
        # same constraint on `order_events.status` below, which uses the
        # identical value list since every status here is also ever written
        # there as part of the audit trail.
        CheckConstraint(
            "status IN ('pending_payment', 'confirmed', 'packed', "
            "'out_for_delivery', 'delivered', 'cancelled', 'refunded')",
            name="ck_order_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_number: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(_STATUS, nullable=False)

    subtotal_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_fee_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # AAD-QUAL-015: `discount_paise` used to live here, always `0` — nothing
    # in this codebase has ever computed a non-zero discount. Carrying a
    # structurally-dead column (and the schema/API field, and the pricing
    # dataclass field it fed) through every order read and write was pure
    # noise for a value that could never differ from its own default.
    # Dropped in migration 0018; if promotions are ever built, a discount
    # column can come back alongside the actual promotion logic that
    # computes it, rather than sitting here unused waiting for one.
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

    # Who is delivering it, set the moment a delivery agent accepts the
    # request — independent of `status`, which staff still drive through
    # packed/out_for_delivery/delivered on their own schedule. NULL means
    # "not yet accepted by anyone."
    delivery_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    delivery_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # AAD-SEC-027: in-app proof-of-delivery. Generated (via extra_set on the
    # CAS transition) the moment an order becomes out_for_delivery, cleared
    # the same way once it becomes delivered — see OrderService.update_status
    # and OrderRepository.transition. See the migration's own docstring for
    # why both a hash and a short-lived plaintext copy are kept.
    delivery_otp_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_otp_plain: Mapped[str | None] = mapped_column(String(8), nullable=True)
    delivery_otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_otp_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

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


class OrderNumberCounter(Base):
    """Backs the daily-scoped human order number (AAD-DATA-001).

    One row per calendar date, incremented atomically by
    `OrderRepository.next_order_number` via `INSERT ... ON CONFLICT DO UPDATE
    ... RETURNING`. That makes `AD-YYMMDD-NNNN` collision-free by
    construction — a real change from the six-random-digits scheme this
    replaces, which had a 50% chance of a collision after ~1,180 orders.
    """

    __tablename__ = "order_number_counters"

    order_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class OrderEvent(Base):
    """Append-only status history."""

    __tablename__ = "order_events"
    __table_args__ = (
        # AAD-DATA-010: same closed set and reasoning as `orders.status`.
        CheckConstraint(
            "status IN ('pending_payment', 'confirmed', 'packed', "
            "'out_for_delivery', 'delivered', 'cancelled', 'refunded')",
            name="ck_order_event_status_valid",
        ),
    )

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
        # AAD-DATA-007: was a plain, non-unique index — `get_by_provider_order_id`
        # uses `.first()`, so two rows that happened to share a provider order
        # id would silently resolve to an arbitrary one instead of either
        # being impossible or raising loudly. Postgres allows any number of
        # NULLs under a UNIQUE index (COD payments never get a provider
        # order id at all), so this only actually constrains the online
        # payments that have one.
        Index("ix_payment_provider_order", "provider_order_id", unique=True),
        # AAD-DATA-010: `method` and `status` are both closed sets
        # (PaymentMethod and PaymentStatus enums).
        CheckConstraint("method IN ('online', 'cod')", name="ck_payment_method_valid"),
        CheckConstraint(
            "status IN ('created', 'authorized', 'captured', 'failed', "
            "'refunded', 'refund_pending', 'amount_mismatch')",
            name="ck_payment_status_valid",
        ),
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
    # here, every later GET /orders/{id} has no way to get it back, and
    # "Open payment page" is stuck disabled forever.
    checkout_payload: Mapped[dict | None] = mapped_column(JSONB)
    # AAD-PAY-005: set only while status == AMOUNT_MISMATCH — the amount the
    # gateway actually reported captured, which is what gets refunded (not
    # `amount_paise` above, which is what we expected and is what a normal
    # refund would use).
    received_amount_paise: Mapped[int | None] = mapped_column(BigInteger)

    order: Mapped[Order] = relationship(back_populates="payment")


class StockLedger(Base):
    """Append-only record of every stock movement."""

    __tablename__ = "stock_ledger"
    __table_args__ = (Index("ix_ledger_sku_created", "sku", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(48), nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    # AAD-DATA-014: was indexed but had no FK — a typo'd or stale order_id
    # would sit here forever with nothing to notice. `order_id` is nullable
    # for adjustments not tied to any order (a manual stock correction,
    # `reason="admin_adjustment"`), so SET NULL rather than CASCADE: nothing
    # in this codebase ever hard-deletes an `orders` row today (checked —
    # every other FK to `orders` is CASCADE precisely because their rows
    # are meaningless without it; the ledger's own audit value is not),
    # but if that ever changes, the ledger entry should survive as an
    # orphaned-but-present audit record rather than disappear with the order
    # it once referenced.
    order_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    actor: Mapped[str] = mapped_column(String(40), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogAudit(Base):
    """AAD-DATA-017: one row per mutated commercial field. `stock_ledger`
    above records quantity changes; this records everything else a staff
    account or the bulk `upsert_product` path can change on a variant —
    price, MRP, availability, stock policy, per-order cap — none of which
    left any record at all before this. Old/new values are stored as text
    rather than typed columns: the fields being audited are a mix of int,
    bool and str, and this table's only job is "what changed, from what, to
    what, by whom, when" — not to be queried structurally beyond that.
    """

    __tablename__ = "catalog_audit"
    __table_args__ = (Index("ix_catalog_audit_sku_created", "sku", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(48), nullable=False)
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(40), default="system", nullable=False)
    # admin_api (a staff/admin action through the app), seed (scripts/seed.py),
    # sheets_sync (not built yet, but named here per the fix's own suggestion
    # so a future sync doesn't need a schema change to record its source).
    source: Mapped[str] = mapped_column(String(16), default="admin_api", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RefreshToken(Base):
    """Server-side record of every refresh token issued, so one can actually
    be revoked (AAD-SEC-002) — a bare JWT, however short-lived, cannot be.

    Rows are never deleted, only marked `revoked_at` — the history is what
    makes reuse detection possible. `token_hash` is a SHA-256 of the raw
    token, not the token itself: this table being read (a DB dump, a stray
    log) must never be enough on its own to replay a session.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user", "user_id"),
        Index("ix_refresh_tokens_expires", "expires_at"),
    )

    jti: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set the moment this token is used for anything: logout, a normal
    # rotation, or a reuse-detected family revocation. NULL means "still a
    # live, usable session."
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only when revoked *by rotation* (a normal refresh). Populated means
    # "this exact token was legitimately exchanged once" — seeing it
    # presented again, after that, is the reuse signal that revokes the
    # whole family. Revoked by logout instead, this stays NULL: an
    # intentional sign-out is not theft and must not look like one.
    replaced_by: Mapped[str | None] = mapped_column(String(40))
    device_label: Mapped[str | None] = mapped_column(String(80))
    last_used_ip: Mapped[str | None] = mapped_column(String(64))


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


class PushToken(Base, TimestampMixin):
    """One row per registered device (Expo push token), used to send order
    and delivery-request notifications.

    Keyed by the token itself, not an autoincrement id: Expo push tokens are
    already globally unique strings, and re-registering the same token (a
    reinstall, or the OS handing the same cached token back) should just
    repoint it at whichever user is signed in now, not accumulate stale
    duplicate rows — see PushTokenRepository.register.
    """

    __tablename__ = "push_tokens"
    __table_args__ = (Index("ix_push_token_user", "user_id"),)

    token: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(16), default="android", nullable=False)


class SupportTicket(Base, TimestampMixin):
    """The "still stuck?" fallback at the end of the Help & Support decision
    tree, for when none of the canned answers actually resolved things.

    Deliberately minimal — this is a mailbox, not a full support-ticketing
    system: no assignment, no reply thread. AAD-BIZ-005 added `status` and
    `GET /admin/support/tickets` (routes/admin.py) so this mailbox is at
    least readable and markable-done; a real workflow (assignment, replies)
    is still a disclosed gap, not an oversight.
    """

    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_ticket_user", "user_id"),
        Index("ix_support_ticket_status_created", "status", "created_at"),
        CheckConstraint(
            "status IN ('open', 'closed')", name="ck_support_ticket_status_valid"
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # AAD-SEC-034: this had no ForeignKey to `users` — a ticket could
    # reference a user id that never existed, or one already deleted, and
    # nothing would notice; the id also couldn't be reliably joined against
    # `users` for an admin view. CASCADE matches every other user-owned row
    # in this schema (addresses, push_tokens): if the account goes, its
    # mailbox entries go with it rather than becoming orphaned rows pointing
    # at nobody.
    user_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Which node of the decision tree they were on when they gave up on the
    # canned answers and wrote in — free-form, purely to help whoever reads
    # this ticket understand the context without re-asking. Null if ever
    # submitted some other way in the future.
    context_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # AAD-BIZ-005: open until a staff account closes it via
    # POST /admin/support/tickets/{id}/close. A plain string, not the
    # SupportTicketStatus enum, for the same reason every other status
    # column here is — see AAD-DATA-010's CHECK-constraint migration.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")


class CashSettlement(Base, TimestampMixin):
    """AAD-BIZ-004: one row per cash-settlement attempt for one delivery
    agent — created before its `CodCollection` rows are claimed into it
    (see `CashRepository.settle_agent`'s own ordering note) so those rows'
    `settlement_id` foreign key always points at a real, already-committed
    parent.

    `status` is never inferred by a reader — it's written once, at
    creation, by comparing `actual_amount_paise` (what an admin recorded as
    physically received) against `expected_amount_paise` (the sum of the
    collections this settlement claims). Exact match: `settled`. Any
    difference: `discrepancy`, with `reason` required — see
    `CashService.settle_agent`. A settlement is never edited after
    creation; a corrected amount is a new settlement against whatever the
    agent still has pending.
    """

    __tablename__ = "cash_settlements"
    __table_args__ = (
        Index("ix_cash_settlement_agent_created", "agent_id", "created_at"),
        CheckConstraint("expected_amount_paise >= 0", name="ck_settlement_expected_non_negative"),
        CheckConstraint("actual_amount_paise >= 0", name="ck_settlement_actual_non_negative"),
        CheckConstraint(
            "status IN ('settled', 'discrepancy')", name="ck_settlement_status_valid"
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expected_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # actual - expected. Signed: negative means short, positive means over.
    discrepancy_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    recorded_by: Mapped[str] = mapped_column(
        String(40), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    collections: Mapped[list[CodCollection]] = relationship(back_populates="settlement")


class CodCollection(Base):
    """AAD-BIZ-004: written exactly once per COD order, the moment
    `OrderService.verify_delivery_code` succeeds for a COD order — never at
    order creation, never by the agent self-reporting an amount. `amount_paise`
    is the order's own recorded total, not anything the agent enters, so
    there is nothing for an agent to under-report.

    `order_id` is UNIQUE: on top of the delivery OTP already being
    single-use (AAD-SEC-027), this is a second, structural guard against
    ever recording the same order's cash twice. `settlement_id` starts
    NULL ("pending, not yet handed back to the farm") and is set exactly
    once, by `CashRepository.claim_for_settlement`, when an admin settles
    that agent's cash — see `CashSettlement`'s own docstring for why a
    claimed collection is never reopened.
    """

    __tablename__ = "cod_collections"
    __table_args__ = (
        Index("ix_cod_collection_agent_pending", "agent_id", "settlement_id"),
        CheckConstraint("amount_paise > 0", name="ck_cod_collection_amount_positive"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    settlement_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("cash_settlements.id", ondelete="SET NULL"), nullable=True
    )

    settlement: Mapped[CashSettlement | None] = relationship(back_populates="collections")
