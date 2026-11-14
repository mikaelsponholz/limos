"""Fetch exception shortlists through gateway tools, then let pure code choose."""

import json
from collections.abc import Awaitable, Callable, Sequence
from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from agent.exceptions import ItemPlan, plan_item, quote_options, usable
from agent.llm import LLM
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.policy import ItemContext
from gateway.schemas import IntentItem, Offer, Quote, QuoteBody, RequestDetail

type FetchOffers = Callable[[list[str]], Awaitable[list[Offer]]]
type Search = Callable[[str], list[str]]


class SubstituteQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)


async def resolve_item(
    item: IntentItem,
    offers: Sequence[Offer],
    detail: RequestDetail,
    env: Resources,
    fetch: FetchOffers,
    search: Search,
    llm: LLM | None,
) -> ItemPlan:
    if not offers:
        raise GatewayError(404, "NO_CANDIDATES", "No products match the requested item.")
    context = ItemContext(
        constraints=item.constraints,
        required_preferences=[p.key for p in detail.intent.preferences if p.level == "require"],
        exclude_allergens=detail.intent.exclude_allergens,
    )
    original = offers[0]
    substitutes: list[Offer] = []
    proposed: list[Offer] = []
    if (
        not usable(original, context)
        or original.quantity_covered.value < item.quantity
        or (original.available_date and original.available_date > detail.intent.deliver_by)
    ):
        record = env.catalog.lookup(original.product_id)
        ids = (
            list(
                dict.fromkeys(
                    rule.substitute_product
                    for rule in record.substitutions
                    if rule.product == original.product_id
                    and env.catalog.lookup(rule.substitute_product)
                )
            )[:5]
            if record
            else []
        )
        if ids:
            substitutes = await fetch(ids)
    if llm is not None and not any(usable(offer, context) for offer in [*offers, *substitutes]):
        result = llm.structured(
            json.dumps(
                {
                    "description": item.description,
                    "constraints": item.constraints,
                    "exclude_allergens": detail.intent.exclude_allergens,
                    "original_product": original.name,
                }
            ),
            SubstituteQuery,
            system=(
                "Propose one substitute product search query. "
                "Return only a query, never quantities, prices or rules."
            ),
        )
        append_step(
            env.store,
            detail.request_id,
            "agent",
            "SUBSTITUTE_PROPOSED",
            f"Searched for a suggested substitute for the {item.description}",
            {"item_id": item.item_id, "query": result.value.query},
            result.model_id,
            env.now(),
        )
        ids = [
            code
            for code in search(result.value.query)[:5]
            if code not in {offer.product_id for offer in [*offers, *substitutes]}
        ]
        if ids:
            proposed = await fetch(ids)
    lots = {
        offer.product_id: float(Fraction(str(record.allowed_lot_size or 1)).numerator)
        for offer in [*offers, *substitutes, *proposed]
        if (record := env.catalog.lookup(offer.product_id)) is not None
    }
    try:
        plan = plan_item(
            item,
            detail.intent.deliver_by,
            offers,
            substitutes,
            proposed,
            context=context,
            lots=lots,
        )
    except ValueError:
        raise GatewayError(
            404, "NO_CANDIDATES", "No product passes the required hard filters."
        ) from None
    for title in plan.decisions:
        append_step(
            env.store,
            detail.request_id,
            "agent",
            "EXCEPTION",
            title,
            {"item_id": item.item_id},
            None,
            env.now(),
        )
    known = {(offer.item_id, offer.product_id) for offer in detail.candidates}
    detail.candidates.extend(
        offer
        for offer in [*substitutes, *proposed]
        if (offer.item_id, offer.product_id) not in known
    )
    env.store.save_request(detail)
    return plan


async def create_options(
    detail: RequestDetail,
    items: Sequence[IntentItem],
    plans: Sequence[ItemPlan],
    agent_id: str,
    create: Callable[[QuoteBody], Awaitable[Quote]],
    alternative_ids: list[str],
) -> Quote:
    variants = quote_options(items, plans, detail.intent.deliver_by)
    quotes = []
    for lines, label in variants:
        quotes.append(
            await create(
                QuoteBody(
                    buyer_id=detail.buyer_id,
                    agent_id=agent_id,
                    request_id=detail.request_id,
                    requested_date=detail.intent.deliver_by,
                    lines=lines,
                    budget=detail.intent.budget,
                    option_label=label,
                )
            )
        )
    alternative_ids.extend(
        quote.quote_id for quote in quotes[1:] if quote.policy.outcome != "BLOCK"
    )
    return quotes[0]
