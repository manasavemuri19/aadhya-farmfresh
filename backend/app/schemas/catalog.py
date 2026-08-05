"""Catalog wire models.

The important modelling decision: a *product* is what the customer browses
("Full Cream Cow Milk"), but a *variant* is what they actually buy ("1 litre").
Price and stock live on the variant, never on the product. Everything
downstream — cart lines, order lines, the stock ledger, the ledger's audit
trail — keys off the variant SKU.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from app.domain.enums import StockPolicy
from app.schemas.common import Schema


class Category(Schema):
    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=48)
    name: str = Field(max_length=64)
    sort_order: int = 0
    is_active: bool = True


class Variant(Schema):
    sku: str = Field(pattern=r"^[A-Z0-9-]+$", max_length=48)
    label: str = Field(max_length=48, description='Customer-facing size, e.g. "1 litre"')

    # Sold quantity, kept numeric so we can sort sizes and compute unit price.
    pack_value: float = Field(gt=0, description="e.g. 1.0 for 1 litre, 250 for 250 g")
    pack_unit: str = Field(pattern=r"^(ml|l|g|kg|piece)$")

    price_paise: int = Field(ge=0)
    # Set only when the item is discounted; must exceed price_paise.
    mrp_paise: int | None = Field(default=None, ge=0)

    stock_policy: StockPolicy = StockPolicy.TRACKED
    stock_qty: int = Field(default=0, ge=0)
    # Orders are blocked below this, keeping a buffer for walk-in customers.
    low_stock_threshold: int = Field(default=3, ge=0)
    max_per_order: int = Field(default=10, ge=1, le=99)
    is_active: bool = True

    @model_validator(mode="after")
    def _check_discount(self) -> Variant:
        if self.mrp_paise is not None and self.mrp_paise <= self.price_paise:
            raise ValueError("mrp_paise must be greater than price_paise")
        return self

    @property
    def discount_percent(self) -> int:
        if not self.mrp_paise:
            return 0
        return round((self.mrp_paise - self.price_paise) * 100 / self.mrp_paise)

    @property
    def in_stock(self) -> bool:
        if not self.is_active:
            return False
        if self.stock_policy is StockPolicy.MADE_TO_ORDER:
            return True
        return self.stock_qty > 0

    def sellable_qty(self) -> int:
        if self.stock_policy is StockPolicy.MADE_TO_ORDER:
            return self.max_per_order
        return min(self.stock_qty, self.max_per_order)


class Product(Schema):
    id: str
    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=80)
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=300)
    category: str = Field(description="Category slug")
    image_url: str = ""
    # Shown on the card as "20 min delivery" — a promise, tracked per product
    # because ghee and pickles are packed slower than milk.
    prep_minutes: int = Field(default=20, ge=5, le=240)
    is_active: bool = True
    sort_order: int = 0
    variants: list[Variant] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_skus(self) -> Product:
        skus = [v.sku for v in self.variants]
        if len(skus) != len(set(skus)):
            raise ValueError("Variant SKUs must be unique within a product")
        return self


class VariantView(Schema):
    """What the mobile client receives. Internal stock counts are not exposed —
    only whether the item can be bought and how many, which is all the UI needs."""

    sku: str
    label: str
    price_paise: int
    mrp_paise: int | None = None
    discount_percent: int = 0
    in_stock: bool
    max_qty: int
    low_stock: bool = False


class ProductView(Schema):
    id: str
    slug: str
    name: str
    description: str
    category: str
    image_url: str
    prep_minutes: int
    variants: list[VariantView]


class CatalogResponse(Schema):
    categories: list[Category]
    products: list[ProductView]
    generated_at: datetime


def to_variant_view(v: Variant) -> VariantView:
    tracked = v.stock_policy is StockPolicy.TRACKED
    return VariantView(
        sku=v.sku,
        label=v.label,
        price_paise=v.price_paise,
        mrp_paise=v.mrp_paise,
        discount_percent=v.discount_percent,
        in_stock=v.in_stock,
        max_qty=v.sellable_qty(),
        low_stock=tracked and 0 < v.stock_qty <= v.low_stock_threshold,
    )


def to_product_view(p: Product) -> ProductView:
    return ProductView(
        id=p.id,
        slug=p.slug,
        name=p.name,
        description=p.description,
        category=p.category,
        image_url=p.image_url,
        prep_minutes=p.prep_minutes,
        variants=[to_variant_view(v) for v in p.variants if v.is_active],
    )
