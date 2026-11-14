"""Price insights inform the buyer from Valio prices, the quote and their own history."""

import json
from datetime import date

from agent.pricing import _season, history_insights, line_insights
from gateway.schemas import ComparisonPrice, Customer, Event, Quantity
from tests.engine_support import BUYER
from tests.test_offers import FakeValio, price, product, stock
from tests.test_pipeline import EXPLANATION, environment, item, picks, start

FLOUR = product("990001", names={"en": "Invented pantry flour"})

CUSTOMER = {
    "customer_id": "invented-frank",
    "name": "Invented Frank",
    "company": "Invented Oy",
    "role": "Owner",
    "buyer_id": BUYER,
    "age_group": "55_plus",
    "playful": False,
    "preferences": [],
    "exclude_allergens": [],
    "defaults": {"priority": "balanced", "typical_guests": None, "budget": None},
    "order_history": [
        {
            "order_ref": "ph_invented",
            "date": "2026-06-01",
            "title": "Invented summer wedding",
            "event_type": "wedding",
            "guests": 20,
            "lines": [
                {
                    "product_id": "990001",
                    "name": "Invented pantry flour",
                    "quantity_sales_units": 4,
                    "sales_unit": "PRK",
                    "quantity_text": "4 PRK",
                    "unit_price": 3.0,
                    "line_total": 12.0,
                }
            ],
            "total": 12.0,
            "source": "limos",
            "simulated": True,
        }
    ],
    "simulated": True,
}


class DatedValio(FakeValio):
    """Flour costs 2.35 before the delivery date and 3.35 on it."""

    async def price(self, product_codes, requested_date):
        response = await super().price(product_codes, requested_date)
        if requested_date < date(2026, 11, 20):
            cheap = {
                "value": "2.35",
                "valueWithTax": "2.67",
                "validFrom": "2026-11-01",
                "validTo": "2026-11-19",
                "currency": "EUR",
            }
            response = response.model_copy(
                update={"products": [price("990001", catalogPrice=cheap)]}
            )
        return response


def run(tmp_path, records, description, quantity, valio=None):
    outputs = [picks(records[0].product_id), EXPLANATION]
    with environment(tmp_path, records, outputs, approval=True, insights=True) as client:
        if valio is not None:
            client.app.state.valio = valio
        return start(client, items=[item(description, quantity)])


def kinds(detail):
    return {insight.kind: insight for insight in detail.insights}


def test_earlier_delivery_price_change_sale_and_waste(tmp_path):
    valio = DatedValio([price("990001")], [stock("990001")])
    detail = run(tmp_path, [FLOUR], "flour", 1.2, valio)
    assert detail.status == "WAITING_APPROVAL"
    found = kinds(detail)
    day = found["delivery_date"]
    assert day.saving == 2.0 and "2026-11-19" in day.text and "6.70 €" in day.text
    assert any("rises by 43%" in i.text for i in detail.insights)
    # The usual gap to list price is the buyer's agreement, not a sale; small leftovers keep.
    assert found["discount"].text == (
        "Your customer prices are 0.70 € below Valio's list prices on this order (usually 9% off)."
    )
    assert "waste" not in found
    # Most useful first: the chat shows the first ones as tips.
    assert [i.kind for i in detail.insights][:2] == ["delivery_date", "price_change"]
    assert detail.insights[-1].text.startswith("Your customer prices")
    # Insights inform only: the quote is unchanged and still needs the approval.
    assert detail.quote.total == 6.7 and detail.approval_id


def test_out_of_season_berries_get_a_frozen_hint(tmp_path):
    berries = product("990001", names={"en": "Invented strawberries"})
    detail = run(tmp_path, [berries], "strawberries", 1)
    season = kinds(detail)["season"]
    assert "out of season in November" in season.text and "Frozen strawberries" in season.text


def test_season_skips_preserved_products_and_knows_finnish_names():
    assert _season("pakastettu mansikka") is None
    assert _season("mansikkahillo") is None
    assert _season("tuore mustikka")[0] == "blueberries"
    assert _season("porkkana") is None


def test_insight_failure_keeps_the_quote_and_approval(tmp_path):
    class Broken(FakeValio):
        async def price(self, product_codes, requested_date):
            if requested_date < date(2026, 11, 20):
                raise RuntimeError("invented outage")
            return await super().price(product_codes, requested_date)

    detail = run(tmp_path, [FLOUR], "flour", 2, Broken([price("990001")], [stock("990001")]))
    assert detail.status == "WAITING_APPROVAL" and detail.insights == []
    step = next(s for s in detail.steps if s.title == "Skipped optional price insights")
    assert step.detail["exception_type"] == "RuntimeError"


def test_costly_leftover_and_a_deeper_sale_are_named(tmp_path):
    detail = run(tmp_path, [FLOUR], "flour", 2)
    line = detail.quote.lines[0]
    big = line.model_copy(
        update={
            "quantity_requested": Quantity(value=1.2, unit="kg"),
            "quantity_covered": Quantity(value=5, unit="kg"),
            "line_total": 20.0,
        }
    )
    sale = line.model_copy(
        update={
            "product_id": "990002",
            "item_id": "it_2",
            "name": "Invented sugar",
            "unit_price": 2.0,
            "list_price": 4.0,
        }
    )
    quote = detail.quote.model_copy(update={"lines": [big, line, sale]})
    found = [i for i in line_insights(quote, []) if i.kind in {"waste", "discount"}]
    assert found[0].kind == "waste" and "3.8 kg (about 15.20 €)" in found[0].text
    assert any("Invented sugar is on sale: 50% off" in i.text for i in found)
    assert not any(i.text.startswith("Invented pantry flour is on sale") for i in found)


def test_bigger_pack_of_the_same_product_line(tmp_path):
    detail = run(tmp_path, [FLOUR], "flour", 2)
    line = detail.quote.lines[0]
    bigger = line.model_copy(
        update={
            "product_id": "990009",
            "name": "Invented pantry flour 5 kg",
            "comparison_price": ComparisonPrice(value=1.0, unit="KG"),
            "quantity_covered": Quantity(value=5, unit="kg"),
            "line_total": 5.0,
        }
    )
    other_brand = bigger.model_copy(update={"product_id": "990010", "name": "Other flour"})
    found = {i.kind: i for i in line_insights(detail.quote, [bigger, other_brand])}
    assert found["pack_size"].product_id == "990009" and found["pack_size"].saving == 1.7


def test_cost_per_guest_and_price_trend_from_history(tmp_path):
    detail = run(tmp_path, [FLOUR], "flour", 2)
    detail.intent.event = Event(type="wedding", date=date(2026, 11, 21), guests=10)
    customer = Customer.model_validate_json(json.dumps(CUSTOMER))
    found = {i.kind: i for i in history_insights(detail, customer)}
    assert found["per_guest"].text == (
        "0.67 € per guest for 10 guests. Your Invented summer wedding on 2026-06-01 cost "
        "0.60 € per guest."
    )
    assert "12% dearer than in your order on 2026-06-01" in found["price_trend"].text


def test_seeded_history_with_invented_prices_is_not_compared(tmp_path):
    detail = run(tmp_path, [FLOUR], "flour", 2)
    customer = Customer.model_validate_json(json.dumps(CUSTOMER).replace('"limos"', '"seeded"'))
    assert [i.kind for i in history_insights(detail, customer)] == []


def test_price_that_ends_soon_is_named(tmp_path):
    ending = {
        "value": "3.35",
        "valueWithTax": "3.80",
        "validFrom": "2026-05-01",
        "validTo": "2026-12-31",
        "currency": "EUR",
    }
    valio = FakeValio([price("990001", catalogPrice=ending)], [stock("990001")])
    found = kinds(run(tmp_path, [FLOUR], "flour", 2, valio))
    assert found["price_change"].text.startswith(
        "Valio's price for Invented pantry flour (3.35 €) is set until 2026-12-31"
    )
