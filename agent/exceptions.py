"""Pure exception planning over a fetched shortlist. No model calls or I/O."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from gateway.policy import ItemContext
from gateway.policy.engine import REQUIRE_HARD_DIETARY_KEYS
from gateway.schemas import IntentItem, Offer, QuoteLine


@dataclass
class ItemPlan:
    recommended: list[QuoteLine]
    alternatives: list[list[QuoteLine]]
    decisions: list[str]
    facts: list[str]
    offers: list[Offer]


def hard_safe(offer: Offer, context: ItemContext) -> bool:
    hard = set(context.constraints) | (
        set(context.required_preferences) & REQUIRE_HARD_DIETARY_KEYS
    )
    excluded = set(context.exclude_allergens) | {
        key.removeprefix("no_") for key in hard if key.startswith("no_")
    }
    return not (
        set(offer.allergens.contains) & excluded
        or any(offer.dietary_check.get(key) == "violated" for key in hard)
    )


def usable(offer: Offer, context: ItemContext) -> bool:
    return hard_safe(offer, context) and not (
        {"NOT_DELIVERABLE", "SALES_BLOCK", "PRICE_MISSING"} & set(offer.flags)
    )


def plan_item(
    item: IntentItem,
    requested_date: date,
    offers: Sequence[Offer],
    valio_substitutes: Sequence[Offer] = (),
    agent_substitutes: Sequence[Offer] = (),
    *,
    context: ItemContext | None = None,
    lots: Mapping[str, float] | None = None,
) -> ItemPlan:
    """Keep preference order, prefer timely delivery, and round remaining need by lot."""
    if not offers:
        raise ValueError("An original offered product is required.")
    context = context or ItemContext(constraints=item.constraints)
    original = offers[0].product_id
    pool = list(offers)
    sources: dict[str, Literal["valio", "agent"]] = {}
    groups: list[tuple[Literal["valio", "agent"], Sequence[Offer]]] = [
        ("valio", valio_substitutes),
        ("agent", agent_substitutes),
    ]
    for source, substitutes in groups:
        for offer in substitutes:
            if offer.product_id not in {entry.product_id for entry in pool}:
                pool.append(offer)
                sources[offer.product_id] = source
    safe = [offer for offer in pool if usable(offer, context)]
    timely = [
        offer for offer in safe if offer.available_date and offer.available_date <= requested_date
    ]
    ordered = [*timely, *[offer for offer in safe if offer not in timely]]
    chosen = (timely or safe or [offer for offer in offers if hard_safe(offer, context)])[0:1]
    if not chosen:
        raise ValueError("No product passes the hard filters.")
    first = chosen[0]

    def line(offer: Offer, remaining: Decimal | None = None) -> QuoteLine:
        lot = Decimal(str((lots or {}).get(offer.product_id, 1)))
        units = Decimal(str(offer.quantity_sales_units)) or lot
        if remaining is not None and offer.quantity_covered.value > 0:
            per_unit = Decimal(str(offer.quantity_covered.value)) / Decimal(
                str(offer.quantity_sales_units)
            )
            wanted = (remaining / per_unit / lot).to_integral_value(rounding=ROUND_CEILING) * lot
            units = min(units, wanted)
        source = sources.get(offer.product_id)
        return QuoteLine(
            item_id=item.item_id,
            product_id=offer.product_id,
            quantity_sales_units=float(units),
            substitute_for=original if source else None,
            substitute_source=source,
        )

    recommended = [line(first)]
    alternatives = []
    decisions = []
    if not usable(first, context):
        decisions.append(f"No usable replacement for the {item.description}")
    if first.product_id != original:
        decisions.append(f"Replaced unavailable or late {offers[0].name} with {first.name}")
    if usable(first, context) and 0 < first.quantity_covered.value < item.quantity:
        rest = Decimal(str(item.quantity)) - Decimal(str(first.quantity_covered.value))
        companions = [
            offer
            for offer in ordered
            if offer.product_id != first.product_id and offer.quantity_covered.value > 0
        ]
        if companions:
            # A second product that covers the rest wins over a second shortage.
            second = next(
                (
                    offer
                    for offer in companions
                    if Decimal(str(offer.quantity_covered.value)) >= rest
                ),
                companions[0],
            )
            recommended.append(line(second, rest))
            decisions.append(f"Split the {item.description} across two products")
        else:
            decisions.append(f"Only part of the {item.description} is available")
        for offer in ordered:
            if (
                offer.product_id != first.product_id
                and offer.quantity_covered.value >= item.quantity
            ):
                alternatives.append([line(offer)])
                if len(alternatives) == 2:
                    break
    selected = [
        offer for offer in pool if offer.product_id in {line.product_id for line in recommended}
    ]
    for offer in selected:
        if offer.available_date and offer.available_date > requested_date:
            decisions.append(f"Wait for {offer.name}, delivered {offer.available_date.isoformat()}")
    facts = [
        f"{offer.name} does not confirm the required {key.replace('_', ' ')} preference."
        for offer in selected
        for key in context.required_preferences
        if key not in context.constraints
        and key not in REQUIRE_HARD_DIETARY_KEYS
        and offer.dietary_check.get(key) != "confirmed"
    ]
    decisions.extend(facts)
    return ItemPlan(recommended, alternatives, decisions, facts, pool)


def option_phrase(
    item: IntentItem, lines: Sequence[QuoteLine], offers: Sequence[Offer], requested: date
) -> str:
    if len(lines) == 2:
        return f"split the {item.description} across two products"
    line = lines[0]
    offer = next(offer for offer in offers if offer.product_id == line.product_id)
    if offer.available_date and offer.available_date > requested:
        day = offer.available_date
        weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        months = (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
        formatted = f"{weekdays[day.weekday()]} {day.day} {months[day.month - 1]}"
        return f"wait for {offer.name}, delivered {formatted}"
    if line.substitute_source == "valio":
        original = next(offer for offer in offers if offer.product_id == line.substitute_for)
        if {"NOT_DELIVERABLE", "SALES_BLOCK", "PRICE_MISSING"} & set(original.flags):
            return f"use Valio's substitute for the {item.description}"
    if line.substitute_source == "agent":
        return f"use a suggested substitute for the {item.description}"
    return f"use {offer.name} for all of the {item.description}"


def quote_options(
    items: Sequence[IntentItem], plans: Sequence[ItemPlan], requested: date
) -> list[tuple[list[QuoteLine], str | None]]:
    """Change one item per alternative; preserve every other recommended line."""
    variants = [[plan.recommended for plan in plans]]
    changed: set[int] = set()
    for index, plan in enumerate(plans):
        for alternative in plan.alternatives:
            if len(variants) == 3:
                break
            variant = [entry.recommended for entry in plans]
            variant[index] = alternative
            variants.append(variant)
            changed.add(index)
    result = []
    for variant in variants:
        phrases = [
            option_phrase(items[index], variant[index], plans[index].offers, requested)
            for index in sorted(changed)
        ]
        label = ", ".join(phrases)
        result.append(
            (
                [line for lines in variant for line in lines],
                label[:1].upper() + label[1:] if label else None,
            )
        )
    return result
