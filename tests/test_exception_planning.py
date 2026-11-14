"""Exception choices are computed from invented, already-fetched offers."""

from datetime import date

import pytest

from agent.exceptions import option_phrase, plan_item, quote_options
from gateway.policy import ItemContext
from gateway.schemas import IntentItem, Offer
from tests.test_offers import REQUESTED, offer, price, product, request, schedule, stock


def item(**changes: object) -> IntentItem:
    return IntentItem.model_validate(
        {
            "item_id": "it_flour",
            "description": "flour",
            "description_fi": "flour",
            "quantity": 3.0,
            "unit": "kg",
            "constraints": [],
            "used_in": [],
            **changes,
        }
    )


async def candidate(code: str = "900101", **changes: object) -> Offer:
    result = await offer(
        record=product(code),
        body=request(product_ids=[code]),
        prices=[price(code)],
        availability=stock(code),
    )
    return result.model_copy(update=changes)


@pytest.mark.parametrize("flag", ["NOT_DELIVERABLE", "SALES_BLOCK", "PRICE_MISSING"])
async def test_next_candidate_precedes_substitutes(flag: str) -> None:
    first = await candidate(flags=[flag])
    second = await candidate("900102")
    substitute = await candidate("900103")
    plan = plan_item(item(), REQUESTED, [first, second], [substitute])
    assert [line.product_id for line in plan.recommended] == ["900102"]
    assert plan.recommended[0].substitute_source is None
    assert plan.decisions and not plan.alternatives


async def test_valio_precedes_agent_and_tags_original() -> None:
    first = await candidate(flags=["NOT_DELIVERABLE"])
    valio = await candidate("900102")
    agent = await candidate("900103")
    plan = plan_item(item(), REQUESTED, [first], [valio], [agent])
    assert plan.recommended[0].substitute_for == first.product_id
    assert plan.recommended[0].substitute_source == "valio"
    assert (
        option_phrase(item(), plan.recommended, plan.offers, REQUESTED)
        == "use Valio's substitute for the flour"
    )
    plan = plan_item(item(), REQUESTED, [first], [], [agent])
    assert plan.recommended[0].substitute_source == "agent"
    assert (
        option_phrase(item(), plan.recommended, plan.offers, REQUESTED)
        == "use a suggested substitute for the flour"
    )


async def test_partial_split_rounds_remainder_and_offers_whole_need() -> None:
    partial = await offer(availability=stock(scheduleLines=[schedule(1)]))
    whole = await candidate("900102")
    plan = plan_item(item(), REQUESTED, [partial, whole])
    assert [line.quantity_sales_units for line in plan.recommended] == [1, 2]
    assert [line.product_id for line in plan.alternatives[0]] == ["900102"]
    assert plan.alternatives[0][0].quantity_sales_units == 3
    options = quote_options([item()], [plan], REQUESTED)
    assert [label for _, label in options] == [
        "Split the flour across two products",
        "Use Invented pantry flour for all of the flour",
    ]
    # Lots and converted coverage, rather than the item's numeric amount, set packs.
    plan = plan_item(item(quantity=2.5), REQUESTED, [partial, whole], lots={"900102": 2})
    assert plan.recommended[1].quantity_sales_units == 2


async def test_partial_can_split_with_a_valio_substitute() -> None:
    partial = await offer(availability=stock(scheduleLines=[schedule(1)]))
    whole = await candidate("900102")
    plan = plan_item(item(), REQUESTED, [partial], [whole])
    assert plan.recommended[1].substitute_source == "valio"
    assert plan.recommended[1].substitute_for == partial.product_id
    assert plan.alternatives[0][0].substitute_source == "valio"
    assert option_phrase(item(), plan.alternatives[0], plan.offers, REQUESTED) == (
        "use Invented pantry flour for all of the flour"
    )


async def test_fractional_need_does_not_round_up_an_extra_pack() -> None:
    from gateway.schemas import Quantity

    partial = await candidate(
        quantity_sales_units=1.0, quantity_covered=Quantity(value=0.1, unit="kg")
    )
    whole = await candidate(
        "900102", quantity_sales_units=4.0, quantity_covered=Quantity(value=0.4, unit="kg")
    )
    plan = plan_item(item(quantity=0.4), REQUESTED, [partial, whole])
    assert [line.quantity_sales_units for line in plan.recommended] == [1, 3]


async def test_split_keeps_late_companion_when_no_timely_remainder_exists() -> None:
    partial = await offer(availability=stock(scheduleLines=[schedule(1)]))
    late = await candidate("900102", available_date=date(2026, 11, 23))
    plan = plan_item(item(), REQUESTED, [partial, late])
    assert [line.quantity_sales_units for line in plan.recommended] == [1, 2]
    assert plan.alternatives[0][0].product_id == "900102"
    assert any("2026-11-23" in decision for decision in plan.decisions)


@pytest.mark.parametrize("timely", [False, True])
async def test_late_uses_timely_candidate_or_keeps_earliest_date(timely: bool) -> None:
    late = await candidate(available_date=date(2026, 11, 23), flags=["LATE_DELIVERY"])
    other = await candidate("900102")
    plan = plan_item(item(), REQUESTED, [late, other] if timely else [late])
    assert plan.recommended[0].product_id == ("900102" if timely else "900101")
    if not timely:
        assert (
            option_phrase(item(), plan.recommended, plan.offers, REQUESTED)
            == "wait for Invented pantry flour, delivered Mon 23 Nov"
        )
        assert "2026-11-23" in plan.decisions[0]


async def test_timely_valio_substitute_beats_late_candidate() -> None:
    late = await candidate(available_date=date(2026, 11, 23))
    plan = plan_item(item(), REQUESTED, [late], [await candidate("900102")])
    assert plan.recommended[0].substitute_source == "valio"


async def test_required_preference_is_a_fact_and_explicit_constraint_is_hard() -> None:
    first = await candidate(dietary_check={"finnish_origin": "violated"})
    plan = plan_item(
        item(), REQUESTED, [first], context=ItemContext(required_preferences=["finnish_origin"])
    )
    assert "required finnish origin preference" in plan.facts[0]
    with pytest.raises(ValueError, match="hard filters"):
        plan_item(item(), REQUESTED, [first], context=ItemContext(constraints=["finnish_origin"]))


@pytest.mark.parametrize("source", ["candidate", "valio", "agent"])
async def test_hard_filter_applies_to_every_source(source: str) -> None:
    blocked = await candidate(flags=["NOT_DELIVERABLE"])
    unsafe = await candidate("900102", dietary_check={"vegan": "violated"})
    valid = await candidate("900103", dietary_check={"vegan": "confirmed"})
    candidates, valio, agent = [blocked], [], []
    {"candidate": candidates, "valio": valio, "agent": agent}[source].extend([unsafe, valid])
    plan = plan_item(
        item(), REQUESTED, candidates, valio, agent, context=ItemContext(constraints=["vegan"])
    )
    assert plan.recommended[0].product_id == "900103"


async def test_allergen_contains_never_becomes_a_split_or_substitute() -> None:
    first = await candidate()
    first.allergens.contains = ["milk"]
    with pytest.raises(ValueError, match="hard filters"):
        plan_item(
            item(), REQUESTED, [first], [first], context=ItemContext(exclude_allergens=["milk"])
        )


async def test_item_constraints_apply_without_an_explicit_context() -> None:
    unsafe = await candidate(dietary_check={"vegan": "violated"})
    safe = await candidate("900102", dietary_check={"vegan": "confirmed"})
    plan = plan_item(item(constraints=["vegan"]), REQUESTED, [unsafe, safe])
    assert plan.recommended[0].product_id == "900102"
    unsafe.allergens.contains = ["milk"]
    with pytest.raises(ValueError, match="hard filters"):
        plan_item(item(constraints=["no_milk"]), REQUESTED, [unsafe])


async def test_option_limit_and_other_item_lines_are_preserved() -> None:
    partial = await offer(availability=stock(scheduleLines=[schedule(1)]))
    plan = plan_item(
        item(), REQUESTED, [partial, await candidate("900102"), await candidate("900103")]
    )
    second_item = item(item_id="it_second", description="second flour")
    second = plan_item(second_item, REQUESTED, [partial, await candidate("900104")])
    options = quote_options([item(), second_item], [plan, second], REQUESTED)
    assert len(options) == 3
    for lines, label in options:
        assert [line for line in lines if line.item_id == "it_second"] == second.recommended
        assert label and label[0].isupper() and not label.endswith(".")
