"""Claim optional ideas and re-quote them for a fresh human decision."""

from agent.constraints import item_constraints
from gateway import quotes, tools
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.schemas import Error, IntentItem, QuoteBody, QuoteLine, RequestDetail, Suggestion


def find_suggestion(detail: RequestDetail, suggestion_id: str) -> Suggestion:
    suggestion = next((s for s in detail.suggestions if s.suggestion_id == suggestion_id), None)
    if suggestion is None:
        raise GatewayError(404, "NOT_FOUND", "Suggestion not found.")
    return suggestion


def claim_suggestion(request_id: str, suggestion_id: str, env: Resources) -> None:
    with env.store.transaction():
        detail = tools.request_detail(env, request_id, env.request_buyer(request_id))
        suggestion = find_suggestion(detail, suggestion_id)
        if detail.status != "WAITING_APPROVAL" or suggestion.status != "OFFERED":
            raise GatewayError(409, "SUGGESTION_NOT_OFFERED", "Suggestion is no longer offered.")
        approval = (
            env.store.get_approval(detail.approval_id, detail.buyer_id)
            if detail.approval_id
            else None
        )
        if approval is None or approval.status != "PENDING" or suggestion.offer is None:
            raise GatewayError(409, "SUGGESTION_NOT_OFFERED", "Suggestion is no longer offered.")
        approval.status = "EXPIRED"
        env.store.save_approval(approval)
        for quote_id in approval.option_quote_ids:
            old = env.store.get_quote(quote_id, detail.buyer_id)
            if old is not None:
                old.status = "EXPIRED"
                env.store.save_quote(old)
        detail.status = "RUNNING"
        env.store.save_request(detail)


async def accept_suggestion(request_id: str, suggestion_id: str, env: Resources) -> None:
    buyer_id = env.request_buyer(request_id)
    try:
        detail = tools.request_detail(env, request_id, buyer_id)
        suggestion = find_suggestion(detail, suggestion_id)
        old = detail.quote
        offer = suggestion.offer
        assert old is not None and offer is not None
        if suggestion.replaces_item_id is None:
            detail.intent.items.append(
                IntentItem(
                    item_id=offer.item_id,
                    description=offer.name,
                    description_fi=offer.name,
                    quantity=offer.quantity_requested.value,
                    unit=offer.quantity_requested.unit,
                    constraints=item_constraints(
                        detail.intent.preferences, detail.intent.exclude_allergens
                    ),
                    used_in=[],
                )
            )
            env.store.save_request(detail)
        lines = [
            QuoteLine(
                item_id=line.item_id,
                product_id=line.product_id,
                quantity_sales_units=line.quantity_sales_units,
                substitute_for=line.substitute_for,
                substitute_source=line.substitute_source,
            )
            for line in old.lines
            if line.item_id != suggestion.replaces_item_id
        ]
        lines.append(
            QuoteLine(
                item_id=offer.item_id,
                product_id=offer.product_id,
                quantity_sales_units=offer.quantity_sales_units,
            )
        )
        tools.tool_step(env, request_id, "request_quote")
        quote = await tools.request_quote(
            QuoteBody(
                buyer_id=buyer_id,
                agent_id=old.agent_id,
                request_id=request_id,
                requested_date=detail.intent.deliver_by,
                lines=lines,
                budget=detail.intent.budget,
            ),
            env,
        )
        with env.store.transaction():
            detail = tools.request_detail(env, request_id, buyer_id)
            find_suggestion(detail, suggestion_id).status = "ACCEPTED"
            detail.approval_id = None
            if quote.policy.outcome == "BLOCK":
                detail.status = "BLOCKED"
            else:
                approval = quotes.request_approval(
                    quote.quote_id,
                    buyer_id,
                    old.agent_id,
                    "Review the quote with your accepted suggestion.",
                    store=env.store,
                    now=env.now(),
                    id_generator=lambda: env.new_id("a_"),
                    allow_auto=True,
                )
                detail.quote = env.store.get_quote(quote.quote_id, buyer_id)
                detail.approval_id = approval.approval_id
                detail.status = "WAITING_APPROVAL"
            env.store.save_request(detail)
            append_step(
                env.store,
                request_id,
                "user",
                "SUGGESTION_ACCEPTED",
                "Accepted an optional idea and requested a new quote",
                {
                    "suggestion_id": suggestion_id,
                    "quote_id": quote.quote_id,
                    "outcome": quote.policy.outcome,
                    "reasons": [rule.description for rule in quote.policy.rules_fired],
                },
                None,
                env.now(),
            )
    except Exception as exc:
        with env.store.transaction():
            detail = tools.request_detail(env, request_id, buyer_id)
            detail.status = "FAILED"
            code = exc.code if isinstance(exc, GatewayError) else "SUGGESTION_FAILED"
            detail.error = Error(
                code=code, message="Could not quote the suggestion. No order placed."
            )
            env.store.save_request(detail)
            append_step(
                env.store,
                request_id,
                "gateway",
                "ERROR",
                detail.error.message,
                {"code": code},
                None,
                env.now(),
            )
