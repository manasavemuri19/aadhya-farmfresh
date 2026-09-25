from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import NotFound
from app.repositories.products import ProductRepository
from app.schemas.catalog import CatalogResponse, ProductView, to_product_view


class CatalogService:
    def __init__(self, products: ProductRepository) -> None:
        self.products = products

    async def get_catalog(self, *, category: str | None = None) -> CatalogResponse:
        categories = await self.products.list_categories()
        products = await self.products.list_products(category=category)
        return CatalogResponse(
            categories=categories,
            products=[to_product_view(p) for p in products],
            generated_at=datetime.now(UTC),
        )

    async def get_product(self, id_or_slug: str) -> ProductView:
        # AAD-PERF-011: this used to always try `get_by_id` first and fall
        # back to `get_by_slug` on a miss — two queries for the common case,
        # since a client navigating by slug (a share link, a deep link) is
        # not also a product id. Product ids are always `prd_<token>`
        # (`core/ids.py`'s `new_id("prd", ...)`, the only prefix ever used
        # for a product — confirmed against `scripts/seed.py`, the one
        # place that mints them); a slug never starts with that, so this
        # branches on the prefix instead of guessing with two round trips.
        product = (
            await self.products.get_by_id(id_or_slug)
            if id_or_slug.startswith("prd_")
            else await self.products.get_by_slug(id_or_slug)
        )
        if product is None or not product.is_active:
            raise NotFound("That product is no longer available.")
        return to_product_view(product)

    async def search(self, term: str) -> list[ProductView]:
        term = term.strip()
        if len(term) < 2:
            return []
        return [to_product_view(p) for p in await self.products.search(term)]
