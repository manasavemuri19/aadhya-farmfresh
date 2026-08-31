"""The delivery agent's entire API surface: two lists and one action."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import DeliveryAgentUser, get_delivery_service
from app.schemas.delivery import AgentLocationUpdate, DeliveryOrderView
from app.services.delivery_service import DeliveryService

router = APIRouter(prefix="/delivery", tags=["delivery"])

Delivery = Annotated[DeliveryService, Depends(get_delivery_service)]


@router.get("/requests", response_model=list[DeliveryOrderView])
async def new_requests(agent: DeliveryAgentUser, svc: Delivery) -> list[DeliveryOrderView]:
    return await svc.list_requests(agent.user_id)


@router.get("/ongoing", response_model=list[DeliveryOrderView])
async def ongoing(agent: DeliveryAgentUser, svc: Delivery) -> list[DeliveryOrderView]:
    return await svc.list_ongoing(agent.user_id)


@router.post("/orders/{order_id}/accept", response_model=DeliveryOrderView)
async def accept_request(
    order_id: str, agent: DeliveryAgentUser, svc: Delivery
) -> DeliveryOrderView:
    return await svc.accept(order_id, agent.user_id)


@router.post("/location", status_code=204)
async def report_location(
    body: AgentLocationUpdate, agent: DeliveryAgentUser, svc: Delivery
) -> None:
    await svc.update_location(agent.user_id, latitude=body.latitude, longitude=body.longitude)
