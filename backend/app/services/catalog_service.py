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
        product = await self.products.get_by_id(id_or_slug)
        if product is None:
            product = await self.products.get_by_slug(id_or_slug)
        if product is None or not product.is_active:
            raise NotFound("That product is no longer available.")
        return to_product_view(product)

    async def search(self, term: str) -> list[ProductView]:
        term = term.strip()
        if len(term) < 2:
            return []
        return [to_product_view(p) for p in await self.products.search(term)]
