"""Code orchestrates shortlists, exception planning and quotes; Gemini proposes ids."""

import asyncio
import json
from collections.abc import Sequence
from fractions import Fraction
from itertools import zip_longest
from typing import Literal

from agent.constraints import item_constraints
from agent.errors import error_code
from agent.exceptions import ItemPlan, usable
from agent.explanation import explain
from agent.llm import LLM
from agent.purchasing import SubstituteQuery, create_options, resolve_item
from agent.relevance import Pick, judge, validated
from gateway import customers, tools
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.catalog.index import tokenize
from gateway.errors import GatewayError
from gateway.offers import OfferSnapshot, _pack
from gateway.schemas import (
    Error,
    IntentItem,
    Offer,
    OffersBody,
    Quantity,
    Quote,
    QuoteBody,
    QuoteLine,
    RequestCreate,
    RequestDetail,
    SearchBody,
)

AGENT_ID = "limos-runner"


def keep_provenance(
    plan: ItemPlan,
    valio_origins: dict[str, str],
    agent_ids: set[str],
    searched_original: str | None,
) -> None:
    """Mark every substitute line from the pipeline's own records, whatever path planned it.

    A substitute can reach the plan as its base product (for example when its original
    was skipped for incomplete data). Without provenance it would skip SUBSTITUTION.
    """

    def source_of(product_id: str) -> tuple[Literal["valio", "agent"], str | None] | None:
        if product_id in valio_origins:
            return "valio", valio_origins[product_id]
        if product_id in agent_ids:
            return "agent", searched_original
        return None

    def mark(line: QuoteLine) -> QuoteLine:
        found = source_of(line.product_id)
        if found is None or line.substitute_source is not None:
            return line
        source, origin = found
        return line.model_copy(
            update={"substitute_source": source, "substitute_for": line.substitute_for or origin}
        )

    for offer_id in {offer.product_id for offer in plan.offers}:
        found = source_of(offer_id)
        if found is not None:
            plan.sources.setdefault(offer_id, found[0])
    plan.recommended = [mark(line) for line in plan.recommended]
    plan.alternatives = [[mark(line) for line in option] for option in plan.alternatives]


async def run_pipeline(
    llm: LLM,
    detail: RequestDetail,
    body: RequestCreate,
    env: Resources,
    search_terms: dict[str, list[str]],
    alternative_ids: list[str],
) -> Quote | None:
    env.permit(detail.buyer_id, AGENT_ID)
    snapshot = OfferSnapshot(env.valio)
    past_ids: dict[str, str] = {}

    def exception(code: str, item_id: str | None = None, product_id: str | None = None) -> None:
        item = next((i for i in detail.intent.items if i.item_id == item_id), None)
        record = env.catalog.lookup(product_id) if product_id else None
        append_step(
            env.store,
            detail.request_id,
            "agent",
            "EXCEPTION",
            f"{code}: {item.description if item else 'Purchasing'}"
            + (f" | product {product_id}" if product_id else ""),
            {
                "code": code,
                "item_id": item_id,
                "product_id": product_id,
                "product_name": (record.names.en or record.names.fi) if record else None,
            },
            None,
            env.now(),
        )

    if body.reorder_of:
        if detail.customer_id is None:
            raise GatewayError(422, "CUSTOMER_REQUIRED", "A reorder needs a customer.")
        customer = customers.get_customer(detail.customer_id, env.store, **env.customer_paths)
        based_on, lines = customers.resolve_reorder(
            customer, body.reorder_of, body.event.guests if body.event else None
        )
        detail.intent.based_on = based_on
        detail.intent.items = []
        for index, line in enumerate(lines, 1):
            record = env.catalog.lookup(line.product_id)
            pack = (
                next(
                    (pack for unit in ("kg", "L", "pcs") if (pack := _pack(record, unit))),
                    None,
                )
                if record
                else None
            )
            # History stores sales units. Preserve physical contents before re-searching.
            lot = float(Fraction(str(record.allowed_lot_size or 1)).numerator) if record else 1
            units = -(-line.quantity_sales_units // lot) * lot
            item = IntentItem(
                item_id=f"it_{index}",
                description=line.name,
                description_fi=line.name,
                quantity=units * pack.value if pack else units,
                unit=pack.unit if pack else "pcs",
                constraints=item_constraints(
                    detail.intent.preferences, detail.intent.exclude_allergens
                ),
                used_in=[],
            )
            detail.intent.items.append(item)
            past_ids[item.item_id] = line.product_id
            search_terms[item.item_id] = [line.name]
    if not detail.intent.items:
        raise GatewayError(422, "NO_EVENT_ITEMS", "No purchase items were resolved.")
    env.store.save_request(detail)

    def search(item: IntentItem, terms: Sequence[str]) -> list[str]:
        groups: list[list[str]] = []
        for term in list(dict.fromkeys(terms))[:2]:
            try:
                products = tools.search_products(
                    SearchBody(
                        buyer_id=detail.buyer_id,
                        agent_id=AGENT_ID,
                        query=term,
                        constraints=item.constraints,
                        exclude_allergens=detail.intent.exclude_allergens,
                        limit=8,
                    ),
                    env,
                ).products
            except Exception as exc:
                exception(error_code(exc), item.item_id)
                raise
            groups.append([product.product_id for product in products])
        return list(
            dict.fromkeys(code for row in zip_longest(*groups) for code in row if code is not None)
        )[:8]

    def offer_body(item: IntentItem, ids: list[str]) -> OffersBody:
        return OffersBody(
            buyer_id=detail.buyer_id,
            agent_id=AGENT_ID,
            product_ids=ids,
            item_id=item.item_id,
            quantity=item.quantity,
            unit=item.unit,
            requested_date=detail.intent.deliver_by,
            constraints=list(
                dict.fromkeys(
                    [
                        *item.constraints,
                        *[p.key for p in detail.intent.preferences if p.level == "require"],
                    ]
                )
            ),
            exclude_allergens=detail.intent.exclude_allergens,
        )

    async def fetch_batch(bodies: list[OffersBody]) -> dict[str, list[Offer]]:
        tools.tool_step(env, detail.request_id, "check_offers")
        try:
            return await tools.check_offers_batch(
                bodies,
                env,
                snapshot,
                lambda item_id, product_id, code: exception(code, item_id, product_id),
            )
        except Exception as exc:
            for entry in bodies:
                for code in entry.product_ids:
                    exception(error_code(exc), entry.item_id, code)
            raise

    tools.tool_step(env, detail.request_id, "search_products")
    shortlists = {}
    for item in detail.intent.items:
        past = past_ids.get(item.item_id)
        if past and env.catalog.lookup(past):
            shortlists[item.item_id] = [past]
        else:
            if past:
                exception("PRODUCT_NOT_FOUND", item.item_id, past)
            shortlists[item.item_id] = search(
                item, search_terms.get(item.item_id, [item.description, item.description_fi])
            )
    base_ids = {key: list(ids) for key, ids in shortlists.items()}
    valio_ids: dict[str, set[str]] = {}
    valio_origins: dict[str, dict[str, str]] = {}

    def include_valio(item_id: str) -> None:
        substitutes = list(
            dict.fromkeys(
                rule.substitute_product
                for code in base_ids[item_id]
                if (record := env.catalog.lookup(code))
                for rule in record.substitutions
                if rule.product == code and env.catalog.lookup(rule.substitute_product)
            )
        )
        valio_ids[item_id] = set(substitutes)
        valio_origins[item_id] = {
            rule.substitute_product: code
            for code in reversed(base_ids[item_id])
            if (record := env.catalog.lookup(code))
            for rule in record.substitutions
            if rule.product == code and rule.substitute_product in valio_ids[item_id]
        }
        shortlists[item_id] = list(dict.fromkeys([*base_ids[item_id], *substitutes]))

    for item in detail.intent.items:
        include_valio(item.item_id)
    offers = await fetch_batch(
        [offer_body(item, shortlists[item.item_id]) for item in detail.intent.items]
    )
    contexts = tools.contexts(detail, [i.item_id for i in detail.intent.items])
    retry = []
    for item in detail.intent.items:
        past = past_ids.get(item.item_id)
        if (
            past
            and base_ids[item.item_id] == [past]
            and not any(usable(offer, contexts[item.item_id]) for offer in offers[item.item_id])
        ):
            exception("REORDER_RESEARCH", item.item_id, past)
            tools.tool_step(env, detail.request_id, "search_products")
            ids = search(item, [item.description])
            base_ids[item.item_id] = list(dict.fromkeys([past, *ids]))
            include_valio(item.item_id)
            retry.append(offer_body(item, shortlists[item.item_id]))
    if retry:
        offers.update(await fetch_batch(retry))
    detail.candidates = [offer for group in offers.values() for offer in group]
    env.store.save_request(detail)
    if not detail.candidates:
        for item in detail.intent.items:
            exception("NOT_SUITABLE", item.item_id)
            exception("NO_USABLE_OFFER", item.item_id)
        detail.status = "BLOCKED"
        detail.error = Error(
            code="NO_USABLE_PRODUCTS",
            message="No usable products for: "
            + ", ".join(i.description for i in detail.intent.items),
        )
        env.store.save_request(detail)
        return None
    try:
        picked = await judge(llm, detail.intent.items, offers, detail.intent.preferences, valio_ids)
    except Exception as exc:
        exception(error_code(exc) if error_code(exc) != "INTERNAL_ERROR" else "LLM_ERROR")
        raise
    if picked.repaired:
        exception("LLM_OUTPUT_ERROR")
    choices: dict[str, list[Pick]] = {}

    def collect(entries: Sequence[Pick], expected: set[str]) -> None:
        for choice in entries:
            if choice.item_id not in expected:
                exception("UNKNOWN_ITEM", choice.item_id, choice.product_id)
                continue
            choices.setdefault(choice.item_id, []).append(choice)

    collect(picked.value.choices, set(offers))
    missing_items = [item for item in detail.intent.items if item.item_id not in choices]
    if missing_items:
        try:
            repaired = await judge(llm, missing_items, offers, detail.intent.preferences, valio_ids)
            collect(repaired.value.choices, {item.item_id for item in missing_items})
            if repaired.repaired:
                exception("LLM_OUTPUT_ERROR")
        except Exception:
            exception("LLM_ERROR")

    selected: dict[str, str] = {}
    suitable: dict[str, list[str]] = {}
    for item in detail.intent.items:
        entries = choices.get(item.item_id, [])
        choice = validated(entries, offers[item.item_id])
        if choice is not None:
            # An explicit null is final, even if suitable_ids were also supplied.
            if choice.product_id is None:
                continue
            selected[item.item_id] = choice.product_id
            suitable[item.item_id] = list(dict.fromkeys([choice.product_id, *choice.suitable_ids]))
            continue
        exception(
            "MISSING_PICK" if not entries else "INVALID_PICK",
            item.item_id,
            entries[0].product_id if entries else None,
        )
        terms = search_terms.get(item.item_id, [item.description, item.description_fi])
        for offer in offers[item.item_id]:
            record = env.catalog.lookup(offer.product_id)
            if not record or not usable(offer, contexts[item.item_id]):
                continue
            names = [tokenize(name) for name in (record.names.en, record.names.fi) if name]
            if any(
                tokenize(term) and any(tokenize(term) <= name for name in names) for term in terms
            ):
                selected[item.item_id] = offer.product_id
                suitable[item.item_id] = [offer.product_id]
                exception("FALLBACK_PICK", item.item_id, offer.product_id)
                break

    # Search proposals are gathered first, then judged in one request-wide call.
    agent_offers: dict[str, list[Offer]] = {}
    substitute_bodies = []
    for item in detail.intent.items:
        if item.item_id not in selected or any(
            offer.product_id in suitable[item.item_id] and usable(offer, contexts[item.item_id])
            for offer in offers[item.item_id]
        ):
            continue
        result = await asyncio.to_thread(
            llm.structured,
            json.dumps(
                {
                    "description": item.description,
                    "constraints": item.constraints,
                    "exclude_allergens": detail.intent.exclude_allergens,
                }
            ),
            SubstituteQuery,
            system="Propose one substitute product search query. Return only a query.",
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
            for code in search(item, [result.value.query])
            if code not in shortlists[item.item_id]
        ]
        if ids:
            substitute_bodies.append(offer_body(item, ids))
    if substitute_bodies:
        proposed = await fetch_batch(substitute_bodies)
        relevant_items = [item for item in detail.intent.items if item.item_id in proposed]
        judged = await judge(llm, relevant_items, proposed, detail.intent.preferences, {})
        for item in relevant_items:
            choice = validated(
                [entry for entry in judged.value.choices if entry.item_id == item.item_id],
                proposed[item.item_id],
            )
            if choice is None or choice.product_id is None:
                continue
            allowed = list(dict.fromkeys([choice.product_id, *choice.suitable_ids]))
            agent_offers[item.item_id] = [
                offer for offer in proposed[item.item_id] if offer.product_id in allowed
            ]
            suitable[item.item_id].extend(allowed)
            detail.candidates.extend(agent_offers[item.item_id])
            shortlists[item.item_id].extend(allowed)
        env.store.save_request(detail)

    async def move_default(chosen: Sequence[Offer]) -> None:
        gap = next(
            (
                g
                for g in detail.intent.filled_gaps
                if g.field == "deliver_by" and g.source == "default"
            ),
            None,
        )
        if gap is None:
            return
        day = max(
            [detail.intent.deliver_by, *[o.available_date for o in chosen if o.available_date]]
        )
        previous = gap.note
        gap.value = day.isoformat()
        gap.note = f"Delivery {day.isoformat()}, the earliest date Valio offers for every item"
        detail.intent.assumptions = [note for note in detail.intent.assumptions if note != previous]
        if gap.note not in detail.intent.assumptions:
            detail.intent.assumptions.append(gap.note)
        changed = day != detail.intent.deliver_by
        detail.intent.deliver_by = day
        env.store.save_request(detail)
        if changed:
            offers.update(
                await fetch_batch(
                    [offer_body(item, shortlists[item.item_id]) for item in detail.intent.items]
                )
            )
            for item_id, proposed_offers in agent_offers.items():
                proposed_ids = {offer.product_id for offer in proposed_offers}
                agent_offers[item_id] = [
                    offer for offer in offers[item_id] if offer.product_id in proposed_ids
                ]

    await move_default(
        [
            offer
            for key, group in offers.items()
            for offer in group
            if offer.product_id == selected.get(key)
        ]
    )
    plans: list[ItemPlan] = []
    planned_items = []
    missing = []
    for item in detail.intent.items:
        group = offers[item.item_id]
        first = selected.get(item.item_id)
        substitute_ids = valio_ids[item.item_id] | {
            offer.product_id for offer in agent_offers.get(item.item_id, [])
        }
        originals = [offer for offer in group if offer.product_id not in substitute_ids]
        rank = {code: index for index, code in enumerate(suitable.get(item.item_id, []))}
        ordered = sorted(originals, key=lambda offer: rank.get(offer.product_id, len(rank)))
        if first in valio_origins[item.item_id]:
            parent = valio_origins[item.item_id][first]
            ordered.sort(key=lambda offer: offer.product_id != parent)
        replacements = [offer for offer in group if offer.product_id in valio_ids[item.item_id]]
        replacements.sort(key=lambda offer: rank.get(offer.product_id, len(rank)))
        # Keep a base product for deterministic substitution provenance.
        if not ordered and replacements:
            ordered, replacements = replacements[:1], replacements[1:]

        async def fetch(ids: list[str], item: IntentItem = item) -> list[Offer]:
            result = await fetch_batch([offer_body(item, ids)])
            return result[item.item_id]

        def search_substitute(query: str, item: IntentItem = item) -> list[str]:
            tools.tool_step(env, detail.request_id, "search_products")
            return search(item, [query])

        plan = None
        if ordered and first:
            try:
                plan = await resolve_item(
                    item,
                    ordered,
                    detail,
                    env,
                    fetch,
                    search_substitute,
                    None,
                    suitable_ids=suitable[item.item_id],
                    valio_substitutes=replacements,
                    agent_substitutes=agent_offers.get(item.item_id, []),
                    substitute_origins=valio_origins[item.item_id],
                )
            except Exception as exc:
                exception(
                    error_code(exc) if error_code(exc) != "INTERNAL_ERROR" else "LLM_ERROR",
                    item.item_id,
                )
                no_original = not any(usable(offer, contexts[item.item_id]) for offer in ordered)
                if not no_original and (
                    not isinstance(exc, GatewayError) or exc.code != "NO_CANDIDATES"
                ):
                    raise
        if plan:
            keep_provenance(
                plan,
                valio_origins[item.item_id],
                {offer.product_id for offer in agent_offers.get(item.item_id, [])},
                shortlists[item.item_id][0] if shortlists[item.item_id] else None,
            )
        if plan and any(
            usable(offer, contexts[item.item_id])
            for offer in plan.offers
            if offer.product_id in {line.product_id for line in plan.recommended}
        ):
            plans.append(plan)
            planned_items.append(item)
        else:
            exception("NOT_SUITABLE", item.item_id)
            exception("NO_USABLE_OFFER", item.item_id)
            missing.append(item.description)
    if not plans:
        detail.status = "BLOCKED"
        detail.error = Error(
            code="NO_USABLE_PRODUCTS", message="No usable products for: " + ", ".join(missing)
        )
        env.store.save_request(detail)
        return None
    await move_default(
        [
            offer
            for plan in plans
            for offer in plan.offers
            if offer.product_id in {line.product_id for line in plan.recommended}
        ]
    )
    needs = {
        item.item_id: Quantity(value=item.quantity, unit=item.unit) for item in detail.intent.items
    }

    async def create(quote_body: QuoteBody) -> Quote:
        tools.tool_step(env, detail.request_id, "request_quote")
        try:
            return await tools.request_quote(
                quote_body,
                env,
                valio=snapshot,
                item_needs=needs,
                on_skip=lambda item_id, product_id, code: exception(code, item_id, product_id),
            )
        except Exception as exc:
            for line in quote_body.lines:
                exception(error_code(exc), line.item_id, line.product_id)
            raise

    quote = await create_options(
        detail, planned_items, plans, AGENT_ID, create, alternative_ids, env
    )
    for quoted_line in quote.lines:
        append_step(
            env.store,
            detail.request_id,
            "agent",
            "CHOICE",
            f"Selected {quoted_line.name}",
            {"item_id": quoted_line.item_id, "product_id": quoted_line.product_id},
            picked.model_id,
            env.now(),
        )
    try:
        explanation = await asyncio.to_thread(explain, llm, quote, picked.model_id)
        text = explanation.text
        if explanation.fallback_used:
            exception("LLM_OUTPUT_ERROR")
    except Exception:
        exception("LLM_ERROR")
        text = (
            " ".join(rule.description for rule in quote.policy.rules_fired) or "The quote is ready."
        )
    for description in missing:
        text += f" No suitable product found for the {description}; a person needs to choose one."
    tools.set_agent_explanation(quote, text, env)
    return quote
