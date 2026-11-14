"""A bounded Gemini purchasing loop using the gateway's HTTP tools."""

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent.explanation import explain
from agent.gateway_client import AGENT_ID, GatewayClient, GatewayClientError
from agent.llm import LLM, LLMOutputError, Tool
from agent.purchasing import create_options, resolve_item
from gateway import tools
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.schemas import (
    Constraint,
    IntentItem,
    Offer,
    OffersBody,
    Quote,
    QuoteBody,
    RequestDetail,
    SearchBody,
)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchArguments(Arguments):
    item_id: str
    query: str = Field(min_length=1, max_length=200)


class OffersArguments(Arguments):
    item_id: str
    product_ids: list[str] = Field(min_length=1, max_length=5)


class Selection(Arguments):
    item_id: str
    product_id: str


class QuoteArguments(Arguments):
    choices: list[Selection] = Field(min_length=1)


def run_loop(
    llm: LLM, detail: RequestDetail, env: Resources, alternative_ids: list[str] | None = None
) -> Quote:
    client = GatewayClient(env.settings, env.gateway_http_client)
    items = {item.item_id: item for item in detail.intent.items}
    searched: dict[str, set[str]] = {}
    seen: dict[str, dict[str, Offer]] = {}
    quote: Quote | None = None

    def constraints(item: IntentItem) -> list[Constraint]:
        return list(
            dict.fromkeys(
                [
                    *item.constraints,
                    *[p.key for p in detail.intent.preferences if p.level == "require"],
                ]
            )
        )

    def error(code: str) -> dict[str, JsonValue]:
        return {"error": code}

    def search_products(item_id: str, query: str) -> object:
        if item_id not in items:
            return error("UNKNOWN_ITEM")
        try:
            response = client.search_products(
                SearchBody(
                    buyer_id=detail.buyer_id,
                    agent_id=AGENT_ID,
                    query=query,
                    constraints=items[item_id].constraints,
                    exclude_allergens=detail.intent.exclude_allergens,
                    limit=5,
                )
            )
        except GatewayClientError as exc:
            return error(exc.code)
        searched.setdefault(item_id, set()).update(p.product_id for p in response.products)
        return response

    def check_offers(item_id: str, product_ids: list[str]) -> object:
        if item_id not in items:
            return error("UNKNOWN_ITEM")
        if not set(product_ids) <= searched.get(item_id, set()):
            return error("PRODUCT_NOT_SEARCHED")
        item = items[item_id]
        try:
            response = client.check_offers(
                OffersBody(
                    buyer_id=detail.buyer_id,
                    agent_id=AGENT_ID,
                    product_ids=product_ids,
                    item_id=item_id,
                    quantity=item.quantity,
                    unit=item.unit,
                    requested_date=detail.intent.deliver_by,
                    constraints=constraints(item),
                    exclude_allergens=detail.intent.exclude_allergens,
                )
            )
        except GatewayClientError as exc:
            return error(exc.code)
        seen.setdefault(item_id, {}).update((o.product_id, o) for o in response.offers)
        detail.candidates = [offer for offers in seen.values() for offer in offers.values()]
        env.store.save_request(detail)
        append_step(
            env.store,
            detail.request_id,
            "agent",
            "CANDIDATES",
            f"Checked offers for {item.description}",
            {"item_id": item_id, "product_ids": [o.product_id for o in response.offers]},
            active_model,
            env.now(),
        )
        return response

    def request_quote(choices: list[Selection]) -> object:
        nonlocal quote
        if quote is not None:
            return error("QUOTE_ALREADY_CREATED")
        if any(choice.item_id not in items for choice in choices):
            return error("UNKNOWN_ITEM")
        if {choice.item_id for choice in choices} != set(items) or len(choices) != len(items):
            return error("MISSING_OR_DUPLICATE_ITEMS")
        selected: list[Offer] = []
        for choice in choices:
            offer = seen.get(choice.item_id, {}).get(choice.product_id)
            if offer is None:
                return error("PRODUCT_NOT_OFFERED")
            if set(offer.allergens.contains) & set(detail.intent.exclude_allergens) or any(
                offer.dietary_check.get(key) == "violated"
                for key in items[choice.item_id].constraints
            ):
                return error("OFFER_EXCLUDED")
            selected.append(offer)

        async def plan_and_quote() -> Quote:
            plans = []
            ordered_items = []
            for offer in selected:
                item = items[offer.item_id]
                ordered_items.append(item)

                async def fetch(ids: list[str], item: IntentItem = item) -> list[Offer]:
                    response = client.check_offers(
                        OffersBody(
                            buyer_id=detail.buyer_id,
                            agent_id=AGENT_ID,
                            product_ids=ids,
                            item_id=item.item_id,
                            quantity=item.quantity,
                            unit=item.unit,
                            requested_date=detail.intent.deliver_by,
                            constraints=constraints(item),
                            exclude_allergens=detail.intent.exclude_allergens,
                        )
                    )
                    return response.offers

                def search(query: str, item: IntentItem = item) -> list[str]:
                    response = client.search_products(
                        SearchBody(
                            buyer_id=detail.buyer_id,
                            agent_id=AGENT_ID,
                            query=query,
                            constraints=item.constraints,
                            exclude_allergens=detail.intent.exclude_allergens,
                            limit=5,
                        )
                    )
                    return [product.product_id for product in response.products]

                ordered = [
                    offer,
                    *[
                        other
                        for other in seen[item.item_id].values()
                        if other.product_id != offer.product_id
                    ],
                ]
                plans.append(await resolve_item(item, ordered, detail, env, fetch, search, llm))

            async def create(body: QuoteBody) -> Quote:
                return client.request_quote(body)

            return await create_options(
                detail,
                ordered_items,
                plans,
                AGENT_ID,
                create,
                alternative_ids if alternative_ids is not None else [],
                env,
            )

        try:
            quote = asyncio.run(plan_and_quote())
        except GatewayClientError as exc:
            return error(exc.code)
        for line in quote.lines:
            append_step(
                env.store,
                detail.request_id,
                "agent",
                "CHOICE",
                f"Selected product {line.product_id} for {items[line.item_id].description}",
                {"item_id": line.item_id, "product_id": line.product_id},
                active_model,
                env.now(),
            )
        return quote

    available_tools = [
        Tool(
            "search_products",
            "Search up to five candidates for an item.",
            SearchArguments,
            search_products,
        ),
        Tool(
            "check_offers", "Check a searched shortlist for an item.", OffersArguments, check_offers
        ),
        Tool(
            "request_quote",
            "Choose one offered product for every item and request one quote.",
            QuoteArguments,
            request_quote,
        ),
    ]
    active_model = ""

    def on_call(name: str, model: str) -> None:
        nonlocal active_model
        active_model = model
        safe_name = name if name in {tool.name for tool in available_tools} else "unknown"
        append_step(
            env.store,
            detail.request_id,
            "agent",
            "TOOL_CALL",
            f"Called {safe_name}",
            {"tool": safe_name, "agent_id": AGENT_ID},
            model,
            env.now(),
        )

    prompt = json.dumps(
        [
            {
                "item_id": item.item_id,
                "description": item.description,
                "quantity": item.quantity,
                "unit": item.unit,
                "constraints": constraints(item),
                "exclude_allergens": detail.intent.exclude_allergens,
            }
            for item in items.values()
        ]
    )
    try:
        result = llm.run_tools(
            prompt,
            available_tools,
            system=(
                "Search once for each item and check offers for its shortlist. Search an item "
                "again only when its search returned no usable products. Choose the most relevant "
                "usable product, then request one quote covering every item. Correct tool errors "
                "and try again. "
                "Use only returned product ids. Quantities, dates and prices are supplied by code. "
                "After a successful quote, finish with a short acknowledgement."
            ),
            # Per item: a search, an offer check and room for one retry; plus the quote and
            # its repair. A six-item recipe needs this headroom (3 * n + 2 ran out at n = 6).
            max_steps=4 * len(items) + 3,
            on_call=on_call,
        )
    except LLMOutputError:
        raise GatewayError(
            422, "AGENT_FAILED", "The purchasing agent could not finish its tool calls."
        ) from None
    if quote is None:
        raise GatewayError(422, "AGENT_FAILED", "The purchasing agent did not request a quote.")
    explanation = explain(llm, quote, result.model_id)
    tools.set_agent_explanation(quote, explanation.text, env)
    if explanation.fallback_used:
        append_step(
            env.store,
            detail.request_id,
            "agent",
            "EXCEPTION",
            "Used a factual quote explanation after invalid agent output",
            {"fallback_used": True},
            explanation.model,
            env.now(),
        )
    return quote
