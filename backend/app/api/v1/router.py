from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import admin, auth, catalog, health, orders, payments, support

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(catalog.router)
api_router.include_router(orders.router)
api_router.include_router(payments.router)
api_router.include_router(admin.router)
api_router.include_router(support.router)
