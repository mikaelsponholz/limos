"""Shared gateway operations used by HTTP routes and the deterministic runner."""

from typing import cast

from gateway import orders, quotes
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.customers import append_placed_order, load_customers
from gateway.errors import GatewayError
from gateway.offers import _check, build_offers, price_fixed_lines
from gateway.policy import ItemContext
from gateway.policy.engine import REQUIRE_HARD_DIETARY_KEYS
from gateway.schemas import (
    Allergens,
    Constraint,
    Intent,
    IntentItem,
    Offer,
    OffersBody,
    Order,
    OrderBody,
    Quantity,
    Quote,
    QuoteBody,
    RequestApprovalBody,
    RequestApprovalResponse,
    RequestDetail,
    RequestInput,
    SearchBody,
    SearchProduct,
    SearchResponse,
)


def search_products(body: SearchBody, env: Resources) -> SearchResponse:
    env.permit(body.buyer_id, body.agent_id)
    catalog = env.catalog
    constraints = list(body.constraints)
    constraints.extend(cast(Constraint, f"no_{code}") for code in body.exclude_allergens)
    # Filter before truncating, so blocked matches do not consume the result limit.
    records = catalog.search(
        body.query,
        exclude_allergens=body.exclude_allergens,
        exclude_sales_blocked=True,
        limit=len(catalog),
    )
    products = []
    for record in records:
        if any(_check(record, key) == "violated" for key in constraints):
            continue
        products.append(
            SearchProduct(
                product_id=record.product_id,
                name=record.names.en or record.names.fi or record.product_id,
                vendor=record.vendor or "",
                sales_unit=record.sales_unit or "",
                pack_size=Quantity(value=record.pack_size.value, unit=record.pack_size.unit)
                if record.pack_size
                else None,
                country_of_origin=record.country_of_origin or "",
                labels=list(record.labels),
                allergens=Allergens(
                    contains=list(record.allergens.contains),
                    may_contain=list(record.allergens.may_contain),
                    data_available=record.allergens.data_available,
                ),
            )
        )
    return SearchResponse(products=products[: max(0, body.limit)])


async def check_offers(body: OffersBody, env: Resources) -> list[Offer]:
    mandate = env.permit(body.buyer_id, body.agent_id)
    return await build_offers(
        body,
        catalog=env.catalog,
        valio=env.valio,
        now=env.now(),
        max_overbuy_percent=mandate.max_overbuy_percent,
    )


def request_detail(env: Resources, request_id: str, buyer_id: str) -> RequestDetail:
    detail = env.store.get_request(request_id, buyer_id)
    if detail is None:
        raise GatewayError(404, "NOT_FOUND", "Request not found.")
    detail.steps = env.store.get_steps(request_id, buyer_id)
    if detail.quote:
        detail.quote = quotes.get_quote(env.store, detail.quote.quote_id, buyer_id, env.now())
    return detail


def title(detail: RequestDetail) -> str:
    if detail.intent.based_on:
        return detail.intent.based_on.title
    return (
        detail.input.text
        or ", ".join(item.description for item in detail.intent.items)
        or "Item order"
    )


def contexts(detail: RequestDetail | None, item_ids: list[str]) -> dict[str, ItemContext]:
    if detail is None:
        return {key: ItemContext() for key in item_ids}
    items = {item.item_id: item for item in detail.intent.items}
    if any(key not in items for key in item_ids):
        raise GatewayError(422, "INVALID_ITEM", "Quote line does not match a request item.")
    return {
        key: ItemContext(
            constraints=items[key].constraints,
            required_preferences=[p.key for p in detail.intent.preferences if p.level == "require"],
            exclude_allergens=detail.intent.exclude_allergens,
        )
        for key in item_ids
    }


def quote_contexts(
    body: QuoteBody, detail: RequestDetail | None, env: Resources
) -> dict[str, ItemContext]:
    context = contexts(detail, [line.item_id for line in body.lines])
    profiles = (
        [
            customer
            for customer in load_customers(**env.customer_paths).values()
            if customer.buyer_id == body.buyer_id
        ]
        if detail is None
        else []
    )
    for key, item in context.items():
        context[key] = ItemContext(
            constraints=list(dict.fromkeys([*item.constraints, *body.constraints])),
            required_preferences=list(
                dict.fromkeys(
                    [
                        *item.required_preferences,
                        *(
                            preference.key
                            for customer in profiles
                            for preference in customer.preferences
                            if preference.level == "require"
                            and preference.key in REQUIRE_HARD_DIETARY_KEYS
                        ),
                    ]
                )
            ),
            exclude_allergens=list(
                dict.fromkeys(
                    [
                        *item.exclude_allergens,
                        *(code for customer in profiles for code in customer.exclude_allergens),
                        *body.exclude_allergens,
                    ]
                )
            ),
        )
    return context


async def request_quote(body: QuoteBody, env: Resources) -> Quote:
    mandate = env.permit(body.buyer_id, body.agent_id)
    detail = request_detail(env, body.request_id, body.buyer_id) if body.request_id else None
    if not body.lines:
        raise GatewayError(422, "INVALID_LINES", "A quote needs at least one line.")
    context = quote_contexts(body, detail, env)
    needs = (
        {
            item.item_id: Quantity(value=item.quantity, unit=item.unit)
            for item in detail.intent.items
        }
        if detail and not detail.intent.based_on
        else None
    )
    lines = await price_fixed_lines(
        body.lines,
        buyer_id=body.buyer_id,
        agent_id=body.agent_id,
        requested_date=body.requested_date,
        catalog=env.catalog,
        valio=env.valio,
        now=env.now(),
        needs=needs,
        item_context=context,
        max_overbuy_percent=mandate.max_overbuy_percent,
    )
    if detail is None:
        # Standalone tool clients still need a request for approvals and the audit.
        items = {
            line.item_id: IntentItem(
                item_id=line.item_id,
                description=line.name,
                description_fi=line.name,
                quantity=line.quantity_requested.value,
                unit=line.quantity_requested.unit,
                constraints=[],
                used_in=[],
            )
            for line in lines
        }
        detail = RequestDetail(
            request_id=env.new_id("r_"),
            buyer_id=body.buyer_id,
            customer_id=None,
            preset_id=None,
            status="RUNNING",
            created_at=env.now(),
            input=RequestInput(text=None, image_url=None),
            intent=Intent(
                event=None,
                dishes=[],
                items=list(items.values()),
                deliver_by=body.requested_date,
                preferences=[],
                exclude_allergens=[],
                budget=body.budget,
                priority="balanced",
                assumptions=[],
                based_on=None,
                filled_gaps=[],
            ),
            question=None,
            steps=[],
            candidates=lines,
            quote=None,
            suggestions=[],
            approval_id=None,
            order=None,
            error=None,
        )
        env.store.save_request(detail)
    quote = quotes.create_quote(
        lines,
        store=env.store,
        request_id=detail.request_id,
        buyer_id=body.buyer_id,
        agent_id=body.agent_id,
        requested_date=body.requested_date,
        item_context=context,
        mandate=mandate,
        now=env.now(),
        id_generator=lambda: env.new_id("q_"),
        budget=detail.intent.budget if detail else body.budget,
        option_label=body.option_label,
    )
    if detail:
        detail.quote = quote
        if quote.policy.outcome == "BLOCK":
            detail.status = "BLOCKED"
        env.store.save_request(detail)
    return quote


def set_agent_explanation(quote: Quote, explanation: str, env: Resources) -> None:
    """Store a code-validated explanation without changing the locked quote content."""
    with env.store.transaction():
        stored = env.store.get_quote(quote.quote_id, quote.buyer_id)
        if stored is None:
            raise GatewayError(404, "NOT_FOUND", "Quote not found.")
        stored.agent_explanation = explanation
        env.store.save_quote(stored)
        detail = env.store.get_request(quote.request_id, quote.buyer_id)
        if detail is not None:
            detail.quote = stored
            env.store.save_request(detail)
        quote.agent_explanation = explanation


def request_approval(
    quote_id: str, body: RequestApprovalBody, env: Resources
) -> RequestApprovalResponse:
    env.permit(body.buyer_id, body.agent_id)
    if len(set(body.alternative_quote_ids or []) | {quote_id}) > 3:
        raise GatewayError(422, "INVALID_OPTIONS", "At most three quote options are allowed.")
    approval = quotes.request_approval(
        quote_id,
        body.buyer_id,
        body.agent_id,
        body.explanation,
        body.alternative_quote_ids,
        store=env.store,
        now=env.now(),
        id_generator=lambda: env.new_id("a_"),
    )
    detail = env.store.get_request(approval.request_id, body.buyer_id)
    if detail:
        detail.status = "WAITING_APPROVAL"
        detail.approval_id = approval.approval_id
        env.store.save_request(detail)
    return RequestApprovalResponse(approval_id=approval.approval_id, status="PENDING")


def finish_order(order: Order, buyer_id: str, env: Resources) -> None:
    detail = env.store.get_request(order.request_id, buyer_id)
    if detail is None:
        return
    detail.order = order
    detail.status = "ORDERED"
    for suggestion in detail.suggestions:
        if suggestion.status == "OFFERED":
            suggestion.status = "DECLINED"
    detail.quote = env.store.get_quote(order.quote_id, buyer_id)
    if detail.customer_id:
        event = detail.intent.event
        append_placed_order(
            env.store,
            detail.customer_id,
            order,
            title(detail),
            event.type if event else "item_list",
            event.guests
            if event
            else (detail.intent.based_on.guests_now if detail.intent.based_on else None),
            env.now(),
            **env.customer_paths,
        )
    env.store.save_request(detail)


def place_order(body: OrderBody, env: Resources) -> Order:
    mandate = env.permit(body.buyer_id, body.agent_id)
    with env.store.transaction():
        order = orders.place_order(
            body.quote_id,
            body.buyer_id,
            body.agent_id,
            body.idempotency_key,
            env.now(),
            store=env.store,
            mandate=mandate,
            id_generator=lambda: env.new_id("o_"),
        )
        finish_order(order, body.buyer_id, env)
    return order


def tool_step(env: Resources, request_id: str, tool: str) -> None:
    append_step(
        env.store,
        request_id,
        "gateway",
        "TOOL_CALL",
        f"Called {tool}",
        {"tool": tool},
        None,
        env.now(),
    )
