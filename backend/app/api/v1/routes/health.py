from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.route import TransactionalRoute
from app.db import base as db

# Neither route here writes anything, so TransactionalRoute is a no-op in
# practice — set for consistency with every other router, so "does this
# router touch the database" is never a question anyone has to answer
# before adding a route to it.
router = APIRouter(tags=["health"], route_class=TransactionalRoute)


@router.get("/health/live", summary="Liveness — is the process up")
async def live() -> dict[str, str]:
    # AAD-SEC-019: used to also return `"env": settings.env` — free,
    # unauthenticated reconnaissance (an attacker learns whether they've
    # found staging or production) for a value the load balancer's health
    # check has no use for at all.
    return {"status": "ok"}


@router.get("/health", summary="Readiness — can the process serve traffic")
async def ready(response: Response) -> dict[str, object]:
    """Readiness checks the database, so an instance that has lost Postgres is
    pulled out of the load balancer instead of serving 500s."""
    db_ok = await db.ping()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if db_ok else "degraded", "checks": {"database": db_ok}}
