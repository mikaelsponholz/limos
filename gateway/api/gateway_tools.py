"""Contract gateway routes. Domain operations also serve the local runner."""

from typing import Annotated

from fastapi import APIRouter, Depends

from gateway import tools
from gateway.api.dependencies import Resources, domain_errors, json_body, resources
from gateway.errors import GatewayError
from gateway.schemas import (
    OffersBody,
    OffersResponse,
    Order,
    OrderBody,
    Quote,
    QuoteBody,
    RequestApprovalBody,
    RequestApprovalResponse,
    SearchBody,
    SearchResponse,
)

router = APIRouter(prefix="/gateway")
Env = Annotated[Resources, Depends(resources)]


@router.post("/search", response_model=SearchResponse)
def search(body: Annotated[SearchBody, json_body(SearchBody)], env: Env) -> SearchResponse:
    return tools.search_products(body, env)


@router.post("/offers", response_model=OffersResponse)
async def offers(body: Annotated[OffersBody, json_body(OffersBody)], env: Env) -> OffersResponse:
    with domain_errors():
        return OffersResponse(offers=await tools.check_offers(body, env))


@router.post("/quotes", response_model=Quote)
async def quote(body: Annotated[QuoteBody, json_body(QuoteBody)], env: Env) -> Quote:
    with domain_errors():
        return await tools.request_quote(body, env)


@router.post("/quotes/{quote_id}/request-approval", response_model=RequestApprovalResponse)
def approval(
    quote_id: str,
    body: Annotated[RequestApprovalBody, json_body(RequestApprovalBody)],
    env: Env,
) -> RequestApprovalResponse:
    return tools.request_approval(quote_id, body, env)


@router.post("/orders", response_model=Order)
def order(body: Annotated[OrderBody, json_body(OrderBody)], env: Env) -> Order:
    return tools.place_order(body, env)


@router.get("/orders/{order_id}", response_model=Order)
def order_status(order_id: str, buyer_id: str, agent_id: str, env: Env) -> Order:
    env.permit(buyer_id, agent_id)
    result = env.store.get_order(order_id, buyer_id)
    if result is None:
        raise GatewayError(404, "NOT_FOUND", "Order not found.")
    return result
