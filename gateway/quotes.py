"""Price-locked quotes and human decisions bound to the chosen quote's hash."""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from gateway.audit import append_step, canonical_hash
from gateway.errors import GatewayError
from gateway.policy import ItemContext, MandateEntry, budget_status, evaluate
from gateway.schemas import Offer, Quote, QuoteStatus
from gateway.store import ApprovalRecord, Store


def check_now(now: datetime) -> None:
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone.")


def compute_content_hash(quote: Quote) -> str:
    return canonical_hash(
        quote.model_dump(
            mode="json",
            include={
                "lines",
                "total",
                "total_with_tax",
                "delivery_date",
                "expires_at",
            },
        )
    )


def _sum_money(values: Sequence[float]) -> float:
    return float(
        sum((Decimal(str(value)) for value in values), Decimal(0)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def create_quote(
    lines: Sequence[Offer],
    *,
    store: Store,
    request_id: str | None,
    buyer_id: str,
    agent_id: str,
    requested_date: date,
    item_context: Mapping[str, ItemContext],
    mandate: MandateEntry,
    now: datetime,
    id_generator: Callable[[], str],
    budget: float | None = None,
    option_label: str | None = None,
    agent_explanation: str = "",
    previous_prices: Mapping[tuple[str, str], float] | None = None,
) -> Quote:
    check_now(now)
    if mandate.buyer_id != buyer_id:
        raise ValueError("Mandate must belong to the quote buyer.")
    locked_lines = [line.model_copy(deep=True) for line in lines]
    total = _sum_money([line.line_total for line in locked_lines])
    policy = evaluate(locked_lines, item_context, mandate, agent_id, budget, previous_prices)
    statuses: dict[str, QuoteStatus] = {
        "AUTO": "AUTO_APPROVED",
        "NEEDS_APPROVAL": "PENDING_APPROVAL",
        "BLOCK": "BLOCKED",
    }
    closing_times = [
        line.closing_time
        for line in locked_lines
        if line.closing_time is not None and line.closing_time > now
    ]
    deadlines = [line.order_deadline for line in locked_lines if line.order_deadline is not None]
    carbon = [
        Decimal(str(line.carbon_kg_co2e_per_kg))
        * Decimal(str(line.quantity_covered.value))
        * (Decimal("0.001") if line.quantity_covered.unit == "g" else Decimal(1))
        for line in locked_lines
        if line.carbon_kg_co2e_per_kg is not None and line.quantity_covered.unit in {"g", "kg"}
    ]
    quote = Quote(
        quote_id=id_generator(),
        request_id=request_id or "",
        buyer_id=buyer_id,
        agent_id=agent_id,
        status=statuses[policy.outcome],
        option_label=option_label,
        delivery_date=max(
            (line.delivery_date for line in locked_lines if line.delivery_date is not None),
            default=requested_date,
        ),
        order_deadline=min(deadlines, default=None),
        lines=locked_lines,
        total=total,
        total_with_tax=_sum_money([line.line_total_with_tax for line in locked_lines]),
        co2e_total_kg=float(sum(carbon, Decimal(0))) if carbon else None,
        budget=budget_status(total, budget),
        created_at=now,
        expires_at=min([now + timedelta(minutes=30), *closing_times]),
        content_hash="",
        policy=policy,
        agent_explanation=agent_explanation,
    )
    quote.content_hash = compute_content_hash(quote)
    with store.transaction():
        if store.get_quote(quote.quote_id, buyer_id) is not None:
            raise ValueError("Quote id generator returned an existing id.")
        store.save_quote(quote)
        if quote.request_id:
            append_step(
                store,
                quote.request_id,
                "gateway",
                "QUOTE_CREATED",
                "Quote created",
                {"quote_id": quote.quote_id, "total": total, "content_hash": quote.content_hash},
                None,
                now,
            )
            append_step(
                store,
                quote.request_id,
                "policy",
                "POLICY_DECISION",
                "Policy evaluated",
                {
                    "quote_id": quote.quote_id,
                    "outcome": policy.outcome,
                    "rules": [rule.rule_id for rule in policy.rules_fired],
                },
                None,
                now,
            )
    return quote.model_copy(deep=True)


def get_quote(store: Store, quote_id: str, buyer_id: str, now: datetime) -> Quote:
    check_now(now)
    quote = store.get_quote(quote_id, buyer_id)
    if quote is None:
        raise GatewayError(404, "NOT_FOUND", "Quote not found.")
    if quote.status != "ORDERED" and now >= quote.expires_at:
        quote.status = "EXPIRED"
    return quote


def require_unexpired(quote: Quote, now: datetime) -> None:
    check_now(now)
    if now >= quote.expires_at or quote.status == "EXPIRED":
        raise GatewayError(410, "QUOTE_EXPIRED", "Quote has expired; request a new quote.")


def _approvable(quote: Quote, now: datetime) -> None:
    require_unexpired(quote, now)
    if quote.status == "BLOCKED" or quote.policy.outcome == "BLOCK":
        raise GatewayError(422, "BLOCKED", "Quote is blocked by policy.")
    if quote.status not in {"PENDING_APPROVAL", "AUTO_APPROVED"}:
        raise GatewayError(409, "ALREADY_DECIDED", "Quote cannot receive another decision.")


def request_approval(
    quote_id: str,
    buyer_id: str,
    agent_id: str,
    explanation: str,
    alternative_quote_ids: Sequence[str] | None = None,
    *,
    store: Store,
    now: datetime,
    id_generator: Callable[[], str],
    allow_auto: bool = False,
) -> ApprovalRecord:
    with store.transaction():
        quote = get_quote(store, quote_id, buyer_id, now)
        _approvable(quote, now)
        if quote.status != "PENDING_APPROVAL" and not allow_auto:
            raise GatewayError(403, "NEEDS_APPROVAL", "Only a pending quote needs approval.")
        options = list(dict.fromkeys([quote_id, *(alternative_quote_ids or [])]))
        if len(options) > 3:
            raise ValueError("An approval can contain at most three quotes in total.")
        for option_id in options:
            option = get_quote(store, option_id, buyer_id, now)
            if option.request_id != quote.request_id:
                raise GatewayError(404, "NOT_FOUND", "Option quote not found for this request.")
            _approvable(option, now)
            if allow_auto and option.status == "AUTO_APPROVED":
                # Preserve the policy result while requiring the human's decision.
                option.status = "PENDING_APPROVAL"
                store.save_quote(option)
        approval = ApprovalRecord(
            approval_id=id_generator(),
            buyer_id=buyer_id,
            agent_id=agent_id,
            quote_id=quote_id,
            request_id=quote.request_id,
            option_quote_ids=options,
            explanation=explanation,
            created_at=now,
        )
        if store.get_approval(approval.approval_id, buyer_id) is not None:
            raise ValueError("Approval id generator returned an existing id.")
        store.save_approval(approval)
        if quote.request_id:
            append_step(
                store,
                quote.request_id,
                "agent",
                "APPROVAL_REQUESTED",
                "Human approval requested",
                {
                    "approval_id": approval.approval_id,
                    "quote_id": quote_id,
                    "option_quote_ids": list(options),
                },
                None,
                now,
            )
        return approval


def _pending_approval(store: Store, approval_id: str, buyer_id: str) -> ApprovalRecord:
    approval = store.get_approval(approval_id, buyer_id)
    if approval is None:
        raise GatewayError(404, "NOT_FOUND", "Approval not found.")
    if approval.status != "PENDING":
        raise GatewayError(409, "ALREADY_DECIDED", "Approval has already been decided.")
    return approval


def approve(
    approval_id: str,
    approver: str,
    quote_id: str,
    content_hash: str,
    comment: str,
    now: datetime,
    *,
    store: Store,
    buyer_id: str,
) -> ApprovalRecord:
    with store.transaction():
        approval = _pending_approval(store, approval_id, buyer_id)
        if quote_id not in approval.option_quote_ids:
            raise GatewayError(404, "NOT_FOUND", "Quote is not an approval option.")
        quote = get_quote(store, quote_id, buyer_id, now)
        _approvable(quote, now)
        if content_hash != quote.content_hash or content_hash != compute_content_hash(quote):
            raise GatewayError(409, "HASH_MISMATCH", "Quote content hash does not match.")
        approval.status = "APPROVED"
        approval.selected_quote_id = quote_id
        approval.approved_hash = content_hash
        approval.decided_by = approver
        approval.decided_at = now
        approval.comment = comment
        quote.status = "APPROVED"
        store.save_approval(approval)
        store.save_quote(quote)
        if quote.request_id:
            append_step(
                store,
                quote.request_id,
                "human",
                "APPROVED",
                "Quote approved",
                {
                    "approval_id": approval_id,
                    "quote_id": quote_id,
                    "approver": approver,
                    "content_hash": content_hash,
                },
                None,
                now,
            )
        return approval


def reject(
    approval_id: str,
    approver: str,
    reason: str,
    now: datetime,
    *,
    store: Store,
    buyer_id: str,
) -> ApprovalRecord:
    with store.transaction():
        approval = _pending_approval(store, approval_id, buyer_id)
        quote = get_quote(store, approval.quote_id, buyer_id, now)
        _approvable(quote, now)
        approval.status = "REJECTED"
        approval.decided_by = approver
        approval.decided_at = now
        approval.comment = reason
        quote.status = "REJECTED"
        store.save_approval(approval)
        store.save_quote(quote)
        if quote.request_id:
            append_step(
                store,
                quote.request_id,
                "human",
                "REJECTED",
                "Quote rejected",
                {
                    "approval_id": approval_id,
                    "quote_id": quote.quote_id,
                    "approver": approver,
                    "reason": reason,
                },
                None,
                now,
            )
        return approval
