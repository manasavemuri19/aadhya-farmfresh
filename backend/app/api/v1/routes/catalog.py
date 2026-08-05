from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import get_catalog_service
from app.schemas.catalog import CatalogResponse, ProductView
from app.services.catalog_service import CatalogService

router = APIRouter(prefix="/catalog", tags=["catalog"])

Catalog = Annotated[CatalogService, Depends(get_catalog_service)]


@router.get("", response_model=CatalogResponse)
async def get_catalog(
    svc: Catalog,
    response: Response,
    category: Annotated[str | None, Query(max_length=48)] = None,
) -> CatalogResponse:
    # Availability changes minute to minute, so this is deliberately not cached
    # for long. A short window still absorbs the burst when the store opens.
    response.headers["Cache-Control"] = "public, max-age=30"
    return await svc.get_catalog(category=category)


@router.get("/products/{id_or_slug}", response_model=ProductView)
async def get_product(id_or_slug: str, svc: Catalog) -> ProductView:
    return await svc.get_product(id_or_slug)


@router.get("/search", response_model=list[ProductView])
async def search(
    svc: Catalog, q: Annotated[str, Query(min_length=2, max_length=64)]
) -> list[ProductView]:
    return await svc.search(q)
