"""Catalog and stock persistence.

The reservation is the important piece. In Postgres it becomes a single
conditional UPDATE whose WHERE clause carries the availability check:

    UPDATE variants SET stock_qty = stock_qty - :qty
     WHERE sku = :sku AND stock_qty >= :qty

Row-level locking means concurrent transactions serialise on that row, and
`rowcount` tells us who won. A CHECK constraint on the column backs it up, so
even a future query with a bug cannot push stock negative.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ValidationError
from app.db.models import CatalogAudit, StockLedger
from app.db.models import Category as CategoryRow
from app.db.models import Product as ProductRow
from app.db.models import Variant as VariantRow
from app.domain.enums import StockPolicy
from app.schemas.catalog import Category, Product, Variant

# AAD-API-006: a price change past this fraction of the currently stored
# price needs SetPriceRequest.confirm_large_change — see set_price.
_LARGE_CHANGE_FRACTION = 0.5

log = logging.getLogger(__name__)


def _to_variant(row: VariantRow) -> Variant:
    return Variant(
        sku=row.sku,
        label=row.label,
        pack_value=row.pack_value,
        pack_unit=row.pack_unit,
        price_paise=row.price_paise,
        mrp_paise=row.mrp_paise,
        stock_policy=StockPolicy(row.stock_policy),
        stock_qty=row.stock_qty,
        low_stock_threshold=row.low_stock_threshold,
        max_per_order=row.max_per_order,
        is_active=row.is_active,
    )


def _to_product(row: ProductRow) -> Product:
    return Product(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        category=row.category,
        image_url=row.image_url,
        prep_minutes=row.prep_minutes,
        is_active=row.is_active,
        sort_order=row.sort_order,
        variants=[_to_variant(v) for v in row.variants],
    )


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------- reads ----------

    async def list_categories(self, *, active_only: bool = True) -> list[Category]:
        stmt = select(CategoryRow).order_by(CategoryRow.sort_order)
        if active_only:
            stmt = stmt.where(CategoryRow.is_active.is_(True))
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            Category(
                slug=r.slug, name=r.name, sort_order=r.sort_order, is_active=r.is_active
            )
            for r in rows
        ]

    async def list_products(
        self, *, category: str | None = None, active_only: bool = True
    ) -> list[Product]:
        stmt = (
            select(ProductRow)
            .options(selectinload(ProductRow.variants))
            .order_by(ProductRow.category, ProductRow.sort_order, ProductRow.name)
        )
        if active_only:
            stmt = stmt.where(ProductRow.is_active.is_(True))
        if category and category != "all":
            stmt = stmt.where(ProductRow.category == category)
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_product(r) for r in rows]

    async def search(self, term: str, *, limit: int = 30) -> list[Product]:
        """Case-insensitive substring search across name and description.

        Adequate for a catalog of this size. If it grows past a few hundred
        products, swap this for a `tsvector` column with a GIN index — the
        query changes, the interface does not.
        """
        pattern = f"%{term.strip()}%"
        stmt = (
            select(ProductRow)
            .options(selectinload(ProductRow.variants))
            .where(
                ProductRow.is_active.is_(True),
                ProductRow.name.ilike(pattern) | ProductRow.description.ilike(pattern),
            )
            .order_by(ProductRow.sort_order)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_product(r) for r in rows]

    async def get_by_id(self, product_id: str) -> Product | None:
        stmt = (
            select(ProductRow)
            .options(selectinload(ProductRow.variants))
            .where(ProductRow.id == product_id)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_product(row) if row else None

    async def get_by_slug(self, slug: str) -> Product | None:
        stmt = (
            select(ProductRow)
            .options(selectinload(ProductRow.variants))
            .where(ProductRow.slug == slug)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_product(row) if row else None

    async def find_variants(self, skus: list[str]) -> dict[str, tuple[Product, dict]]:
        """Resolve SKUs to (product, variant) pairs in one query."""
        if not skus:
            return {}
        unique = list(dict.fromkeys(skus))
        stmt = (
            select(VariantRow)
            .options(
                selectinload(VariantRow.product).selectinload(ProductRow.variants)
            )
            .where(VariantRow.sku.in_(unique))
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return {
            row.sku: (_to_product(row.product), _to_variant(row).model_dump())
            for row in rows
        }

    # ---------- stock ----------

    async def reserve_stock(self, sku: str, qty: int) -> bool:
        """Atomically decrement stock. Returns False when unavailable.

        The `stock_qty >= qty` predicate lives in the WHERE clause, so the check
        and the write are one statement. Two concurrent buyers of the last unit
        serialise on the row lock and exactly one sees `rowcount == 1`.
        """
        result = await self.session.execute(
            update(VariantRow)
            .where(
                VariantRow.sku == sku,
                VariantRow.is_active.is_(True),
                VariantRow.stock_policy == StockPolicy.TRACKED.value,
                VariantRow.stock_qty >= qty,
            )
            .values(stock_qty=VariantRow.stock_qty - qty)
        )
        if result.rowcount == 1:
            return True

        # Made-to-order SKUs never decrement, so a zero rowcount is expected.
        made_to_order = await self.session.execute(
            select(VariantRow.sku).where(
                VariantRow.sku == sku,
                VariantRow.is_active.is_(True),
                VariantRow.stock_policy == StockPolicy.MADE_TO_ORDER.value,
            )
        )
        return made_to_order.scalars().first() is not None

    async def release_stock(self, sku: str, qty: int) -> None:
        await self.session.execute(
            update(VariantRow)
            .where(
                VariantRow.sku == sku,
                VariantRow.stock_policy == StockPolicy.TRACKED.value,
            )
            .values(stock_qty=VariantRow.stock_qty + qty)
        )

    async def set_stock(
        self, sku: str, qty: int, *, expected_qty: int
    ) -> tuple[bool, int | None]:
        """Compare-and-swap (AAD-DATA-016): the write only lands if the
        row's `stock_qty` still equals `expected_qty` — the value the
        caller last saw on screen. Without this, a staff member confirming
        a stale morning count silently overwrites whatever `reserve_stock`
        decremented in between (a real customer reservation, gone).

        Returns `(True, qty)` on success. On failure: `(False, None)` means
        no such SKU; `(False, current_qty)` means the row moved since the
        caller last read it — the caller (admin.py) turns that into a 409
        carrying the current value, rather than silently overwriting it.

        This also fixes AAD-DATA-015 as a side effect: because the swap is
        now guaranteed to only succeed when `expected_qty` was correct, the
        caller's `new_qty - expected_qty` is finally the *real* delta for
        the stock ledger, not the absolute value it used to record.
        """
        result = await self.session.execute(
            update(VariantRow)
            .where(VariantRow.sku == sku, VariantRow.stock_qty == expected_qty)
            .values(stock_qty=qty)
        )
        if result.rowcount == 1:
            return True, qty
        current = (
            await self.session.execute(
                select(VariantRow.stock_qty).where(VariantRow.sku == sku)
            )
        ).scalar_one_or_none()
        return False, current

    async def record_catalog_change(
        self,
        *,
        sku: str,
        field: str,
        old_value: object,
        new_value: object,
        actor: str = "system",
        source: str = "admin_api",
    ) -> None:
        """AAD-DATA-017: one row per mutated commercial field. Stringifies
        both values — see `CatalogAudit`'s docstring for why."""
        self.session.add(
            CatalogAudit(
                sku=sku,
                field=field,
                old_value=None if old_value is None else str(old_value),
                new_value=None if new_value is None else str(new_value),
                actor=actor,
                source=source,
            )
        )

    async def set_price(
        self,
        sku: str,
        price_paise: int,
        mrp_paise: int | None = None,
        *,
        confirm_large_change: bool = False,
        actor: str = "system",
        source: str = "admin_api",
    ) -> bool:
        """`mrp_paise` is optional: omit it to change price only, leaving
        whatever MRP is already stored untouched (send it alongside
        `price_paise` to raise or clear the MRP in the same call).

        AAD-DATA-009: raising a price past the *stored* MRP without also
        raising the MRP is caught here, not silently — the check-constraint
        violation from `ck_variant_mrp_above_price` is turned into a clear
        422 rather than reaching the caller as a raw `IntegrityError` (a
        500). Run in a SAVEPOINT, same shape as the idempotency-key claim in
        idempotency.py, so a rejected update doesn't poison the surrounding
        request transaction.

        AAD-API-006: a change past `_LARGE_CHANGE_FRACTION` of the
        currently stored price needs `confirm_large_change=True` — a stray
        extra zero should not silently make a product ₹50 → ₹5000 (or free).
        A `SELECT ... FOR UPDATE` fetches the current price/MRP first, both
        to run that check and to capture the "old value" for AAD-DATA-017's
        audit row — the lock also means a concurrent price change on the
        same SKU can't interleave with this one within the request's
        transaction.
        """
        current = (
            await self.session.execute(
                select(VariantRow.price_paise, VariantRow.mrp_paise)
                .where(VariantRow.sku == sku)
                .with_for_update()
            )
        ).first()
        if current is None:
            return False
        old_price, old_mrp = current.price_paise, current.mrp_paise

        # old_price == 0 makes the percentage undefined; any change away
        # from a free item always needs confirmation.
        if not confirm_large_change and price_paise != old_price and (
            old_price == 0 or abs(price_paise - old_price) > old_price * _LARGE_CHANGE_FRACTION
        ):
            raise ValidationError(
                f"That changes the price by more than "
                f"{_LARGE_CHANGE_FRACTION:.0%} (from {old_price} to "
                f"{price_paise} paise). Resend with "
                "confirm_large_change=true if that's intentional."
            )

        values: dict[str, Any] = {"price_paise": price_paise}
        if mrp_paise is not None:
            values["mrp_paise"] = mrp_paise
        try:
            async with self.session.begin_nested():
                result = await self.session.execute(
                    update(VariantRow).where(VariantRow.sku == sku).values(**values)
                )
        except IntegrityError as exc:
            raise ValidationError(
                "mrp_paise must be at least price_paise. Raise mrp_paise in "
                "the same request if you're pricing above the current MRP."
            ) from exc

        if result.rowcount == 1:
            if price_paise != old_price:
                await self.record_catalog_change(
                    sku=sku, field="price_paise", old_value=old_price,
                    new_value=price_paise, actor=actor, source=source,
                )
            if mrp_paise is not None and mrp_paise != old_mrp:
                await self.record_catalog_change(
                    sku=sku, field="mrp_paise", old_value=old_mrp,
                    new_value=mrp_paise, actor=actor, source=source,
                )
        return result.rowcount == 1

    async def adjust_stock(self, sku: str, delta: int) -> bool:
        result = await self.session.execute(
            update(VariantRow)
            .where(VariantRow.sku == sku, VariantRow.stock_qty >= max(0, -delta))
            .values(stock_qty=VariantRow.stock_qty + delta)
        )
        return result.rowcount == 1

    async def record_stock_movement(
        self,
        *,
        sku: str,
        delta: int,
        reason: str,
        order_id: str | None = None,
        actor: str = "system",
    ) -> None:
        self.session.add(
            StockLedger(
                sku=sku, delta=delta, reason=reason, order_id=order_id, actor=actor
            )
        )

    # ---------- writes ----------

    async def upsert_category(self, category: Category) -> None:
        existing = await self.session.get(CategoryRow, category.slug)
        if existing:
            existing.name = category.name
            existing.sort_order = category.sort_order
            existing.is_active = category.is_active
        else:
            self.session.add(CategoryRow(**category.model_dump()))

    async def upsert_product(
        self, product: Product, *, actor: str = "system", source: str = "seed"
    ) -> None:
        """Insert or update a product and its variants.

        Live stock counts are preserved on update: re-seeding a running shop
        must not wipe the morning's quantities.

        AAD-DATA-017: this is the bulk path — today only `scripts/seed.py`,
        but the natural home for a future Google Sheets sync — that used to
        rewrite `price_paise`, `mrp_paise`, `max_per_order`, `is_active` and
        `stock_policy` across the *entire* catalog with no record of any of
        it. `target` is already a loaded ORM object here, so the "old"
        values are just its current attributes, read before they're
        overwritten — no extra query needed. Only genuinely *changed*
        fields on *existing* variants get an audit row; a brand-new variant
        has no "old" value to record against.
        """
        stmt = (
            select(ProductRow)
            .options(selectinload(ProductRow.variants))
            .where(ProductRow.slug == product.slug)
        )
        row = (await self.session.execute(stmt)).scalars().first()

        if row is None:
            row = ProductRow(id=product.id, slug=product.slug)
            self.session.add(row)

        row.name = product.name
        row.description = product.description
        row.category = product.category
        row.image_url = product.image_url
        row.prep_minutes = product.prep_minutes
        row.is_active = product.is_active
        row.sort_order = product.sort_order

        audited_fields = (
            "price_paise", "mrp_paise", "stock_policy", "max_per_order", "is_active",
        )

        existing_by_sku = {v.sku: v for v in row.variants}
        for index, variant in enumerate(product.variants):
            target = existing_by_sku.pop(variant.sku, None)
            is_new = target is None
            if target is None:
                target = VariantRow(sku=variant.sku, stock_qty=variant.stock_qty)
                row.variants.append(target)

            before = None if is_new else {f: getattr(target, f) for f in audited_fields}

            target.label = variant.label
            target.pack_value = variant.pack_value
            target.pack_unit = variant.pack_unit
            target.price_paise = variant.price_paise
            target.mrp_paise = variant.mrp_paise
            target.stock_policy = variant.stock_policy.value
            target.low_stock_threshold = variant.low_stock_threshold
            target.max_per_order = variant.max_per_order
            target.is_active = variant.is_active
            target.sort_order = index

            if before is not None:
                for field in audited_fields:
                    after = getattr(target, field)
                    if after != before[field]:
                        await self.record_catalog_change(
                            sku=target.sku, field=field, old_value=before[field],
                            new_value=after, actor=actor, source=source,
                        )

        # Variants no longer in the catalog are deactivated rather than deleted,
        # so historic orders keep something to point at.
        for orphan in existing_by_sku.values():
            if orphan.is_active:
                await self.record_catalog_change(
                    sku=orphan.sku, field="is_active", old_value=True,
                    new_value=False, actor=actor, source=source,
                )
            orphan.is_active = False

    async def set_variant_active(
        self, sku: str, active: bool, *, actor: str = "system", source: str = "admin_api"
    ) -> bool:
        current = (
            await self.session.execute(
                select(VariantRow.is_active).where(VariantRow.sku == sku).with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            return False
        result = await self.session.execute(
            update(VariantRow).where(VariantRow.sku == sku).values(is_active=active)
        )
        if result.rowcount == 1 and active != current:
            await self.record_catalog_change(
                sku=sku, field="is_active", old_value=current, new_value=active,
                actor=actor, source=source,
            )
        return result.rowcount == 1
