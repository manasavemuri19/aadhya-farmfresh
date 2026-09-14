"""Dependency wiring.

Repositories and services are constructed per request from the shared database
handle. They are cheap objects; the expensive resource (the Mongo connection
pool) is created once at startup.
"""

from __future__ import annotations

from typing import Annotated

from collections.abc import AsyncIterator

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, Unauthorized
from app.core.logging import user_id_var
from app.core.security import decode_token
from app.db.base import get_session_factory
from app.domain.enums import Role
from app.payments import PaymentProvider, get_payment_provider
from app.repositories.delivery import DeliveryRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.orders import OrderRepository
from app.repositories.otp import OtpRepository
from app.repositories.products import ProductRepository
from app.repositories.push_tokens import PushTokenRepository
from app.repositories.support import SupportRepository
from app.repositories.users import UserRepository
from app.services.auth_service import AuthService
from app.services.catalog_service import CatalogService
from app.services.delivery_service import DeliveryService
from app.services.order_service import OrderService
from app.services.push_service import PushService
from app.services.support_service import SupportService


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request.

    Everything a request writes commits together or not at all. A handler
    that raises rolls the whole thing back, which is why the checkout path
    needs no compensation logic of its own.

    This dependency only creates the session and hands it off — it does not
    commit, roll back, or close it. That used to happen right here, in the
    code after `yield`, but FastAPI runs that code during dependency
    teardown, which is *after* the route's Response has already been built.
    A commit failure there had nowhere to go (AAD-REL-001): the success
    response could already be on its way out. `TransactionalRoute`
    (api/route.py) now owns commit/rollback/close, wrapped around the whole
    endpoint call instead, so a commit failure is a normal exception raised
    before any response is handed off. Every router that resolves this
    dependency (directly or via a repository) uses
    `route_class=TransactionalRoute` for exactly this reason.
    """
    session = get_session_factory()()
    request.state.db_session = session
    yield session


DB = Annotated[AsyncSession, Depends(db_session)]


def get_product_repo(db: DB) -> ProductRepository:
    return ProductRepository(db)


def get_order_repo(db: DB) -> OrderRepository:
    return OrderRepository(db)


def get_user_repo(db: DB) -> UserRepository:
    return UserRepository(db)


def get_otp_repo(db: DB) -> OtpRepository:
    return OtpRepository(db)


def get_idempotency_repo(db: DB) -> IdempotencyRepository:
    return IdempotencyRepository(db)


def get_support_repo(db: DB) -> SupportRepository:
    return SupportRepository(db)


def get_delivery_repo(db: DB) -> DeliveryRepository:
    return DeliveryRepository(db)


def get_push_token_repo(db: DB) -> PushTokenRepository:
    return PushTokenRepository(db)


def get_push_service(
    tokens: Annotated[PushTokenRepository, Depends(get_push_token_repo)],
) -> PushService:
    return PushService(tokens)


def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repo)],
    otps: Annotated[OtpRepository, Depends(get_otp_repo)],
) -> AuthService:
    return AuthService(users, otps)


def get_catalog_service(
    products: Annotated[ProductRepository, Depends(get_product_repo)],
) -> CatalogService:
    return CatalogService(products)


def get_order_service(
    products: Annotated[ProductRepository, Depends(get_product_repo)],
    orders: Annotated[OrderRepository, Depends(get_order_repo)],
    idem: Annotated[IdempotencyRepository, Depends(get_idempotency_repo)],
    payments: Annotated[PaymentProvider, Depends(get_payment_provider)],
    users: Annotated[UserRepository, Depends(get_user_repo)],
    push: Annotated[PushService, Depends(get_push_service)],
) -> OrderService:
    # users/push are optional on OrderService itself (default None) so the
    # background housekeeping sweeper in main.py, which constructs an
    # OrderService directly rather than through this dependency chain, keeps
    # working unchanged — it just doesn't send notifications for the expired
    # holds it cancels. Every real HTTP request goes through here and gets
    # both wired automatically.
    return OrderService(products, orders, idem, payments, users=users, push=push)


def get_support_service(
    tickets: Annotated[SupportRepository, Depends(get_support_repo)],
) -> SupportService:
    return SupportService(tickets)


def get_delivery_service(
    deliveries: Annotated[DeliveryRepository, Depends(get_delivery_repo)],
    users: Annotated[UserRepository, Depends(get_user_repo)],
    orders: Annotated[OrderService, Depends(get_order_service)],
) -> DeliveryService:
    # DeliveryService delegates the actual status transition (packed / on
    # the way / delivered) to OrderService rather than duplicating the
    # transition-legality logic that already lives there — see
    # DeliveryService.update_status.
    return DeliveryService(deliveries, users, orders)


class Principal:
    """The authenticated caller."""

    __slots__ = ("user_id", "role")

    def __init__(self, user_id: str, role: str) -> None:
        self.user_id = user_id
        self.role = role

    @property
    def is_staff(self) -> bool:
        return self.role in {Role.STAFF.value, Role.ADMIN.value}

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    @property
    def is_delivery_agent(self) -> bool:
        return self.role == Role.DELIVERY_AGENT.value


async def current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Sign in to continue.")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token, expected_type="access")

    principal = Principal(payload["sub"], payload.get("role", Role.CUSTOMER.value))
    user_id_var.set(principal.user_id)
    return principal


CurrentUser = Annotated[Principal, Depends(current_user)]


async def require_staff(principal: CurrentUser) -> Principal:
    if not principal.is_staff:
        raise Forbidden("This area is for farm staff.")
    return principal


async def require_admin(principal: CurrentUser) -> Principal:
    if not principal.is_admin:
        raise Forbidden("This action needs an owner account.")
    return principal


async def require_delivery_agent(principal: CurrentUser) -> Principal:
    if not principal.is_delivery_agent:
        raise Forbidden("This area is for delivery agents.")
    return principal


StaffUser = Annotated[Principal, Depends(require_staff)]
AdminUser = Annotated[Principal, Depends(require_admin)]
DeliveryAgentUser = Annotated[Principal, Depends(require_delivery_agent)]


async def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    if idempotency_key and (len(idempotency_key) > 128 or len(idempotency_key) < 8):
        raise Unauthorized("Idempotency-Key must be between 8 and 128 characters.")
    return idempotency_key
