from __future__ import annotations

import hashlib
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from app.api.deps import get_catalog_service
from app.api.route import TransactionalRoute
from app.core.rate_limit import IpRateLimiter
from app.schemas.catalog import CatalogResponse, ProductView
from app.services.catalog_service import CatalogService

router = APIRouter(prefix="/catalog", tags=["catalog"], route_class=TransactionalRoute)

Catalog = Annotated[CatalogService, Depends(get_catalog_service)]

# AAD-SEC-004: uncached (AAD-PERF-001), so every request is a database
# query — the fix's own suggested limit, keyed on the trusted client IP
# AAD-SEC-005 established.
_catalog_per_minute = IpRateLimiter(limit=60, seconds=60)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _etag_bytes(payload: bytes) -> str:
    return f'"{hashlib.sha256(payload).hexdigest()}"'


def _prime_cache_headers(request: Request, response: Response, etag: str, *, max_age: int) -> bool:
    """Set the cache headers a client should keep either way, and report
    whether this exact body was one it already had.

    AAD-PERF-001: gives a route the same cache opt-out `/catalog` already
    has. `SecurityHeadersMiddleware` defaults every response to
    `Cache-Control: no-store`; setting the header explicitly here — same as
    `/catalog` already does — opts back in without touching that default
    for any other route."""
    response.headers["Cache-Control"] = f"public, max-age={max_age}, stale-while-revalidate=300"
    response.headers["ETag"] = etag
    return request.headers.get("if-none-match") == etag


def _cache_model(
    request: Request, response: Response, body: _ModelT, *, max_age: int
) -> _ModelT | Response:
    """Product detail carries no `generated_at`-style timestamp the way
    `/catalog` does — the same product produces a byte-identical response
    until the underlying row actually changes, so hashing the serialized
    body is a valid cache-validation token here. A matching `If-None-Match`
    short-circuits to an empty 304 instead of re-sending the body."""
    etag = _etag_bytes(body.model_dump_json().encode())
    if _prime_cache_headers(request, response, etag, max_age=max_age):
        return Response(status_code=304, headers=dict(response.headers))
    return body


def _cache_model_list(
    request: Request, response: Response, body: list[_ModelT], *, max_age: int
) -> list[_ModelT] | Response:
    """Same as `_cache_model`, for the list-returning search endpoint."""
    payload = b"[" + b",".join(m.model_dump_json().encode() for m in body) + b"]"
    etag = _etag_bytes(payload)
    if _prime_cache_headers(request, response, etag, max_age=max_age):
        return Response(status_code=304, headers=dict(response.headers))
    return body


@router.get("", response_model=CatalogResponse, dependencies=[Depends(_catalog_per_minute)])
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
async def get_product(
    id_or_slug: str, svc: Catalog, request: Request, response: Response
) -> ProductView | Response:
    view = await svc.get_product(id_or_slug)
    return _cache_model(request, response, view, max_age=60)


@router.get("/search", response_model=list[ProductView])
async def search(
    svc: Catalog,
    request: Request,
    response: Response,
    q: Annotated[str, Query(min_length=2, max_length=64)],
) -> list[ProductView] | Response:
    views = await svc.search(q)
    return _cache_model_list(request, response, views, max_age=60)
