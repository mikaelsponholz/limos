from fastapi import APIRouter, BackgroundTasks

from gateway import customers, runner, tools
from gateway.api.gateway_tools import Env
from gateway.api.presets import RequestBody
from gateway.errors import GatewayError
from gateway.schemas import AnswerBody, RequestCreated, RequestDetail, RequestListEntry

router = APIRouter()


@router.post("/requests/{request_id}/answer", status_code=202, response_model=RequestCreated)
async def answer_request(
    request_id: str, body: AnswerBody, background: BackgroundTasks, env: Env
) -> RequestCreated:
    resolved = runner.answer_question(request_id, body.question_id, body.answer, env)
    background.add_task(runner.run, request_id, resolved, env, resume=True)
    return RequestCreated(request_id=request_id, status="RUNNING")


@router.post("/requests", status_code=202, response_model=RequestCreated)
async def create_request(
    body: RequestBody, background: BackgroundTasks, env: Env
) -> RequestCreated:
    detail, resolved = runner.prepare(body, env)
    background.add_task(runner.run, detail.request_id, resolved, env)
    return RequestCreated(request_id=detail.request_id, status="RUNNING")


@router.get("/requests", response_model=list[RequestListEntry])
def list_requests(
    env: Env, buyer_id: str | None = None, customer_id: str | None = None
) -> list[RequestListEntry]:
    if customer_id:
        customer = customers.get_customer(customer_id, env.store, **env.customer_paths)
        if buyer_id is not None and buyer_id != customer.buyer_id:
            return []
        buyer_id = customer.buyer_id
    if buyer_id is None:
        raise GatewayError(422, "BUYER_REQUIRED", "Filter by buyer_id or customer_id.")
    details = sorted(env.store.list_requests(buyer_id), key=lambda r: r.created_at, reverse=True)
    return [
        RequestListEntry(
            request_id=detail.request_id,
            customer_id=detail.customer_id,
            created_at=detail.created_at,
            title=tools.title(detail),
            status=detail.status,
            total=detail.quote.total if detail.quote else 0.0,
            delivery_date=detail.quote.delivery_date if detail.quote else None,
            order_deadline=detail.quote.order_deadline if detail.quote else None,
        )
        for detail in details
        if customer_id is None or detail.customer_id == customer_id
    ]


@router.get("/requests/{request_id}", response_model=RequestDetail)
def get_request(request_id: str, env: Env) -> RequestDetail:
    return tools.request_detail(env, request_id, env.request_buyer(request_id))
