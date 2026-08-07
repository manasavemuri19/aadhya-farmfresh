"""Seed the catalog with the real Aadya product list.

Idempotent: re-running updates existing records rather than duplicating them.
Stock is seeded at a plausible morning quantity so the app is usable straight
after setup.

    python -m scripts.seed
"""

from __future__ import annotations

import asyncio
import logging

from app.core.ids import new_id
from app.core.logging import configure_logging
from app.db.base import connect, disconnect, session_scope
from app.domain.enums import StockPolicy
from app.repositories.products import ProductRepository
from app.schemas.catalog import Category, Product, Variant

log = logging.getLogger("seed")

CATEGORIES = [
    Category(slug="milk", name="Milk", sort_order=1),
    Category(slug="curd-buttermilk", name="Curd & Buttermilk", sort_order=2),
    Category(slug="paneer-khoya", name="Paneer & Khoya", sort_order=3),
    Category(slug="ghee-butter", name="Ghee & Butter", sort_order=4),
    Category(slug="pickles", name="Pickles", sort_order=5),
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
    _p(
        "full-cream-cow-milk", "Full Cream Cow Milk",
        "Farm fresh, unhomogenised, 6% fat", "milk", 20, 1,
        [
            _v("MILK-COW-500ML", "500 ml", 500, "ml", 20, mrp=23, stock=60),
            _v("MILK-COW-1L", "1 litre", 1, "l", 35, mrp=40, stock=80),
            _v("MILK-COW-2L", "2 litre", 2, "l", 68, mrp=80, stock=30),
            _v("MILK-COW-5L", "5 litre can", 5, "l", 168, mrp=200, stock=10, max_per_order=4),
        ],
    ),
    _p(
        "buffalo-milk", "Buffalo Milk",
        "Thick & creamy, ideal for tea and sweets", "milk", 20, 2,
        [
            _v("MILK-BUF-500ML", "500 ml", 500, "ml", 24, stock=50),
            _v("MILK-BUF-1L", "1 litre", 1, "l", 42, stock=60),
            _v("MILK-BUF-2L", "2 litre", 2, "l", 82, stock=25),
        ],
    ),
    _p(
        "toned-milk", "Toned Milk", "Light everyday milk, 3% fat", "milk", 20, 3,
        [
            _v("MILK-TON-500ML", "500 ml", 500, "ml", 16, stock=70),
            _v("MILK-TON-1L", "1 litre", 1, "l", 28, stock=90),
        ],
    ),
    _p(
        "homestyle-set-curd", "Homestyle Set Curd",
        "Set in earthen pots, mildly sweet", "curd-buttermilk", 25, 1,
        [
            _v("CURD-200G", "200 g cup", 200, "g", 25, stock=45),
            _v("CURD-500G", "500 g pack", 500, "g", 55, stock=35),
            _v("CURD-1KG", "1 kg pack", 1, "kg", 100, stock=20),
        ],
    ),
    _p(
        "masala-buttermilk", "Masala Buttermilk",
        "Curry leaves, ginger & rock salt", "curd-buttermilk", 25, 2,
        [
            _v("BTRMLK-250ML", "250 ml", 250, "ml", 18, stock=60),
            _v("BTRMLK-1L", "1 litre", 1, "l", 60, stock=25),
        ],
    ),
    _p(
        "fresh-malai-paneer", "Fresh Malai Paneer",
        "Made this morning, soft & spongy", "paneer-khoya", 30, 1,
        [
            _v("PNR-200G", "200 g", 200, "g", 90, stock=30),
            _v("PNR-500G", "500 g", 500, "g", 215, stock=20),
            _v("PNR-1KG", "1 kg", 1, "kg", 420, stock=10, max_per_order=5),
        ],
    ),
    _p(
        "pure-khoya-mawa", "Pure Khoya (Mawa)",
        "Slow reduced full cream milk", "paneer-khoya", 35, 2,
        [
            # Khoya is reduced to order — never blocks on a stock count.
            _v("KHOYA-250G", "250 g", 250, "g", 140,
               policy=StockPolicy.MADE_TO_ORDER, stock=0, max_per_order=6),
            _v("KHOYA-500G", "500 g", 500, "g", 270,
               policy=StockPolicy.MADE_TO_ORDER, stock=0, max_per_order=4),
        ],
    ),
    _p(
        "bilona-cow-ghee", "Bilona Cow Ghee",
        "Hand churned, aromatic & grainy", "ghee-butter", 40, 1,
        [
            _v("GHEE-250ML", "250 ml jar", 250, "ml", 375, stock=25),
            _v("GHEE-500ML", "500 ml jar", 500, "ml", 720, stock=18),
            _v("GHEE-1L", "1 litre tin", 1, "l", 1390, stock=8, max_per_order=3),
        ],
    ),
    _p(
        "fresh-white-butter", "Fresh White Butter",
        "Unsalted makhan, churned daily", "ghee-butter", 40, 2,
        [
            _v("BUTTER-200G", "200 g", 200, "g", 160, stock=22),
            _v("BUTTER-500G", "500 g", 500, "g", 385, stock=14),
        ],
    ),
    _p(
        "avakaya-mango-pickle", "Avakaya Mango Pickle",
        "Andhra style, sun cured raw mango", "pickles", 45, 1,
        [
            _v("PKL-AVK-250G", "250 g", 250, "g", 145, stock=40),
            _v("PKL-AVK-500G", "500 g", 500, "g", 275, stock=30),
            _v("PKL-AVK-1KG", "1 kg", 1, "kg", 520, stock=15, max_per_order=5),
        ],
    ),
    _p(
        "lemon-pickle", "Lemon Pickle",
        "Tangy, oil free, no preservatives", "pickles", 45, 2,
        [
            _v("PKL-LMN-250G", "250 g", 250, "g", 125, stock=40),
            _v("PKL-LMN-500G", "500 g", 500, "g", 235, stock=28),
        ],
    ),
    _p(
        "gongura-pickle", "Gongura Pickle",
        "Sorrel leaves with garlic tempering", "pickles", 45, 3,
        [
            _v("PKL-GNG-250G", "250 g", 250, "g", 155, stock=35),
            _v("PKL-GNG-500G", "500 g", 500, "g", 295, stock=22),
        ],
    ),
    _p(
        "garlic-pickle", "Garlic Pickle",
        "Whole cloves in red chilli masala", "pickles", 45, 4,
        [
            _v("PKL-GRL-250G", "250 g", 250, "g", 135, stock=35),
            _v("PKL-GRL-500G", "500 g", 500, "g", 255, stock=24),
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
