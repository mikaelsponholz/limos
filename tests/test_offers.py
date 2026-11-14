"""Offer engine checks using only invented catalogue and Valio records."""

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from gateway.catalog.index import Catalog
from gateway.catalog.models import SlimRecord
from gateway.offers import OfferDataError, UnknownProductError, build_offers
from gateway.schemas import Offer, OffersBody
from gateway.valio.models import (
    AvailabilityResponse,
    PriceResponse,
    ProductAvailability,
    ProductPrice,
)

NOW = datetime.fromisoformat("2026-11-14T09:00:00+02:00")
REQUESTED = date(2026, 11, 20)


def product(product_id: str = "900101", **changes: Any) -> SlimRecord:
    return SlimRecord.model_validate(
        {
            "product_id": product_id,
            "names": {"en": "Invented pantry flour"},
            "vendor": "Invented Pantry",
            "sales_unit": "PRK",
            "pack_size": {"value": 1, "unit": "kg"},
            **changes,
        }
    )


def request(**changes: Any) -> OffersBody:
    return OffersBody.model_validate(
        {
            "buyer_id": "invented-buyer",
            "agent_id": "invented-agent",
            "product_ids": ["900101"],
            "item_id": "it_flour",
            "quantity": 3.0,
            "unit": "kg",
            "requested_date": REQUESTED,
            "constraints": [],
            "exclude_allergens": [],
            **changes,
        }
    )


def price(product_id: str = "900101", **changes: Any) -> ProductPrice:
    amount = {
        "value": "3.35",
        "valueWithTax": "3.80",
        "validFrom": "2026-01-01",
        "validTo": "2026-12-31",
        "currency": "EUR",
    }
    return ProductPrice.model_validate(
        {
            "productId": product_id,
            "quantity": 1,
            "catalogPrice": amount,
            "listPrice": {**amount, "value": "3.70"},
            "comparisonPrice": amount,
            "comparisonUnit": "KG",
            "tax": {"taxRate": "13.5", "taxValue": "0.45"},
            "netPriceFound": True,
            "error": False,
            **changes,
        }
    )


def stock(product_id: str = "900101", **changes: Any) -> ProductAvailability:
    return ProductAvailability.model_validate(
        {
            "productId": product_id,
            "logisticModel": "Stock",
            "closingTime": "2026-11-19T17:00:00+02:00",
            "salesBlock": False,
            "scheduleLines": [schedule()],
            **changes,
        }
    )


def schedule(amount: float = 100, **changes: Any) -> dict[str, Any]:
    return {
        "salesBlock": False,
        "availableAmount": amount,
        "availableDate": "2026-11-16",
        "salesUnit": "PRK",
        **changes,
    }


class FakeValio:
    def __init__(self, prices: list[ProductPrice], availability: list[ProductAvailability]) -> None:
        self.prices = prices
        self.stock = availability
        self.calls: list[tuple[str, list[str], date]] = []

    async def price(self, product_codes: Iterable[str], requested_date: date) -> PriceResponse:
        self.calls.append(("price", list(product_codes), requested_date))
        return PriceResponse(
            customerId="invented-buyer",
            currency="EUR",
            pricingDate=requested_date.isoformat(),
            products=self.prices,
        )

    async def availability(
        self, product_codes: Iterable[str], requested_date: date
    ) -> AvailabilityResponse:
        self.calls.append(("availability", list(product_codes), requested_date))
        return AvailabilityResponse(
            timeNow=NOW.isoformat(),
            requestedDate=requested_date.isoformat(),
            products=self.stock,
        )


async def offer(
    *,
    record: SlimRecord | None = None,
    body: OffersBody | None = None,
    prices: list[ProductPrice] | None = None,
    availability: ProductAvailability | None = None,
    max_overbuy_percent: float = 25,
) -> Offer:
    result = await build_offers(
        body or request(),
        catalog=Catalog([record or product()]),
        valio=FakeValio(
            [price()] if prices is None else prices,
            [availability or stock()],
        ),
        now=NOW,
        max_overbuy_percent=max_overbuy_percent,
    )
    assert len(result) == 1
    # Revalidate serialized output too, including timezone and strict contract constraints.
    return Offer.model_validate_json(result[0].model_dump_json())


@pytest.mark.parametrize(
    ("quantity", "unit", "pack", "lot", "units", "covered", "text"),
    [
        (1500, "g", {"value": 1, "unit": "kg"}, None, 2, 2000, "2 x 1 kg"),
        (0.3, "kg", {"value": 0.1, "unit": "kg"}, None, 3, 0.3, "3 x 0.1 kg"),
        (2500, "ml", {"value": 2, "unit": "L"}, None, 2, 4000, "2 x 2 l"),
        (2, "L", {"value": 0.25, "unit": "L"}, None, 8, 2, "8 x 0.25 l"),
        (5, "pcs", {"value": 2, "unit": "pcs"}, None, 3, 6, "3 x 2 pcs"),
        (3, "kg", {"value": 1, "unit": "kg"}, 2, 4, 4, "4 x 1 kg"),
        (4.1, "kg", {"value": 2, "unit": "kg"}, 3, 3, 6, "3 x 2 kg"),
        (2.1, "kg", {"value": 1, "unit": "kg"}, 2.5, 5, 5, "5 x 1 kg"),
    ],
)
async def test_conversion_and_lot_rounding(
    quantity: float,
    unit: str,
    pack: dict[str, Any],
    lot: float | None,
    units: float,
    covered: float,
    text: str,
) -> None:
    result = await offer(
        record=product(pack_size=pack, allowedLotSize=lot),
        body=request(quantity=float(quantity), unit=unit),
    )
    assert result.quantity_sales_units == units
    assert result.quantity_requested.value == quantity
    assert result.quantity_requested.unit == unit
    assert result.quantity_covered.value == pytest.approx(covered)
    assert result.quantity_covered.unit == unit
    assert result.quantity_text == text
    assert "PACK_SIZE_UNKNOWN" not in result.flags


async def test_resolves_catalogue_conversion_before_name() -> None:
    result = await offer(
        record=product(
            names={"en": "Invented flour 9 kg"},
            pack_size=None,
            sales_unit="CASE",
            base_unit="EACH",
            units=[{"unitId": "CASE", "sizeInBaseUnits": 4}],
            unit_conversions=[
                {
                    "synkkaPackagingType": "BASE_UNIT_OR_EACH",
                    "netWeight": {"value": 500, "unit": "GRM"},
                }
            ],
        )
    )
    assert result.pack_size is not None and result.pack_size.value == 2
    assert result.quantity_sales_units == 2
    assert result.quantity_text == "2 x 2 kg"


async def test_resolves_pack_from_invented_name() -> None:
    result = await offer(record=product(pack_size=None, names={"fi": "Keksitty jauho 500 g"}))
    assert result.pack_size is not None and result.pack_size.value == 0.5
    assert result.quantity_sales_units == 6


@pytest.mark.parametrize("pack", [None, {"value": 2, "unit": "L"}])
@pytest.mark.parametrize(("lot", "expected"), [(None, 1), (2, 2), (0.5, 1)])
async def test_unknown_or_incompatible_pack_orders_smallest_lot(
    pack: dict[str, Any] | None, lot: float | None, expected: float
) -> None:
    result = await offer(record=product(pack_size=pack, allowedLotSize=lot))
    assert result.pack_size is None
    assert result.quantity_sales_units == expected
    assert result.quantity_covered.value == 0
    assert result.quantity_covered.unit == "kg"
    assert result.quantity_text == f"{expected:g} PRK"
    assert "PACK_SIZE_UNKNOWN" in result.flags
    assert "OVERBUY" not in result.flags


async def test_partial_stock_preserves_lot_multiples_and_reprices() -> None:
    result = await offer(
        record=product(allowedLotSize=2),
        body=request(quantity=7.0),
        availability=stock(scheduleLines=[schedule(5.5)]),
    )
    assert result.available_amount == 5.5
    assert result.quantity_sales_units == 4
    assert result.quantity_covered.value == 4
    assert result.line_total == 13.4
    assert result.line_total_with_tax == 15.2
    assert result.flags == ["PARTIAL"]
    assert "available in full lots" in result.reason


@pytest.mark.parametrize("lines", [[], [schedule(0)]])
async def test_zero_stock_or_empty_schedule_is_not_deliverable(lines: list[dict[str, Any]]) -> None:
    result = await offer(availability=stock(scheduleLines=lines))
    assert "NOT_DELIVERABLE" in result.flags
    assert result.available_date is None
    assert result.delivery_date is None
    assert result.quantity_sales_units == result.quantity_covered.value == 0
    assert result.line_total == result.line_total_with_tax == 0


async def test_no_full_lot_available_is_not_deliverable() -> None:
    result = await offer(
        record=product(allowedLotSize=2), availability=stock(scheduleLines=[schedule(1.9)])
    )
    assert result.quantity_sales_units == 0
    assert result.available_amount == 1.9
    assert result.flags == ["NOT_DELIVERABLE", "PARTIAL"]
    assert result.available_date is result.delivery_date is None


@pytest.mark.parametrize(
    ("model", "amount", "available"),
    [
        ("Stock", 9999999999, None),
        ("Stock", 1000000000, None),
        ("Stock", 999999, 999999),
        ("Terminal", 999999, None),
        ("Terminal", 1, None),
    ],
)
async def test_sentinel_and_terminal_amounts_are_not_partial(
    model: str, amount: float, available: float | None
) -> None:
    result = await offer(availability=stock(logisticModel=model, scheduleLines=[schedule(amount)]))
    assert result.quantity_sales_units == 3
    assert result.available_amount == available
    assert "PARTIAL" not in result.flags
    assert "NOT_DELIVERABLE" not in result.flags


async def test_terminal_zero_is_not_deliverable() -> None:
    result = await offer(availability=stock(logisticModel="Terminal", scheduleLines=[schedule(0)]))
    assert result.available_amount is None
    assert result.flags == ["NOT_DELIVERABLE"]


@pytest.mark.parametrize(
    ("available", "delivered", "late"),
    [
        ("2026-11-16", REQUESTED, False),
        ("2026-11-20", REQUESTED, False),
        ("2026-11-23", date(2026, 11, 23), True),
    ],
)
async def test_delivery_date_is_later_of_requested_and_available(
    available: str, delivered: date, late: bool
) -> None:
    result = await offer(availability=stock(scheduleLines=[schedule(availableDate=available)]))
    assert result.available_date == date.fromisoformat(available)
    assert result.delivery_date == delivered
    assert ("LATE_DELIVERY" in result.flags) == late


@pytest.mark.parametrize(
    ("closing", "passed"),
    [
        ("2026-11-19T17:00:00+02:00", False),
        ("2026-11-13T17:00:00+02:00", True),
        ("2026-11-14T09:00:00+02:00", True),
        ("2026-11-14T07:00:00Z", True),
        ("2026-11-19T17:00:00", False),
        (None, False),
    ],
)
async def test_deadline_and_cutoff(closing: str | None, passed: bool) -> None:
    result = await offer(availability=stock(closingTime=closing))
    if closing is None:
        assert result.closing_time is result.order_deadline is None
    else:
        parsed = datetime.fromisoformat(closing)
        expected = parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=NOW.tzinfo)
        assert result.closing_time == expected
        assert result.order_deadline == (None if passed else expected)
    assert ("CUTOFF_PASSED" in result.flags) == passed
    assert result.delivery_date == REQUESTED


@pytest.mark.parametrize(
    "prices",
    [[], [price(error=True)], [price(netPriceFound=False)], [price(catalogPrice=None)]],
)
async def test_missing_price_uses_numeric_zeroes(prices: list[ProductPrice]) -> None:
    result = await offer(prices=prices)
    assert "PRICE_MISSING" in result.flags
    assert result.unit_price == result.list_price == result.comparison_price.value == 0
    assert result.line_total == result.line_total_with_tax == result.tax_rate == 0
    assert "EUR/" not in result.reason


async def test_prices_preserve_values_and_round_line_totals_to_cents() -> None:
    original = price()
    assert original.catalog_price is not None
    original.catalog_price.value = Decimal("1.005")
    original.catalog_price.value_with_tax = Decimal("1.145")
    result = await offer(body=request(quantity=1.0), prices=[original])
    assert result.unit_price == 1.005
    assert result.list_price == 3.7
    assert result.comparison_price.value == 3.35
    assert result.comparison_price.unit == "KG"
    assert result.tax_rate == 13.5
    assert result.line_total == 1.01
    assert result.line_total_with_tax == 1.15


async def test_explicit_gross_price_is_used() -> None:
    result = await offer(
        prices=[
            price(
                priceWithTax={
                    "value": "4.05",
                    "validFrom": "2026-01-01",
                    "validTo": "2026-12-31",
                    "currency": "EUR",
                }
            )
        ]
    )
    assert result.line_total_with_tax == 12.15


async def test_missing_comparison_price_does_not_invent_a_per_kg_reason() -> None:
    result = await offer(prices=[price(comparisonPrice=None)])
    assert result.comparison_price.value == 0
    assert "3.35 EUR/PRK" in result.reason
    assert "EUR/kg" not in result.reason


async def test_volume_comparison_price_keeps_its_valio_unit() -> None:
    result = await offer(
        record=product(pack_size={"value": 1, "unit": "L"}),
        body=request(unit="L"),
        prices=[price(comparisonUnit="L")],
    )
    assert result.comparison_price.unit == "L"
    assert "3.35 EUR/l" in result.reason


@pytest.mark.parametrize(("reliability", "low"), [(0.89, True), (0.90, False), (None, False)])
@pytest.mark.parametrize("model", ["Stock", "Terminal"])
async def test_low_reliability_only_flags_terminal(
    reliability: float | None, low: bool, model: str
) -> None:
    result = await offer(
        record=product(delivery_reliability=reliability), availability=stock(logisticModel=model)
    )
    assert result.delivery_reliability == reliability
    assert ("TERMINAL_LOW_RELIABILITY" in result.flags) == (low and model == "Terminal")


@pytest.mark.parametrize(
    ("quantity", "limit", "overbuy"),
    [
        (4.0, 25, False),
        (3.999, 25, True),
        (4.0, 24.99, True),
        (5.0, 0, False),
    ],
)
async def test_overbuy_strict_boundary(quantity: float, limit: float, overbuy: bool) -> None:
    result = await offer(
        record=product(pack_size={"value": 5, "unit": "kg"}),
        body=request(quantity=quantity),
        max_overbuy_percent=limit,
    )
    assert ("OVERBUY" in result.flags) == overbuy


@pytest.mark.parametrize(
    ("facts", "constraint", "expected"),
    [
        ({"claims": ["lactose_free"]}, "lactose_free", "confirmed"),
        ({"labels": ["gluten_free"]}, "gluten_free", "confirmed"),
        (
            {"allergens": {"free_from": ["lactose"], "data_available": True}},
            "lactose_free",
            "confirmed",
        ),
        ({"claims": ["low_lactose"]}, "lactose_free", "violated"),
        (
            {"allergens": {"contains": ["gluten"], "data_available": True}},
            "gluten_free",
            "violated",
        ),
        ({"country_of_origin": "fi"}, "finnish_origin", "confirmed"),
        ({"country_of_origin": "se", "labels": ["finnish_label"]}, "finnish_origin", "violated"),
        ({"labels": ["finnish_label"]}, "finnish_origin", "unknown"),
        ({"organic": {"strength": "label"}}, "organic", "confirmed"),
        ({"labels": ["organic_label"]}, "organic", "confirmed"),
        ({"labels": ["organic_name"], "organic": {"strength": "name_only"}}, "organic", "unknown"),
        ({"claims": ["vegan"]}, "vegetarian", "confirmed"),
        ({"claims": ["vegetarian"]}, "vegan", "unknown"),
        (
            {"claims": ["vegan"], "allergens": {"contains": ["milk"], "data_available": True}},
            "vegan",
            "violated",
        ),
        ({"allergens": {"contains": ["fish"], "data_available": True}}, "vegetarian", "violated"),
        ({"carbon_kg_co2e_per_kg": 0.01}, "low_carbon", "unknown"),
        ({"allergens": {"free_from": ["egg"], "data_available": True}}, "no_egg", "confirmed"),
        (
            {"allergens": {"contains": ["milk"], "free_from": ["milk"], "data_available": True}},
            "no_milk",
            "violated",
        ),
        ({"allergens": {"contains": ["milk"], "data_available": True}}, "no_egg", "unknown"),
        ({"allergens": {"may_contain": ["egg"], "data_available": True}}, "no_egg", "unknown"),
        ({"allergens": {"undeclared": ["egg"], "data_available": True}}, "no_egg", "unknown"),
    ],
)
async def test_dietary_evidence(facts: dict[str, Any], constraint: str, expected: str) -> None:
    result = await offer(record=product(**facts), body=request(constraints=[constraint]))
    assert result.dietary_check == {constraint: expected}


async def test_missing_evidence_is_unknown_for_every_preference() -> None:
    constraints = [
        "lactose_free",
        "gluten_free",
        "finnish_origin",
        "organic",
        "vegetarian",
        "vegan",
        "low_carbon",
    ]
    result = await offer(body=request(constraints=constraints, exclude_allergens=["milk"]))
    assert result.dietary_check == {key: "unknown" for key in [*constraints, "no_milk"]}
    assert result.flags == ["ALLERGEN_DATA_MISSING"]


async def test_allergen_exclusions_merge_with_constraints_and_flag_trace_risk() -> None:
    result = await offer(
        record=product(
            allergens={"may_contain": ["tree_nuts"], "free_from": ["egg"], "data_available": True}
        ),
        body=request(
            constraints=["no_tree_nuts", "no_egg"], exclude_allergens=["tree_nuts", "egg"]
        ),
    )
    assert result.dietary_check == {"no_tree_nuts": "unknown", "no_egg": "confirmed"}
    assert result.flags == ["MAY_CONTAIN_EXCLUDED"]


async def test_allergen_constraint_alone_triggers_missing_data_flag() -> None:
    result = await offer(body=request(constraints=["no_milk"]))
    assert result.flags == ["ALLERGEN_DATA_MISSING"]


@pytest.mark.parametrize("source", ["catalogue", "product", "schedule"])
async def test_sales_block_sources(source: str) -> None:
    result = await offer(
        record=product(sales_block=source == "catalogue"),
        availability=stock(
            salesBlock=source == "product",
            scheduleLines=[schedule(salesBlock=source == "schedule")],
        ),
    )
    assert "SALES_BLOCK" in result.flags
    assert result.delivery_date is None
    assert result.available_amount == 100


async def test_earliest_usable_schedule_ignores_blocked_dates_without_summing_stock() -> None:
    result = await offer(
        availability=stock(
            scheduleLines=[
                schedule(100, salesBlock=True, availableDate="2026-11-16"),
                schedule(20, availableDate="2026-11-24"),
                schedule(2, availableDate="2026-11-23"),
            ]
        )
    )
    assert result.available_date == result.delivery_date == date(2026, 11, 23)
    assert result.available_amount == result.quantity_sales_units == 2
    assert result.flags == ["LATE_DELIVERY", "PARTIAL"]


async def test_one_call_per_shortlist_input_order_and_unranked_contract() -> None:
    records = [
        product("900101", country_of_origin="fi", carbon_kg_co2e_per_kg=2.3),
        product("900102"),
    ]
    fake = FakeValio(
        [price("900101"), price("900102", error=True)],
        [stock("900102"), stock("900101")],
    )
    codes = ["900102", "900101", "900102"]
    results = await build_offers(
        request(product_ids=codes), catalog=Catalog(records), valio=fake, now=NOW
    )
    assert fake.calls == [("price", codes, REQUESTED), ("availability", codes, REQUESTED)]
    assert [result.product_id for result in results] == codes
    for result in results:
        assert Offer.model_validate_json(result.model_dump_json()) == result
        assert result.tier is None and result.score == 0
        assert result.is_split is result.is_substitute is False
        assert result.substitute_for is result.substitute_source is None
        assert result.item_id == "it_flour"
        assert result.flags == sorted(set(result.flags))
        assert result.reason
    assert results[1].carbon_kg_co2e_per_kg == 2.3
    assert results[1].country_of_origin == "fi"
    assert results[1].name == "Invented pantry flour"
    assert results[1].vendor == "Invented Pantry"
    assert results[2].carbon_kg_co2e_per_kg is None
    assert results[0].unit_price == results[2].unit_price == 0
    assert results[1].unit_price == 3.35


async def test_unknown_catalogue_id_fails_before_valio_calls() -> None:
    fake = FakeValio([], [])
    with pytest.raises(UnknownProductError, match="900999") as caught:
        await build_offers(
            request(product_ids=["900101", "900999"]),
            catalog=Catalog([product()]),
            valio=fake,
            now=NOW,
        )
    assert caught.value.code == "PRODUCT_NOT_FOUND"
    assert fake.calls == []


async def test_empty_shortlist_avoids_valio() -> None:
    fake = FakeValio([], [])
    assert (
        await build_offers(request(product_ids=[]), catalog=Catalog([]), valio=fake, now=NOW) == []
    )
    assert fake.calls == []


@pytest.mark.parametrize("availability", [[], [stock(logisticModel=None)]])
async def test_incomplete_availability_does_not_invent_stock_model(
    availability: list[ProductAvailability],
) -> None:
    with pytest.raises(OfferDataError):
        await build_offers(
            request(),
            catalog=Catalog([product()]),
            valio=FakeValio([price()], availability),
            now=NOW,
        )


async def test_flags_are_sorted_and_not_duplicated() -> None:
    result = await offer(
        record=product(pack_size=None, delivery_reliability=0.8),
        body=request(exclude_allergens=["milk"]),
        prices=[],
        availability=stock(
            logisticModel="Terminal",
            closingTime="2026-11-12T12:00:00+02:00",
            scheduleLines=[schedule(999999, availableDate="2026-11-23")],
        ),
    )
    assert result.flags == [
        "ALLERGEN_DATA_MISSING",
        "CUTOFF_PASSED",
        "LATE_DELIVERY",
        "PACK_SIZE_UNKNOWN",
        "PRICE_MISSING",
        "TERMINAL_LOW_RELIABILITY",
    ]
