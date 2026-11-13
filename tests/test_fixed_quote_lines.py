"""Explicit sales quantities reuse offer evidence and batch upstream requests."""

import pytest

from gateway.catalog.index import Catalog
from gateway.offers import OfferDataError, UnknownProductError, price_fixed_lines
from gateway.policy import ItemContext, evaluate
from gateway.schemas import Quantity, QuoteLine
from tests.engine_support import AGENT, BUYER, mandate
from tests.test_offers import NOW, REQUESTED, FakeValio, price, product, schedule, stock


async def test_fixed_quantities_one_call_and_optional_item_needs() -> None:
    lines = [
        QuoteLine(item_id="it_flour", product_id="900101", quantity_sales_units=4.0),
        QuoteLine(item_id="it_other", product_id="900102", quantity_sales_units=2.0),
        QuoteLine(item_id="it_third", product_id="900101", quantity_sales_units=1.0),
    ]
    fake = FakeValio([price(), price("900102")], [stock(), stock("900102")])
    results = await price_fixed_lines(
        lines,
        buyer_id=BUYER,
        agent_id=AGENT,
        requested_date=REQUESTED,
        catalog=Catalog([product(), product("900102", pack_size={"value": 0.5, "unit": "L"})]),
        valio=fake,
        now=NOW,
        needs={"it_flour": Quantity(value=3000.0, unit="g")},
        item_context={"it_flour": ItemContext(constraints=["organic"])},
    )
    assert fake.calls == [
        ("price", ["900101", "900102"], REQUESTED),
        ("availability", ["900101", "900102"], REQUESTED),
    ]
    assert [r.quantity_sales_units for r in results] == [4, 2, 1]
    assert results[0].quantity_requested == Quantity(value=3000.0, unit="g")
    assert results[0].quantity_covered == Quantity(value=4000.0, unit="g")
    assert results[0].line_total == 13.4 and results[0].line_total_with_tax == 15.2
    assert "OVERBUY" in results[0].flags and results[0].dietary_check == {"organic": "unknown"}
    assert (
        results[1].quantity_requested
        == results[1].quantity_covered
        == Quantity(value=1.0, unit="L")
    )
    assert (
        results[2].quantity_requested
        == results[2].quantity_covered
        == Quantity(value=1.0, unit="kg")
    )


@pytest.mark.parametrize("available", [0, 2])
async def test_fixed_quantity_never_shrinks_when_unavailable(available: int) -> None:
    fake = FakeValio([price()], [stock(scheduleLines=[schedule(available)])])
    lines = await price_fixed_lines(
        [QuoteLine(item_id="it_flour", product_id="900101", quantity_sales_units=4.0)],
        buyer_id=BUYER,
        agent_id=AGENT,
        requested_date=REQUESTED,
        catalog=Catalog([product()]),
        valio=fake,
        now=NOW,
    )
    assert lines[0].quantity_sales_units == 4 and lines[0].line_total == 13.4
    assert "NOT_DELIVERABLE" in lines[0].flags
    assert evaluate(lines, {"it_flour": ItemContext()}, mandate(), AGENT).outcome == "BLOCK"


@pytest.mark.parametrize("units", [0.0, -1.0, 1.5, 3.0])
async def test_invalid_fixed_units_rejected_before_calls(units: float) -> None:
    fake = FakeValio([], [])
    with pytest.raises(ValueError, match="whole lot"):
        await price_fixed_lines(
            [QuoteLine(item_id="it_flour", product_id="900101", quantity_sales_units=units)],
            buyer_id=BUYER,
            agent_id=AGENT,
            requested_date=REQUESTED,
            catalog=Catalog([product(allowedLotSize=2.0)]),
            valio=fake,
            now=NOW,
        )
    assert fake.calls == []


async def test_empty_unknown_missing_availability_and_unknown_pack() -> None:
    fake = FakeValio([], [])
    params = {
        "buyer_id": BUYER,
        "agent_id": AGENT,
        "requested_date": REQUESTED,
        "catalog": Catalog([product(pack_size=None)]),
        "valio": fake,
        "now": NOW,
    }
    assert await price_fixed_lines([], **params) == []
    with pytest.raises(UnknownProductError):
        await price_fixed_lines(
            [QuoteLine(item_id="it", product_id="900999", quantity_sales_units=1.0)], **params
        )
    assert fake.calls == []
    line = QuoteLine(item_id="it", product_id="900101", quantity_sales_units=3.0)
    with pytest.raises(OfferDataError):
        await price_fixed_lines([line], **params)
    params["valio"] = FakeValio([price()], [stock()])
    result = (await price_fixed_lines([line], **params))[0]
    assert result.quantity_sales_units == 3
    assert "PACK_SIZE_UNKNOWN" in result.flags
