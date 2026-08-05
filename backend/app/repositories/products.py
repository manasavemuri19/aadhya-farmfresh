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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Category as CategoryRow
from app.db.models import Product as ProductRow
from app.db.models import StockLedger, Variant as VariantRow
from app.domain.enums import StockPolicy
from app.schemas.catalog import Category, Product, Variant

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

    async def set_stock(self, sku: str, qty: int) -> bool:
        result = await self.session.execute(
            update(VariantRow).where(VariantRow.sku == sku).values(stock_qty=qty)
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

    async def upsert_product(self, product: Product) -> None:
        """Insert or update a product and its variants.

        Live stock counts are preserved on update: re-seeding a running shop
        must not wipe the morning's quantities.
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

        existing_by_sku = {v.sku: v for v in row.variants}
        for index, variant in enumerate(product.variants):
            target = existing_by_sku.pop(variant.sku, None)
            if target is None:
                target = VariantRow(sku=variant.sku, stock_qty=variant.stock_qty)
                row.variants.append(target)
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

        # Variants no longer in the catalog are deactivated rather than deleted,
        # so historic orders keep something to point at.
        for orphan in existing_by_sku.values():
            orphan.is_active = False

    async def set_variant_active(self, sku: str, active: bool) -> bool:
        result = await self.session.execute(
            update(VariantRow).where(VariantRow.sku == sku).values(is_active=active)
        )
        return result.rowcount == 1
