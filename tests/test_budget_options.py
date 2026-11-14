"""Budget exception planning and both runners use invented, mocked offers only."""

from pathlib import Path

import pytest

from agent.exceptions import ItemPlan, cheaper_option
from agent.llm import FakeLLM, ToolRequest
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.policy import ItemContext
from gateway.schemas import Offer, QuoteLine
from tests.agent_loop_support import EXPLANATION, purchase
from tests.engine_support import BUYER, Engine
from tests.test_e2e import poll
from tests.test_exception_gateway import environment
from tests.test_offers import REQUESTED, offer, price, product, schedule, stock

# No files are needed: customer loading treats an absent directory as empty.
CUSTOMERS_DIR = Path(__file__).with_name("invented-absent-customers")


async def priced(item_id: str, code: str, cost: float, **changes: object) -> Offer:
    original = await offer()
    return original.model_copy(
        update={
            "item_id": item_id,
            "product_id": code,
            "unit_price": cost,
            "line_total": cost,
            "line_total_with_tax": cost,
            "quantity_sales_units": 1.0,
            "quantity_requested": original.quantity_requested.model_copy(update={"value": 1.0}),
            "quantity_covered": original.quantity_covered.model_copy(update={"value": 1.0}),
            **changes,
        },
        deep=True,
    )


def plan(*offers: Offer) -> ItemPlan:
    first = offers[0]
    return ItemPlan(
        [
            QuoteLine(
                item_id=first.item_id,
                product_id=first.product_id,
                quantity_sales_units=first.quantity_sales_units,
            )
        ],
        [],
        [],
        [],
        list(offers),
    )


async def test_largest_saving_first_and_stop_as_soon_as_within_budget() -> None:
    first = [await priced("it_a", "900801", 100), await priced("it_a", "900802", 90)]
    second = [await priced("it_b", "900803", 100), await priced("it_b", "900804", 60)]
    context = {"it_a": ItemContext(), "it_b": ItemContext()}
    quote = await Engine().quote(lines=[first[0], second[0]], budget=175, item_context=context)
    result = cheaper_option(quote, [plan(*first), plan(*second)], context, REQUESTED)
    assert result is not None
    lines, label = result
    assert [line.product_id for line in lines] == ["900801", "900804"]
    assert label == "Cheaper option within budget"


async def test_uses_cheapest_usable_timely_offer_with_full_coverage() -> None:
    original = await priced("it_a", "900801", 110)
    cheapest = await priced("it_a", "900802", 90)
    not_cheapest = await priced("it_a", "900803", 95)
    unsafe = await priced("it_a", "900804", 1, dietary_check={"vegan": "violated"})
    late = await priced("it_a", "900805", 1, available_date=REQUESTED.replace(day=23))
    short = await priced(
        "it_a",
        "900806",
        1,
        quantity_covered=original.quantity_covered.model_copy(update={"value": 0.5}),
    )
    blocked = await priced("it_a", "900807", 1, flags=["PRICE_MISSING"])
    context = {"it_a": ItemContext(constraints=["vegan"])}
    quote = await Engine().quote(lines=[original], budget=100, item_context=context)
    result = cheaper_option(
        quote,
        [plan(original, not_cheapest, unsafe, late, short, blocked, cheapest)],
        context,
        REQUESTED,
    )
    assert result is not None
    assert result[0][0].product_id == "900802"
    assert result[0][0].quantity_sales_units == 1


async def test_still_over_label_and_no_saving() -> None:
    original = await priced("it_a", "900801", 110)
    cheaper = await priced("it_a", "900802", 105)
    context = {"it_a": ItemContext()}
    quote = await Engine().quote(lines=[original], budget=100, item_context=context)
    result = cheaper_option(quote, [plan(original, cheaper)], context, REQUESTED)
    assert result is not None and result[1] == "Cheaper option"
    assert cheaper_option(quote, [plan(original)], context, REQUESTED) is None


async def test_split_swaps_preserve_item_coverage_and_substitution_source() -> None:
    first = await priced("it_a", "900801", 60)
    second = await priced("it_a", "900802", 60)
    first.quantity_requested.value = second.quantity_requested.value = 1.5
    third = await priced("it_a", "900803", 10)
    fourth = await priced("it_a", "900804", 20)
    context = {"it_a": ItemContext()}
    quote = await Engine().quote(lines=[first, second], budget=25, item_context=context)
    split = plan(first, second, third, fourth)
    split.recommended.append(QuoteLine(item_id="it_a", product_id="900802", quantity_sales_units=1))
    split.sources["900803"] = "valio"
    split.lots = {"900803": 1, "900804": 1}
    result = cheaper_option(quote, [split], context, REQUESTED)
    assert result is not None
    lines, label = result
    assert [line.product_id for line in lines] == ["900803", "900804"]
    assert sum(line.quantity_sales_units for line in lines) >= 1.5
    assert lines[0].substitute_for == "900801" and lines[0].substitute_source == "valio"
    assert label == "Cheaper option"


def set_price(code: str, amount: float):
    return price(
        code,
        catalogPrice={
            "value": str(amount),
            "valueWithTax": str(amount),
            "currency": "EUR",
            "validFrom": "2026-01-01",
            "validTo": "2026-12-31",
        },
    )


def start_budget(client, budget: float = 100):
    response = client.post(
        "/requests",
        json={
            "buyer_id": BUYER,
            "requested_date": "2026-11-20",
            "budget": budget,
            "items": [{"description": "flour", "quantity": 3.0, "unit": "kg", "constraints": []}],
        },
    )
    assert response.status_code == 202, response.text
    return poll(client, response.json()["request_id"])


@pytest.mark.parametrize("gemini", [False, True])
@pytest.mark.parametrize("cheapest", [30, 34])
def test_both_runners_offer_cheaper_with_real_policy_and_labels(
    gemini: bool,
    cheapest: float,
) -> None:
    turns = purchase("900701")
    turns[1] = [
        ToolRequest(
            "check_offers",
            {
                "item_id": "it_1",
                "product_ids": ["900701", "900702"],
            },
        )
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION]) if gemini else None
    with environment(CUSTOMERS_DIR, mode="normal", llm=llm) as client:
        client.app.state.valio.prices = [set_price("900701", 35), set_price("900702", cheapest)]
        detail = start_budget(client)
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert detail.quote.total == 105
        result = client.get(f"/approvals/{detail.approval_id}").json()
        options = result["options"]
        assert len(options) == 2 and options[0]["quote_id"] == detail.quote.quote_id
        assert options[1]["total"] == cheapest * 3
        assert options[1]["option_label"] == (
            "Cheaper option within budget" if cheapest == 30 else "Cheaper option"
        )
        assert options[1]["policy"]["outcome"] == ("AUTO" if cheapest == 30 else "NEEDS_APPROVAL")
        assert any("largest savings first" in step.title for step in detail.steps)
        assert verify_chain(detail.steps)


@pytest.mark.parametrize("gemini", [False, True])
@pytest.mark.parametrize("cheapest", [30, 34, 40])
def test_hard_stop_recovers_only_when_cheaper_quote_is_not_blocked(
    gemini: bool,
    cheapest: float,
) -> None:
    turns = purchase("900701")
    turns[1] = [
        ToolRequest(
            "check_offers",
            {
                "item_id": "it_1",
                "product_ids": ["900701", "900702"],
            },
        )
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION]) if gemini else None
    with environment(CUSTOMERS_DIR, mode="normal", llm=llm) as client:
        client.app.state.valio.prices = [
            set_price("900701", 45),
            set_price("900702", cheapest),
        ]
        detail = start_budget(client)
        expected = {30: "ORDERED", 34: "WAITING_APPROVAL", 40: "BLOCKED"}[cheapest]
        assert detail.status == expected, detail.error
        if cheapest < 40:
            assert detail.quote.lines[0].product_id == "900702"
            assert detail.quote.option_label == (
                "Cheaper option within budget" if cheapest == 30 else "Cheaper option"
            )
            if cheapest == 30:
                assert detail.order.quote_id == detail.quote.quote_id
            else:
                assert detail.order is None and detail.approval_id is not None
        else:
            assert detail.order is None and detail.approval_id is None
            assert detail.quote.lines[0].product_id == "900701"


def test_cheaper_replaces_last_option_at_three_quote_limit() -> None:
    with environment(CUSTOMERS_DIR, mode="split") as client:
        client.app.state.catalog = Catalog(
            [
                product(code, names={"en": f"Invented flour {index}"})
                for index, code in enumerate(("900701", "900702", "900703"))
            ]
        )
        client.app.state.valio.prices = [
            set_price(code, amount)
            for code, amount in (("900701", 50), ("900702", 30), ("900703", 32))
        ]
        client.app.state.valio.stock = [
            stock("900701", scheduleLines=[schedule(1)]),
            stock("900702"),
            stock("900703"),
        ]
        detail = start_budget(client)
        assert detail.status == "WAITING_APPROVAL", detail.error
        options = client.get(f"/approvals/{detail.approval_id}").json()["options"]
        assert len(options) == 3
        assert options[0]["quote_id"] == detail.quote.quote_id
        assert options[0]["total"] == 110
        assert options[1]["lines"][0]["product_id"] == "900702"
        assert options[2]["option_label"] == "Cheaper option within budget"
        assert options[2]["total"] == 92
        assert len(client.app.state.store.list_quotes(detail.request_id, BUYER)) == 3
