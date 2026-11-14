"""Optional ideas from Gemini, validated and priced by gateway code."""

import asyncio
import json
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.constraints import item_constraints
from agent.exceptions import usable
from agent.intent import helsinki_today
from gateway import customers, tools
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.policy import ItemContext
from gateway.schemas import IntentItem, OffersBody, SearchBody, Suggestion


class SuggestionIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["seasonal", "holiday", "trend"]
    title: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    replaces_item_id: str | None
    query: str = Field(min_length=1, max_length=200)


class SuggestionIdeas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestions: list[SuggestionIdea]


async def propose_suggestions(request_id: str, env: Resources) -> None:
    detail = tools.request_detail(env, request_id, env.request_buyer(request_id))
    if detail.status != "WAITING_APPROVAL" or detail.quote is None or not detail.customer_id:
        return
    customer = customers.get_customer(detail.customer_id, env.store, **env.customer_paths)
    if not customer.playful or env.llm is None:
        return
    result = await asyncio.to_thread(
        env.llm.structured,
        json.dumps(
            {
                "today": helsinki_today(env.now()).isoformat(),
                "delivery_date": detail.intent.deliver_by.isoformat(),
                "event_type": detail.intent.event.type if detail.intent.event else None,
                "items": [
                    {"item_id": item.item_id, "description": item.description}
                    for item in detail.intent.items
                ],
                "preferences": [p.model_dump() for p in customer.preferences],
            }
        ),
        SuggestionIdeas,
        system=(
            "Propose up to three optional seasonal, holiday or trend product ideas. "
            "Use only the supplied facts. Give a search query and an existing item id for "
            "a swap, or null for an addition. Never set prices, quantities, new ids or rules."
        ),
    )
    items = {item.item_id: item for item in detail.intent.items}
    occupied = {line.product_id for line in detail.quote.lines}
    suggestions: list[Suggestion] = []
    agent_id = detail.quote.agent_id
    required = [p.key for p in detail.intent.preferences if p.level == "require"]
    for idea in result.value.suggestions[:3]:
        if idea.replaces_item_id is not None and idea.replaces_item_id not in items:
            continue
        title, reason = idea.title.strip()[:160], idea.reason.strip()[:500]
        if not title or not reason:
            continue
        suggestion_id = f"sg_{request_id}_{len(suggestions) + 1}"
        item = items.get(idea.replaces_item_id) if idea.replaces_item_id else None
        constraints = item_constraints(
            detail.intent.preferences,
            detail.intent.exclude_allergens,
            item.constraints if item else (),
        )
        tools.tool_step(env, request_id, "search_products")
        products = tools.search_products(
            SearchBody(
                buyer_id=detail.buyer_id,
                agent_id=agent_id,
                query=idea.query,
                constraints=constraints,
                exclude_allergens=detail.intent.exclude_allergens,
                limit=5,
            ),
            env,
        ).products
        ids = [product.product_id for product in products if product.product_id not in occupied]
        if not ids:
            continue
        if item is None:
            # An addition starts with one sales lot of the first search match.
            record = env.catalog.lookup(ids[0])
            assert record is not None
            lot = float(Fraction(str(record.allowed_lot_size or 1)).numerator)
            pack = record.pack_size
            item = IntentItem(
                item_id=f"it_{suggestion_id}",
                description=title,
                description_fi=title,
                quantity=pack.value * lot if pack else lot,
                unit=pack.unit if pack else "pcs",
                constraints=constraints,
                used_in=[],
            )
        need = next(
            (
                line.quantity_requested
                for line in detail.quote.lines
                if line.item_id == item.item_id
            ),
            None,
        )
        tools.tool_step(env, request_id, "check_offers")
        offers = await tools.check_offers(
            OffersBody(
                buyer_id=detail.buyer_id,
                agent_id=agent_id,
                product_ids=ids,
                item_id=item.item_id,
                quantity=need.value if need else item.quantity,
                unit=need.unit if need else item.unit,
                requested_date=detail.intent.deliver_by,
                constraints=list(dict.fromkeys([*constraints, *required])),
                exclude_allergens=detail.intent.exclude_allergens,
            ),
            env,
        )
        context = ItemContext(
            constraints=constraints,
            required_preferences=required,
            exclude_allergens=detail.intent.exclude_allergens,
        )
        offer = next((offer for offer in offers if usable(offer, context)), None)
        if offer is None:
            continue
        occupied.add(offer.product_id)
        suggestions.append(
            Suggestion(
                suggestion_id=suggestion_id,
                kind=idea.kind,
                title=title,
                reason=reason,
                replaces_item_id=idea.replaces_item_id,
                offer=offer,
                status="OFFERED",
            )
        )
    with env.store.transaction():
        current = tools.request_detail(env, request_id, detail.buyer_id)
        # A human may have decided while the model or offer checks were running.
        if current.status != "WAITING_APPROVAL" or current.approval_id != detail.approval_id:
            return
        current.suggestions = suggestions
        env.store.save_request(current)
        append_step(
            env.store,
            request_id,
            "agent",
            "SUGGESTIONS_PROPOSED",
            "Proposed optional ideas with checked offers",
            {"suggestion_ids": [suggestion.suggestion_id for suggestion in suggestions]},
            result.model_id,
            env.now(),
        )
