"""Deterministic item-list and reorder runner, without model calls."""

from datetime import timedelta
from fractions import Fraction
from typing import cast

from gateway import customers, tools
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.policy.engine import REQUIRE_HARD_DIETARY_KEYS
from gateway.schemas import (
    Constraint,
    Error,
    FilledGap,
    Intent,
    IntentItem,
    OffersBody,
    OrderBody,
    QuoteBody,
    QuoteLine,
    RequestApprovalBody,
    RequestCreate,
    RequestDetail,
    RequestInput,
    SearchBody,
    StepType,
)

AGENT_ID = "limos-runner"


def prepare(body: RequestCreate, env: Resources) -> tuple[RequestDetail, RequestCreate]:
    customer = (
        customers.get_customer(body.customer_id, env.store, **env.customer_paths)
        if body.customer_id
        else None
    )
    resolved, gaps = customers.apply_profile(body, customer)
    buyer = resolved.buyer_id
    if buyer is None:
        raise GatewayError(422, "BUYER_REQUIRED", "A buyer is required.")
    env.permit(buyer, AGENT_ID)
    now = env.now()
    deliver_by = resolved.requested_date
    if deliver_by is None:
        deliver_by = now.date() + timedelta(days=1)
        gaps.append(
            FilledGap(
                field="deliver_by",
                value=deliver_by.isoformat(),
                source="default",
                note="Delivery requested for the next day.",
            )
        )
    hard: list[Constraint] = [
        p.key
        for p in resolved.preferences
        if p.level == "require" and p.key in REQUIRE_HARD_DIETARY_KEYS
    ]
    hard.extend(cast(Constraint, f"no_{code}") for code in resolved.exclude_allergens)
    items = [
        IntentItem(
            **item.model_dump(exclude={"constraints"}),
            item_id=f"it_{index}",
            description_fi=item.description,
            used_in=[],
            constraints=list(dict.fromkeys([*item.constraints, *hard])),
        )
        for index, item in enumerate(resolved.items, 1)
    ]
    intent = Intent(
        event=None,
        dishes=[],
        items=items,
        deliver_by=deliver_by,
        preferences=resolved.preferences,
        exclude_allergens=resolved.exclude_allergens,
        budget=resolved.budget,
        priority=resolved.priority,
        assumptions=[],
        based_on=None,
        filled_gaps=gaps,
    )
    detail = RequestDetail(
        request_id=env.new_id("r_"),
        buyer_id=buyer,
        customer_id=resolved.customer_id,
        preset_id=resolved.preset_id,
        status="RUNNING",
        created_at=now,
        input=RequestInput(text=resolved.text, image_url=None),
        intent=intent,
        question=None,
        steps=[],
        candidates=[],
        quote=None,
        suggestions=[],
        approval_id=None,
        order=None,
        error=None,
    )
    env.store.save_request(detail)
    return detail, resolved


async def run(request_id: str, body: RequestCreate, env: Resources) -> None:
    detail = tools.request_detail(env, request_id, env.request_buyer(request_id))

    def log(kind: StepType, title: str) -> None:
        append_step(env.store, request_id, "agent", kind, title, {}, None, env.now())

    try:
        append_step(
            env.store,
            request_id,
            "user",
            "REQUEST_RECEIVED",
            "Request received",
            {},
            None,
            env.now(),
        )
        if detail.customer_id:
            log("CUSTOMER_LOADED", "Customer profile loaded")
        lines: list[QuoteLine] = []
        if body.reorder_of:
            if detail.customer_id is None:
                raise GatewayError(422, "CUSTOMER_REQUIRED", "A reorder needs a customer.")
            customer = customers.get_customer(detail.customer_id, env.store, **env.customer_paths)
            based_on, reorder_lines = customers.resolve_reorder(
                customer, body.reorder_of, body.event.guests if body.event else None
            )
            detail.intent.based_on = based_on
            for index, line in enumerate(reorder_lines, 1):
                if env.catalog.lookup(line.product_id) is None:
                    log("EXCEPTION", f"Skipped missing product {line.product_id}")
                    continue
                item_id = f"it_{index}"
                detail.intent.items.append(
                    IntentItem(
                        item_id=item_id,
                        description=line.name,
                        description_fi=line.name,
                        quantity=float(line.quantity_sales_units),
                        unit="pcs",
                        constraints=list(
                            dict.fromkeys(
                                [
                                    p.key
                                    for p in detail.intent.preferences
                                    if p.level == "require" and p.key in REQUIRE_HARD_DIETARY_KEYS
                                ]
                                + [
                                    cast(Constraint, f"no_{code}")
                                    for code in detail.intent.exclude_allergens
                                ]
                            )
                        ),
                        used_in=[],
                    )
                )
                lines.append(
                    QuoteLine(
                        item_id=item_id,
                        product_id=line.product_id,
                        quantity_sales_units=float(line.quantity_sales_units),
                    )
                )
        elif not detail.intent.items:
            raise GatewayError(
                503,
                "AGENT_NOT_AVAILABLE",
                "Text and event requests need the Gemini agent, which is not available yet.",
            )
        log("INTENT_PARSED", "Item quantities and requirements resolved")
        env.store.save_request(detail)
        if body.reorder_of:
            log("CANDIDATES", f"Resolved {len(lines)} products from the past order")
            log("CHOICE", "Selected the past order's products and scaled sales units")
        if not body.reorder_of:
            for item in detail.intent.items:
                tools.tool_step(env, request_id, "search_products")
                shortlist = tools.search_products(
                    SearchBody(
                        buyer_id=detail.buyer_id,
                        agent_id=AGENT_ID,
                        query=item.description,
                        constraints=item.constraints,
                        exclude_allergens=detail.intent.exclude_allergens,
                        limit=5,
                    ),
                    env,
                )
                tools.tool_step(env, request_id, "check_offers")
                offers = await tools.check_offers(
                    OffersBody(
                        buyer_id=detail.buyer_id,
                        agent_id=AGENT_ID,
                        product_ids=[p.product_id for p in shortlist.products],
                        item_id=item.item_id,
                        quantity=item.quantity,
                        unit=item.unit,
                        requested_date=detail.intent.deliver_by,
                        constraints=list(
                            dict.fromkeys(
                                [
                                    *item.constraints,
                                    *[
                                        p.key
                                        for p in detail.intent.preferences
                                        if p.level == "require"
                                    ],
                                ]
                            )
                        ),
                        exclude_allergens=detail.intent.exclude_allergens,
                    ),
                    env,
                )
                detail.candidates.extend(offers)
                env.store.save_request(detail)
                log("CANDIDATES", f"Checked {len(offers)} offers for {item.description}")
                offers.sort(key=lambda offer: offer.product_id)
                if not offers:
                    raise GatewayError(
                        404, "NO_CANDIDATES", "No products match the requested item."
                    )
                safe = [
                    offer
                    for offer in offers
                    if not (
                        {"NOT_DELIVERABLE", "SALES_BLOCK", "PRICE_MISSING"} & set(offer.flags)
                        or set(offer.allergens.contains) & set(detail.intent.exclude_allergens)
                        or any(
                            offer.dietary_check.get(key) == "violated" for key in item.constraints
                        )
                    )
                ]
                chosen = (safe or offers)[0]
                record = env.catalog.lookup(chosen.product_id)
                assert record is not None
                # An undeliverable offer can contain zero units. Lock one valid lot
                # so fixed-line pricing records its BLOCK evidence in the quote.
                units = chosen.quantity_sales_units or float(
                    Fraction(str(record.allowed_lot_size or 1)).numerator
                )
                log("CHOICE", f"Selected product {chosen.product_id} for {item.description}")
                lines.append(
                    QuoteLine(
                        item_id=item.item_id,
                        product_id=chosen.product_id,
                        quantity_sales_units=units,
                    )
                )
        tools.tool_step(env, request_id, "request_quote")
        quote = await tools.request_quote(
            QuoteBody(
                buyer_id=detail.buyer_id,
                agent_id=AGENT_ID,
                request_id=request_id,
                requested_date=detail.intent.deliver_by,
                lines=lines,
                budget=detail.intent.budget,
            ),
            env,
        )
        if body.reorder_of:
            detail.candidates = quote.lines
        detail.quote = quote
        env.store.save_request(detail)
        if quote.policy.outcome == "AUTO":
            tools.tool_step(env, request_id, "place_order")
            tools.place_order(
                OrderBody(
                    buyer_id=detail.buyer_id,
                    agent_id=AGENT_ID,
                    quote_id=quote.quote_id,
                    idempotency_key=f"{request_id}-{quote.quote_id}",
                ),
                env,
            )
        elif quote.policy.outcome == "NEEDS_APPROVAL":
            tools.tool_step(env, request_id, "request_approval")
            tools.request_approval(
                quote.quote_id,
                RequestApprovalBody(
                    buyer_id=detail.buyer_id,
                    agent_id=AGENT_ID,
                    explanation="Human approval is required: "
                    + "; ".join(rule.description for rule in quote.policy.rules_fired),
                ),
                env,
            )
        else:
            detail.status = "BLOCKED"
            env.store.save_request(detail)
    except Exception as exc:
        detail = env.store.get_request(request_id, detail.buyer_id) or detail
        detail.status = "FAILED"
        detail.error = (
            Error(code=exc.code, message=exc.message)
            if isinstance(exc, GatewayError)
            else Error(code="INTERNAL_ERROR", message="The request could not be completed.")
        )
        append_step(
            env.store,
            request_id,
            "gateway",
            "ERROR",
            detail.error.message,
            {"code": detail.error.code},
            None,
            env.now(),
        )
        env.store.save_request(detail)
