"""Human decisions, with a fresh availability check before committing approval."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter

from gateway import quotes, tools
from gateway.api.dependencies import Resources, domain_errors, json_body
from gateway.api.gateway_tools import Env
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.requote import requote_expired
from gateway.schemas import (
    ApprovalDetail,
    ApprovalListEntry,
    ApprovalRequestSummary,
    ApprovalStatus,
    ApproveBody,
    ApproveResponse,
    OrderBody,
    Quote,
    RejectBody,
    RejectResponse,
)
from gateway.store import ApprovalRecord
from gateway.valio.client import ValioClient
from gateway.valio.models import normalize_amount

router = APIRouter(prefix="/approvals")


def find_approval(approval_id: str, env: Resources) -> ApprovalRecord:
    for buyer in env.mandates:
        approval = env.store.get_approval(approval_id, buyer)
        if approval is not None:
            return approval
    raise GatewayError(404, "NOT_FOUND", "Approval not found.")


def selected_quote(approval: ApprovalRecord, body: ApproveBody, env: Resources) -> Quote:
    if approval.status == "EXPIRED":
        raise GatewayError(410, "QUOTE_EXPIRED", "Quote has expired; request a new quote.")
    if approval.status != "PENDING":
        raise GatewayError(409, "ALREADY_DECIDED", "Approval has already been decided.")
    if body.quote_id not in approval.option_quote_ids:
        raise GatewayError(409, "QUOTE_CHANGED", "Quote does not match an approval option.")
    quote = quotes.get_quote(env.store, body.quote_id, approval.buyer_id, env.now())
    if body.content_hash != quote.content_hash or body.content_hash != quotes.compute_content_hash(
        quote
    ):
        raise GatewayError(409, "QUOTE_CHANGED", "Quote content hash does not match.")
    quotes.require_unexpired(quote, env.now())
    return quote


@router.get("", response_model=list[ApprovalListEntry])
def list_approvals(
    env: Env, status: ApprovalStatus = "PENDING", buyer_id: str | None = None
) -> list[ApprovalListEntry]:
    entries = []
    for buyer in [buyer_id] if buyer_id else env.mandates:
        for approval in env.store.list_approvals(buyer):
            quote = quotes.get_quote(env.store, approval.quote_id, buyer, env.now())
            effective_status = (
                "EXPIRED"
                if approval.status == "PENDING" and quote.status == "EXPIRED"
                else approval.status
            )
            if effective_status != status:
                continue
            entries.append(
                ApprovalListEntry(
                    approval_id=approval.approval_id,
                    request_id=approval.request_id,
                    quote_id=approval.quote_id,
                    buyer_id=buyer,
                    created_at=approval.created_at,
                    summary=approval.explanation,
                    reasons=[rule.rule_id for rule in quote.policy.rules_fired],
                    order_deadline=quote.order_deadline,
                    expires_at=quote.expires_at,
                )
            )
    return sorted(entries, key=lambda entry: entry.created_at, reverse=True)


@router.get("/{approval_id}", response_model=ApprovalDetail)
def get_approval(approval_id: str, env: Env) -> ApprovalDetail:
    approval = find_approval(approval_id, env)
    quote = quotes.get_quote(env.store, approval.quote_id, approval.buyer_id, env.now())
    request = env.store.get_request(approval.request_id, approval.buyer_id)
    if request is None:
        raise GatewayError(404, "NOT_FOUND", "Approval request not found.")
    return ApprovalDetail(
        approval_id=approval_id,
        status="EXPIRED"
        if approval.status == "PENDING" and quote.status == "EXPIRED"
        else approval.status,
        quote=quote,
        request=ApprovalRequestSummary(
            request_id=request.request_id, title=tools.title(request), intent=request.intent
        ),
        options=[
            quotes.get_quote(env.store, key, approval.buyer_id, env.now())
            for key in approval.option_quote_ids
        ]
        if len(approval.option_quote_ids) > 1
        else [],
        decided_by=approval.decided_by,
        decided_at=approval.decided_at,
        comment=approval.comment,
    )


async def recheck(quote: Quote, env: Resources) -> None:
    quantities: dict[str, Decimal] = {}
    for line in quote.lines:
        quantities[line.product_id] = quantities.get(line.product_id, Decimal(0)) + Decimal(
            str(line.quantity_sales_units)
        )
    with domain_errors():
        valio = env.valio
        availability = (
            await valio.availability(quantities, quote.delivery_date, refresh=True)
            if isinstance(valio, ValioClient)
            else await valio.availability(quantities, quote.delivery_date)
        )
    stock = {entry.product_id: entry for entry in availability.products}
    changed = []
    for code, quantity in quantities.items():
        product = stock.get(code)
        deliverable = False
        if product is not None and product.deliverable:
            for schedule in product.schedule_lines:
                amount = normalize_amount(product.logistic_model, schedule.available_amount).amount
                if (
                    not schedule.sales_block
                    and schedule.available_amount > 0
                    and schedule.available_date is not None
                    and datetime.fromisoformat(schedule.available_date).date()
                    <= quote.delivery_date
                    and (amount is None or amount >= quantity)
                ):
                    deliverable = True
                    break
        if not deliverable:
            changed.append(code)
    if quote.request_id:
        append_step(
            env.store,
            quote.request_id,
            "gateway",
            "AVAILABILITY_RECHECKED",
            "Rechecked availability for the locked quantities",
            {"product_ids": list(quantities), "changed_product_ids": list(changed)},
            None,
            env.now(),
        )
    if changed:
        raise GatewayError(
            409, "AVAILABILITY_CHANGED", "The locked quantities are no longer deliverable."
        )


@router.post("/{approval_id}/approve", response_model=ApproveResponse)
async def approve(
    approval_id: str, body: Annotated[ApproveBody, json_body(ApproveBody)], env: Env
) -> ApproveResponse:
    approval = find_approval(approval_id, env)
    try:
        quote = selected_quote(approval, body, env)
    except GatewayError as exc:
        if exc.code == "QUOTE_EXPIRED":
            await requote_expired(approval, env)
        raise
    env.permit(quote.buyer_id, quote.agent_id)
    await recheck(quote, env)
    try:
        with env.store.transaction():
            quotes.approve(
                approval_id,
                body.approver,
                body.quote_id,
                body.content_hash,
                body.comment,
                env.now(),
                store=env.store,
                buyer_id=approval.buyer_id,
            )
            order = tools.place_order(
                OrderBody(
                    buyer_id=quote.buyer_id,
                    agent_id=quote.agent_id,
                    quote_id=quote.quote_id,
                    idempotency_key=f"{quote.request_id}-{quote.quote_id}",
                ),
                env,
            )
    except GatewayError as exc:
        if exc.code == "HASH_MISMATCH":
            raise GatewayError(409, "QUOTE_CHANGED", exc.message) from None
        if exc.code == "QUOTE_EXPIRED":
            await requote_expired(approval, env)
        raise
    return ApproveResponse(status="APPROVED", order=order)


@router.post("/{approval_id}/reject", response_model=RejectResponse)
def reject(
    approval_id: str, body: Annotated[RejectBody, json_body(RejectBody)], env: Env
) -> RejectResponse:
    approval = find_approval(approval_id, env)
    with env.store.transaction():
        quotes.reject(
            approval_id,
            body.approver,
            body.reason,
            env.now(),
            store=env.store,
            buyer_id=approval.buyer_id,
        )
        detail = env.store.get_request(approval.request_id, approval.buyer_id)
        if detail:
            detail.status = "REJECTED"
            detail.quote = env.store.get_quote(approval.quote_id, approval.buyer_id)
            env.store.save_request(detail)
    return RejectResponse(status="REJECTED")
