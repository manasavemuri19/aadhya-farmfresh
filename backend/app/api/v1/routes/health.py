from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.db import base as db

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="Liveness — is the process up")
async def live() -> dict[str, str]:
    return {"status": "ok", "env": settings.env}


@router.get("/health", summary="Readiness — can the process serve traffic")
async def ready(response: Response) -> dict[str, object]:
    """Readiness checks the database, so an instance that has lost Postgres is
    pulled out of the load balancer instead of serving 500s."""
    db_ok = await db.ping()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if db_ok else "degraded", "checks": {"database": db_ok}}
