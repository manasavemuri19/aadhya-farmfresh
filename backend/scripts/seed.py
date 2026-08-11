"""Seed the catalog with the real Aadya product list.

Idempotent: re-running updates existing records rather than duplicating them.
Stock is seeded at a plausible morning quantity so the app is usable straight
after setup.

    python -m scripts.seed
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete, select

from app.core.ids import new_id
from app.core.logging import configure_logging
from app.db.base import connect, disconnect, session_scope
from app.db.models import Product as ProductRow
from app.domain.enums import StockPolicy
from app.repositories.products import ProductRepository
from app.schemas.catalog import Category, Product, Variant

log = logging.getLogger("seed")

CATEGORIES = [
    Category(slug="milk", name="Milk", sort_order=1),
    Category(slug="curd", name="Curd", sort_order=2),
    Category(slug="paneer-cheese", name="Paneer & Cheese", sort_order=3),
    Category(slug="ghee-butter", name="Ghee & Butter", sort_order=4),
    Category(slug="sweets", name="Sweets & Traditional", sort_order=5),
    Category(slug="eggs", name="Eggs", sort_order=6),
    Category(slug="pantry", name="Pantry & Snacks", sort_order=7),
]


def _v(
    sku: str,
    label: str,
    pack_value: float,
    pack_unit: str,
    rupees: int,
    *,
    mrp: int | None = None,
    stock: int = 40,
    policy: StockPolicy = StockPolicy.TRACKED,
    max_per_order: int = 10,
) -> Variant:
    return Variant(
        sku=sku,
        label=label,
        pack_value=pack_value,
        pack_unit=pack_unit,
        price_paise=rupees * 100,
        mrp_paise=mrp * 100 if mrp else None,
        stock_policy=policy,
        stock_qty=stock,
        max_per_order=max_per_order,
    )


# The mobile app ships its own bundled product photography and resolves
# images locally by product name — see mobile/src/lib/productImages.ts. This
# field is not used by the app right now; it's kept for a future admin panel
# or web client, and left empty rather than pointing at prototype URLs that
# were never meant to be a real image host. Populate with real farm photo
# CDN URLs here once they exist — the app prefers this field over its local
# bundle automatically when it's non-empty.
_IMAGE_BY_SLUG: dict[str, str] = {}


def _p(
    slug: str,
    name: str,
    description: str,
    category: str,
    prep_minutes: int,
    sort_order: int,
    variants: list[Variant],
) -> Product:
    return Product(
        id=new_id("prd", 12),
        slug=slug,
        name=name,
        description=description,
        category=category,
        image_url=_IMAGE_BY_SLUG.get(slug, ""),
        prep_minutes=prep_minutes,
        sort_order=sort_order,
        variants=variants,
    )


PRODUCTS = [
    # ---------------- Milk ----------------
    _p(
        "buffalo-milk", "Buffalo Milk",
        "Rich and creamy, ideal for tea and sweets", "milk", 20, 1,
        [
            _v("MILK-BUF-500ML", "500 ml", 500, "ml", 32, stock=40),
            _v("MILK-BUF-1L", "1 litre", 1, "l", 60, stock=40),
        ],
    ),
    _p(
        "cow-milk", "Cow Milk",
        "Farm fresh, light and easy to digest", "milk", 20, 2,
        [
            _v("MILK-COW-500ML", "500 ml", 500, "ml", 26, stock=40),
            _v("MILK-COW-1L", "1 litre", 1, "l", 50, stock=40),
        ],
    ),

    # ---------------- Curd ----------------
    _p(
        "curd", "Curd",
        "Set the traditional way, thick and fresh", "curd", 20, 1,
        [
            _v("CURD-500G", "500 g", 500, "g", 38, stock=30),
            _v("CURD-1KG", "1 kg", 1, "kg", 70, stock=25),
        ],
    ),

    # ---------------- Paneer & Cheese ----------------
    _p(
        "paneer", "Paneer",
        "Soft malai paneer, cut fresh to order", "paneer-cheese", 25, 1,
        [
            _v("PNR-200G", "200 g", 200, "g", 85, stock=20),
            _v("PNR-400G", "400 g", 400, "g", 160, stock=15),
            _v("PNR-1KG", "1 kg", 1, "kg", 380, stock=10),
        ],
    ),
    _p(
        "cheese", "Cheese",
        "House-made block cheese", "paneer-cheese", 25, 2,
        [
            _v("CHS-200G", "200 g", 200, "g", 115, stock=15),
            _v("CHS-500G", "500 g", 500, "g", 270, stock=10),
        ],
    ),

    # ---------------- Ghee & Butter ----------------
    _p(
        "ghee", "Ghee",
        "Traditional bilona-churned cow ghee", "ghee-butter", 20, 1,
        [
            # Owner's list said "200mg" — almost certainly meant grams
            # (200mg of ghee is a couple of drops). Confirm before launch.
            _v("GHEE-200G", "200 g", 200, "g", 210, stock=25),
            _v("GHEE-500G", "500 g", 500, "g", 500, stock=20),
            _v("GHEE-1KG", "1 kg", 1, "kg", 970, stock=12),
        ],
    ),
    _p(
        "butter", "Butter",
        "Fresh white butter, unsalted", "ghee-butter", 20, 2,
        [
            _v("BTR-200G", "200 g", 200, "g", 105, stock=20),
            _v("BTR-500G", "500 g", 500, "g", 250, stock=12),
        ],
    ),
    _p(
        "honey", "Honey",
        "Raw and unprocessed", "ghee-butter", 20, 3,
        [
            _v("HNY-500G", "500 g", 500, "g", 270, stock=18),
            _v("HNY-1KG", "1 kg", 1, "kg", 500, stock=12),
        ],
    ),
    _p(
        "jam", "Jam",
        "House-made fruit jam", "ghee-butter", 20, 4,
        [
            _v("JAM-200G", "200 g", 200, "g", 90, stock=15),
            _v("JAM-500G", "500 g", 500, "g", 200, stock=10),
        ],
    ),

    # ---------------- Sweets & Traditional ----------------
    # Made-to-order: small-batch traditional sweets, not held as shelf stock.
    _p(
        "junnu", "Junnu",
        "Traditional colostrum milk pudding, in season", "sweets", 40, 1,
        [
            _v("JNU-200G", "200 g cup", 200, "g", 60, stock=0, policy=StockPolicy.MADE_TO_ORDER),
        ],
    ),
    _p(
        "doodh-peda", "Doodh Peda",
        "Milk-solid sweet, made fresh", "sweets", 40, 2,
        [
            _v("PEDA-250G", "250 g", 250, "g", 180, stock=0, policy=StockPolicy.MADE_TO_ORDER),
            _v("PEDA-500G", "500 g", 500, "g", 340, stock=0, policy=StockPolicy.MADE_TO_ORDER),
        ],
    ),
    _p(
        "mysore-pak", "Mysore Pak",
        "Ghee-rich gram flour sweet", "sweets", 40, 3,
        [
            _v("MYSPAK-250G", "250 g", 250, "g", 160, stock=0, policy=StockPolicy.MADE_TO_ORDER),
            _v("MYSPAK-500G", "500 g", 500, "g", 300, stock=0, policy=StockPolicy.MADE_TO_ORDER),
        ],
    ),

    # ---------------- Eggs ----------------
    _p(
        "eggs", "Eggs",
        "White eggs, sold by count", "eggs", 15, 1,
        [
            _v("EGG-6", "6 pcs", 6, "piece", 42, stock=30),
            _v("EGG-12", "12 pcs", 12, "piece", 80, stock=25),
            _v("EGG-30", "30 pcs (tray)", 30, "piece", 190, stock=10),
        ],
    ),
    _p(
        "brown-eggs", "Brown Eggs",
        "Brown-shell eggs, sold by count", "eggs", 15, 2,
        [
            _v("BEGG-6", "6 pcs", 6, "piece", 54, stock=30),
            _v("BEGG-12", "12 pcs", 12, "piece", 100, stock=25),
            _v("BEGG-30", "30 pcs (tray)", 30, "piece", 240, stock=10),
        ],
    ),

    # ---------------- Pantry & Snacks ----------------
    _p(
        "nilofer-chaipatha", "Nilofer Chaipatha",
        "Tea leaves", "pantry", 20, 1,
        [
            _v("TEA-250G", "250 g", 250, "g", 140, stock=20),
            _v("TEA-500G", "500 g", 500, "g", 270, stock=15),
        ],
    ),
    _p(
        "osmania-biscuits", "Osmania Biscuits",
        "Hyderabadi salt-sweet biscuits", "pantry", 20, 2,
        [
            _v("OSM-250G", "250 g", 250, "g", 90, stock=25),
            _v("OSM-500G", "500 g", 500, "g", 170, stock=18),
        ],
    ),
    _p(
        "avd-mixture", "AVD Mixture",
        "Savoury snack mixture", "pantry", 20, 3,
        [
            _v("AVD-200G", "200 g", 200, "g", 80, stock=20),
            _v("AVD-500G", "500 g", 500, "g", 180, stock=12),
        ],
    ),
    _p(
        "cool-drinks", "Cool Drinks",
        "Chilled soft drinks", "pantry", 10, 4,
        [
            _v("CLD-250ML", "250 ml", 250, "ml", 20, stock=40),
            _v("CLD-750ML", "750 ml", 750, "ml", 45, stock=25),
        ],
    ),
]

async def main() -> None:
    configure_logging("INFO")
    await connect()

    async with session_scope() as session:
        repo = ProductRepository(session)

        # Categories first: products reference them by foreign key.
        for category in CATEGORIES:
            await repo.upsert_category(category)
        await session.flush()

        # Deactivate categories no longer in use (rather than delete — the
        # Category model already has an is_active flag for exactly this, and
        # products carry a RESTRICT foreign key to their category, so this is
        # the safe option regardless of deletion order below).
        from app.db.models import Category as CategoryRow

        current_category_slugs = {c.slug for c in CATEGORIES}
        await session.execute(
            CategoryRow.__table__.update()
            .where(CategoryRow.slug.notin_(current_category_slugs))
            .values(is_active=False)
        )

        # Retire any product whose slug is no longer in PRODUCTS — this makes
        # PRODUCTS the full source of truth for the catalog. Necessary because
        # `upsert_product` only ever adds or updates by slug; it has no way to
        # know a slug was renamed or a line was dropped. Deleting the row
        # (rather than leaving it inactive forever) also frees its SKUs for
        # reuse, and cascades to its variants — safe, because order lines
        # snapshot the product at purchase time rather than referencing it.
        current_slugs = {p.slug for p in PRODUCTS}
        existing = (await session.execute(select(ProductRow.slug))).scalars().all()
        retired = [slug for slug in existing if slug not in current_slugs]
        if retired:
            await session.execute(delete(ProductRow).where(ProductRow.slug.in_(retired)))
            log.info("retired discontinued products", extra={"slugs": retired})

        for product in PRODUCTS:
            # `upsert_product` preserves live stock counts on an existing SKU,
            # so re-seeding a running shop never wipes the morning's numbers.
            await repo.upsert_product(product)

    variant_count = sum(len(p.variants) for p in PRODUCTS)
    log.info(
        "seed complete",
        extra={
            "categories": len(CATEGORIES),
            "products": len(PRODUCTS),
            "skus": variant_count,
        },
    )
    await disconnect()


if __name__ == "__main__":
    asyncio.run(main())
