from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    admin,
    auth,
    catalog,
    delivery,
    health,
    notifications,
    orders,
    payments,
    support,
)

# AAD-OPS-004: the single source of truth for where this router is mounted.
# `app/main.py`'s `include_router` and `app/api/middleware.py`'s health-path
# log suppression both import this instead of each hardcoding "/v1" —
# previously only `main.py` had it, the middleware's own copy read "/health"
# with no prefix, the two silently drifted apart, and every health-check
# probe ended up logged instead of suppressed.
API_V1_PREFIX = "/v1"

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(catalog.router)
api_router.include_router(orders.router)
api_router.include_router(payments.router)
api_router.include_router(admin.router)
api_router.include_router(support.router)
api_router.include_router(delivery.router)
api_router.include_router(notifications.router)
