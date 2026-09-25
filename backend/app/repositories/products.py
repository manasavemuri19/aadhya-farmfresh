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

from sqlalchemy import Integer, String, column, func, select, update, values
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
        query changes, the interface does not. AAD-PERF-010's leading
        wildcard (the reason this can't use a plain B-tree index) is left
        as-is for that same reason — not worth the schema change at 20
        products, and the endpoint's real exposure (unauthenticated,
        unthrottled) is a rate-limiting question, not a query one.

        AAD-PERF-010: `%` and `_` in the caller's own term used to go
        straight into the pattern unescaped — ILIKE treats both as
        wildcards, so `q="%"` matched every active product and `q="_"`
        matched any single character, not the literal characters typed.
        Escaped here (backslash as the escape character, itself escaped
        first so a literal backslash in the term doesn't turn into an
        unintended escape) so a search for the character "%" behaves like
        the plain substring search this looks like, not a second special
        syntax nobody asked for.
        """
        term = term.strip()
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = (
            select(ProductRow)
            .options(selectinload(ProductRow.variants))
            .where(
                ProductRow.is_active.is_(True),
                ProductRow.name.ilike(pattern, escape="\\")
                | ProductRow.description.ilike(pattern, escape="\\"),
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
        """Resolve SKUs to (product, variant) pairs in one query.

        AAD-PERF-011: `_to_product` used to run once per requested *SKU*,
        rebuilding the same `Product` — including every sibling variant —
        from scratch each time, so a 10-line cart from 3 products built 10
        Product objects for 3 distinct ones. Built once per distinct
        product id instead, here, and the same object is shared across
        every SKU that belongs to it — `Product` is an immutable schema
        instance, so sharing one across multiple dict values is safe.
        """
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
        products_by_id: dict[str, Product] = {}
        result: dict[str, tuple[Product, dict]] = {}
        for row in rows:
            product = products_by_id.get(row.product.id)
            if product is None:
                product = _to_product(row.product)
                products_by_id[row.product.id] = product
            result[row.sku] = (product, _to_variant(row).model_dump())
        return result

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

    async def reserve_stock_bulk(self, items: list[tuple[str, int]]) -> dict[str, bool]:
        """The same atomic reserve as `reserve_stock`, for every line of an
        order in one round trip instead of one-line-at-a-time.

        AAD-PERF-008: `_create_order_inner` used to call `reserve_stock`
        once per line in a serial `await` loop — a 10-line order cost up to
        20 round trips (an UPDATE, and for any made-to-order line a second
        SELECT), each one holding row locks on every SKU reserved so far
        for that much longer. This does the same compare-and-swap for every
        requested SKU as a single `UPDATE ... FROM (VALUES ...)` statement
        (Postgres still takes its usual per-row lock on each matched row —
        concurrent safety on any one SKU is unchanged, `reserve_stock`
        itself is untouched and still what `test_concurrency_real.py`
        exercises directly), then exactly one more SELECT to tell "made-to-
        order, nothing to reserve" apart from "genuinely out of stock"
        among whatever didn't decrement — two round trips total, regardless
        of how many lines the order has, matching the two-round-trip shape
        `reserve_stock` itself already has for a single line.

        Returns `{sku: ok}` for every sku in `items`; a duplicate sku in
        `items` is deduplicated by only the first (qty, sku) instance
        seen, since a cart only ever has one line per sku.
        """
        if not items:
            return {}
        unique_items = list({sku: (sku, qty) for sku, qty in items}.values())
        skus = [sku for sku, _ in unique_items]

        data = values(
            column("sku", String), column("qty", Integer), name="reservation_data"
        ).data(unique_items)

        stmt = (
            update(VariantRow)
            .where(
                VariantRow.sku == data.c.sku,
                VariantRow.is_active.is_(True),
                VariantRow.stock_policy == StockPolicy.TRACKED.value,
                VariantRow.stock_qty >= data.c.qty,
            )
            .values(stock_qty=VariantRow.stock_qty - data.c.qty)
            .returning(VariantRow.sku)
        )
        reserved = set((await self.session.execute(stmt)).scalars().all())

        result = {sku: (sku in reserved) for sku in skus}
        remaining = [sku for sku in skus if sku not in reserved]
        if remaining:
            made_to_order = await self.session.execute(
                select(VariantRow.sku).where(
                    VariantRow.sku.in_(remaining),
                    VariantRow.is_active.is_(True),
                    VariantRow.stock_policy == StockPolicy.MADE_TO_ORDER.value,
                )
            )
            for sku in made_to_order.scalars().all():
                result[sku] = True

        return result

    async def release_stock(self, sku: str, qty: int) -> bool:
        """Credit stock back. Returns whether a row actually moved — False
        for a made-to-order SKU (the `stock_policy == TRACKED` filter below
        excludes it, same as `reserve_stock` never decrementing it in the
        first place) rather than silently updating zero rows. AAD-DATA-004:
        the caller uses this to decide what the stock ledger should say
        really happened, instead of always recording `delta=+qty` whether
        or not any row was actually credited.
        """
        result = await self.session.execute(
            update(VariantRow)
            .where(
                VariantRow.sku == sku,
                VariantRow.stock_policy == StockPolicy.TRACKED.value,
            )
            .values(stock_qty=VariantRow.stock_qty + qty)
        )
        return result.rowcount == 1

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

    async def adjust_stock(self, sku: str, delta: int) -> tuple[bool, str | None]:
        """Apply a stock correction (AAD-DATA-015's `:delta` path — "two got
        broken", not the morning `set_qty` recount).

        Returns `(True, None)` on success. On failure the second element
        names why, mirroring `set_stock`'s `(bool, int | None)` shape:
        `"no_such_sku"`, `"not_tracked"`, or `"insufficient_stock"`.

        AAD-API-005: this used to return a plain `bool`, so `False` meant
        either "no such SKU" or "that would take stock below zero" —
        indistinguishable to the caller. `admin.py` reported every `False`
        as the negative-stock message, so a staff member who mistyped a SKU
        was told *"That adjustment would take stock below zero"* for a
        product that doesn't exist, which sends them looking for a typo in
        the wrong place.

        AAD-QUAL-030: also now filters to `stock_policy == TRACKED`, like
        `reserve_stock`/`release_stock` already do. `sellable_qty()`
        ignores `stock_qty` entirely for a MADE_TO_ORDER variant, so an
        adjustment against one was previously silent, uncaught noise: the
        write "succeeded", the ledger recorded a movement
        (`find_stock_discrepancies` never checks it — scoped to TRACKED
        only, by its own docstring), and the number it moved had no effect
        on anything a customer could ever see. Now reported explicitly as
        `"not_tracked"` instead of quietly accepted.
        """
        result = await self.session.execute(
            update(VariantRow)
            .where(
                VariantRow.sku == sku,
                VariantRow.stock_policy == StockPolicy.TRACKED.value,
                VariantRow.stock_qty >= max(0, -delta),
            )
            .values(stock_qty=VariantRow.stock_qty + delta)
        )
        if result.rowcount == 1:
            return True, None

        row = (
            await self.session.execute(
                select(VariantRow.stock_policy, VariantRow.stock_qty).where(
                    VariantRow.sku == sku
                )
            )
        ).one_or_none()
        if row is None:
            return False, "no_such_sku"
        if row.stock_policy != StockPolicy.TRACKED.value:
            return False, "not_tracked"
        return False, "insufficient_stock"

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

    async def find_stock_discrepancies(self) -> list[dict[str, Any]]:
        """AAD-DATA-004: the reconciliation query the ledger exists for —
        `SUM(delta) GROUP BY sku`, compared against the live `stock_qty`
        for every TRACKED variant. Scoped to TRACKED only: MADE_TO_ORDER
        variants have no shelf count to reconcile, and now (with the
        made-to-order and opening-balance fixes above) contribute nothing
        but zero-delta entries anyway.

        Not wired to a schedule from here — this repository has no
        scheduler to attach one to (see app/main.py for where the other
        periodic sweeps live) — but it is real and callable today: from a
        one-off script, a staff endpoint, or the sweeper once someone wires
        it in. A variant that already had nonzero stock before this session
        added the opening-balance entry above will show up here with a
        discrepancy equal to that old, un-ledgered starting quantity —
        that's a real gap in the historical data, not a bug in this query,
        and it needs a one-time backfill from the farm's own records, not a
        number invented by this codebase.
        """
        stmt = (
            select(
                VariantRow.sku,
                VariantRow.stock_qty,
                func.coalesce(func.sum(StockLedger.delta), 0).label("ledger_sum"),
            )
            .outerjoin(StockLedger, StockLedger.sku == VariantRow.sku)
            .where(VariantRow.stock_policy == StockPolicy.TRACKED.value)
            .group_by(VariantRow.sku, VariantRow.stock_qty)
            .having(VariantRow.stock_qty != func.coalesce(func.sum(StockLedger.delta), 0))
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "sku": row.sku,
                "stock_qty": row.stock_qty,
                "ledger_sum": row.ledger_sum,
                "discrepancy": row.stock_qty - row.ledger_sum,
            }
            for row in rows
        ]

    # ---------- writes ----------

    async def upsert_category(self, category: Category) -> None:
        existing = await self.session.get(CategoryRow, category.slug)
        if existing:
            existing.name = category.name
            existing.sort_order = category.sort_order
            existing.is_active = category.is_active
        else:
            # AAD-QUAL-029 / AAD-SEC-021: the update branch above already
            # assigns each field by name — the insert branch was the one
            # instance left splatting a schema dump straight into the ORM
            # constructor. `upsert_category` is only ever called from
            # `scripts/seed.py` today, never a live route, so `category`
            # isn't attacker-controlled here the way `users.py`'s address
            # upsert is — but the fix is the same for the same reason: one
            # schema change away from `CategoryRow` picking up a field this
            # constructor call would then set without anyone deciding to.
            self.session.add(
                CategoryRow(
                    slug=category.slug,
                    name=category.name,
                    sort_order=category.sort_order,
                    is_active=category.is_active,
                )
            )

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

        AAD-DATA-018: `slug` is the only natural key this method has to
        match an existing product by — `product.id` isn't a stable
        identifier the caller intentionally sets; `scripts/seed.py` (this
        method's only caller today) generates a fresh random one on every
        single run, for every product, whether or not it already exists.
        This method only ever uses that fresh id when no existing row
        matched by slug — for an existing product it's silently discarded,
        the stored row keeps its original id, which is correct. The real
        gap: if a product's `slug` is ever *edited* in the source list (a
        typo fix, a rename) while keeping "the same" conceptual product,
        this method has no way to recognise that — the old-slug row is
        never found, a brand-new row is inserted under the new slug with a
        new id, and the old row is silently orphaned rather than updated or
        removed. There is no reliable way to detect a rename automatically
        without an explicit old-slug -> new-slug mapping this codebase
        doesn't have, so this isn't fixed here — but every genuinely new
        insert is now logged, so an accidental slug edit at least becomes
        visible in the seed run's output instead of a silent, permanent
        orphan. A deliberate rename must still be done by hand today (e.g.
        `UPDATE products SET slug = ... WHERE id = ...` before re-seeding).
        """
        stmt = (
            select(ProductRow)
            .options(selectinload(ProductRow.variants))
            .where(ProductRow.slug == product.slug)
        )
        row = (await self.session.execute(stmt)).scalars().first()

        if row is None:
            log.warning(
                "upsert_product: no existing row for slug %r — inserting a "
                "new product. If this slug used to be something else, the "
                "old row is now an orphan (AAD-DATA-018) and needs a "
                "manual fix, not another seed run.",
                product.slug,
            )
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
                # AAD-DATA-004: a brand-new TRACKED variant's starting
                # stock_qty used to enter the system with no ledger entry at
                # all — every reconciliation of this SKU from that day
                # forward would be short by exactly this many units,
                # forever, through no fault of anything that happened
                # later. One opening entry here is what lets
                # `SUM(delta) GROUP BY sku` actually equal `stock_qty` from
                # the moment a variant exists, not just from whenever
                # someone happened to first run a stock count through
                # `set_stock`. Zero-quantity and made-to-order variants get
                # nothing to reconcile in the first place, so there's
                # nothing worth recording for them.
                if variant.stock_policy == StockPolicy.TRACKED and variant.stock_qty:
                    await self.record_stock_movement(
                        sku=variant.sku, delta=variant.stock_qty,
                        reason="opening_balance", actor=actor,
                    )

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

    # AAD-BIZ-003: low-stock owner alert. `low_stock_notified` is what keeps
    # the housekeeping sweep (every 2 minutes) from re-pushing the same SKU
    # to staff/admin on every pass while it sits at, say, 1 unit — see the
    # column's own comment in db/models.py.
    async def find_newly_low_stock(self) -> list[dict[str, Any]]:
        """SKUs that are in the low-stock band right now and haven't already
        been flagged — `is_active` only, since a disabled/delisted variant
        isn't something an owner needs woken up for."""
        stmt = select(VariantRow.sku, VariantRow.label, VariantRow.stock_qty).where(
            VariantRow.stock_policy == StockPolicy.TRACKED.value,
            VariantRow.is_active.is_(True),
            VariantRow.low_stock_notified.is_(False),
            VariantRow.stock_qty > 0,
            VariantRow.stock_qty <= VariantRow.low_stock_threshold,
        )
        rows = (await self.session.execute(stmt)).all()
        return [{"sku": r.sku, "label": r.label, "stock_qty": r.stock_qty} for r in rows]

    async def mark_low_stock_notified(self, skus: list[str]) -> None:
        if not skus:
            return
        await self.session.execute(
            update(VariantRow)
            .where(VariantRow.sku.in_(skus))
            .values(low_stock_notified=True)
        )

    async def clear_stale_low_stock_flags(self) -> int:
        """Re-arms the flag once a SKU's stock has moved back out of the
        low-stock band — restocked above the threshold, or sold through to
        zero (already visible in-app as sold out; nothing more to alert on
        until it's restocked and dips low again). Run every sweep, same as
        find_newly_low_stock, so a restock is picked up within one cycle."""
        result = await self.session.execute(
            update(VariantRow)
            .where(
                VariantRow.low_stock_notified.is_(True),
                (VariantRow.stock_qty == 0)
                | (VariantRow.stock_qty > VariantRow.low_stock_threshold),
            )
            .values(low_stock_notified=False)
        )
        return result.rowcount
