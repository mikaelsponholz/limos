"""Pure exception planning over a fetched shortlist. No model calls or I/O."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Literal, cast

from gateway.policy import ItemContext
from gateway.policy.engine import REQUIRE_HARD_DIETARY_KEYS
from gateway.schemas import Constraint, IntentItem, Offer, Quote, QuoteLine


@dataclass
class ItemPlan:
    recommended: list[QuoteLine]
    alternatives: list[list[QuoteLine]]
    decisions: list[str]
    facts: list[str]
    offers: list[Offer]
    sources: dict[str, Literal["valio", "agent"]] = field(default_factory=dict)
    lots: Mapping[str, float] = field(default_factory=dict)
    suitable_ids: frozenset[str] | None = None
    substitute_origins: Mapping[str, str] = field(default_factory=dict)


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
    suitable_ids: Sequence[str] | None = None,
    substitute_origins: Mapping[str, str] | None = None,
) -> ItemPlan:
    """Keep preference order, prefer timely delivery, and round remaining need by lot."""
    if not offers:
        raise ValueError("An original offered product is required.")
    context = context or ItemContext(constraints=item.constraints)
    original = offers[0].product_id
    allowed = frozenset(suitable_ids) if suitable_ids is not None else None
    pool = [offer for offer in offers if allowed is None or offer.product_id in allowed]
    sources: dict[str, Literal["valio", "agent"]] = {}
    groups: list[tuple[Literal["valio", "agent"], Sequence[Offer]]] = [
        ("valio", valio_substitutes),
        ("agent", agent_substitutes),
    ]
    for source, substitutes in groups:
        for offer in substitutes:
            if allowed is not None and offer.product_id not in allowed:
                continue
            if offer.product_id not in {entry.product_id for entry in pool}:
                pool.append(offer)
                sources[offer.product_id] = source
    safe = [offer for offer in pool if usable(offer, context)]
    if suitable_ids is not None:
        rank = {code: index for index, code in enumerate(suitable_ids)}
        safe.sort(key=lambda offer: rank[offer.product_id])
    timely = [
        offer for offer in safe if offer.available_date and offer.available_date <= requested_date
    ]
    ordered = [*timely, *[offer for offer in safe if offer not in timely]]
    fallback_pool = offers if allowed is None else pool
    chosen = (timely or safe or [offer for offer in fallback_pool if hard_safe(offer, context)])[
        0:1
    ]
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
            substitute_for=(substitute_origins or {}).get(offer.product_id, original)
            if source
            else None,
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
    # Keep the proposed choice visible, but offer confirmed allergen evidence as
    # an approval option even when the risky product has enough stock.
    excluded = set(context.exclude_allergens) | {
        key.removeprefix("no_") for key in context.constraints if key.startswith("no_")
    }
    if excluded and any(
        not offer.allergens.data_available or excluded & set(offer.allergens.may_contain)
        for offer in selected
    ):
        for candidate in ordered:
            alternative = [line(candidate)]
            if (
                candidate.quantity_covered.value >= item.quantity
                and candidate.allergens.data_available
                and all(
                    candidate.dietary_check.get(cast(Constraint, f"no_{key}")) == "confirmed"
                    for key in excluded
                )
                and alternative not in alternatives
            ):
                alternatives.append(alternative)
                if len(alternatives) >= 2:
                    break
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
    return ItemPlan(
        recommended,
        alternatives,
        decisions,
        facts,
        pool,
        sources,
        lots or {},
        allowed,
        substitute_origins or {},
    )


def cheaper_option(
    quote: Quote,
    plans: Sequence[ItemPlan],
    contexts: Mapping[str, ItemContext],
    requested: date,
) -> tuple[list[QuoteLine], str] | None:
    """Replace lines by largest saving, retaining coverage, lots and timely delivery."""
    if quote.budget is None:
        return None
    by_item = {plan.recommended[0].item_id: plan for plan in plans}
    lines = [
        QuoteLine(
            item_id=line.item_id,
            product_id=line.product_id,
            quantity_sales_units=line.quantity_sales_units,
            substitute_for=line.substitute_for,
            substitute_source=line.substitute_source,
        )
        for line in quote.lines
    ]
    costs = [Decimal(str(line.line_total)) for line in quote.lines]
    coverage = [Decimal(str(line.quantity_covered.value)) for line in quote.lines]
    total = Decimal(str(quote.total))
    limit = Decimal(str(quote.budget.limit))
    pending = set(range(len(lines)))
    changed = False
    while total > limit:
        swaps: list[tuple[Decimal, int, QuoteLine, Decimal, Decimal]] = []
        for index in sorted(pending):
            old = quote.lines[index]
            plan = by_item[old.item_id]
            pool = [
                offer
                for offer in plan.offers
                if usable(offer, contexts[old.item_id])
                and (plan.suitable_ids is None or offer.product_id in plan.suitable_ids)
            ]
            timely = [o for o in pool if o.available_date and o.available_date <= requested]
            pool = timely or pool
            occupied = {
                line.product_id
                for other, line in enumerate(lines)
                if other != index and line.item_id == old.item_id
            }
            # Pack rounding may exceed the need; retain the line's contribution to it.
            other_coverage = sum(
                coverage[other]
                for other, line in enumerate(quote.lines)
                if other != index and line.item_id == old.item_id
            )
            needed = min(
                Decimal(str(old.quantity_covered.value)),
                max(Decimal(0), Decimal(str(old.quantity_requested.value)) - other_coverage),
            )
            candidates = []
            for offer in pool:
                if (
                    offer.item_id != old.item_id
                    or offer.product_id in occupied | {old.product_id}
                    or offer.quantity_covered.unit != old.quantity_covered.unit
                    or offer.quantity_covered.value <= 0
                    or offer.quantity_sales_units <= 0
                    or needed <= 0
                ):
                    continue
                per_unit = Decimal(str(offer.quantity_covered.value)) / Decimal(
                    str(offer.quantity_sales_units)
                )
                lot = Decimal(str(plan.lots.get(offer.product_id, 1)))
                units = (needed / per_unit / lot).to_integral_value(rounding=ROUND_CEILING) * lot
                if units > Decimal(str(offer.quantity_sales_units)):
                    continue
                cost = (units * Decimal(str(offer.unit_price))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                source = plan.sources.get(offer.product_id)
                replacement = QuoteLine(
                    item_id=old.item_id,
                    product_id=offer.product_id,
                    quantity_sales_units=float(units),
                    substitute_for=plan.substitute_origins.get(
                        offer.product_id, old.substitute_for or plan.offers[0].product_id
                    )
                    if source
                    else None,
                    substitute_source=source,
                )
                candidates.append((cost, offer.product_id, replacement, units * per_unit))
            if candidates:
                cost, _, replacement, covered = min(
                    candidates, key=lambda entry: (entry[0], entry[1])
                )
                saving = costs[index] - cost
                if saving > 0:
                    swaps.append((saving, index, replacement, cost, covered))
        if not swaps:
            break
        saving, index, replacement, cost, covered = min(
            swaps, key=lambda entry: (-entry[0], entry[1])
        )
        lines[index] = replacement
        costs[index] = cost
        coverage[index] = covered
        pending.remove(index)
        total -= saving
        changed = True
    if not changed:
        return None
    return lines, "Cheaper option within budget" if total <= limit else "Cheaper option"


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
        original = next(
            (offer for offer in offers if offer.product_id == line.substitute_for), None
        )
        if original is None or {"NOT_DELIVERABLE", "SALES_BLOCK", "PRICE_MISSING"} & set(
            original.flags
        ):
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
            if plan.suitable_ids is not None and any(
                line.product_id not in plan.suitable_ids for line in alternative
            ):
                continue
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
