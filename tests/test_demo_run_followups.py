"""Second live-run regressions using invented catalogues and offline clients."""

import json
from decimal import Decimal

import pytest

from agent.exceptions import cheaper_option, plan_item
from gateway.catalog.index import Catalog, tokenize
from gateway.policy import ItemContext
from tests.test_demo_run_fixes import submit
from tests.test_offers import REQUESTED, offer, price, product, request
from tests.test_pipeline import EXPLANATION, environment, picks


def carton_price(product_id="900101", unit_price="41.41", comparison="10.35", unit="KG"):
    result = price(product_id, comparisonUnit=unit)
    result.catalog_price.value = Decimal(unit_price)
    result.catalog_price.value_with_tax = Decimal(unit_price) * Decimal("1.135")
    result.comparison_price.value = Decimal(comparison)
    return result


@pytest.mark.parametrize("sales_unit", ["KI", "CS", "PRK"])
@pytest.mark.parametrize(
    "unit,quantity,pack_unit,comparison_unit",
    [
        ("kg", 5.0, "kg", "KG"),
        ("g", 5000.0, "kg", "KG"),
        ("L", 5.0, "L", "L"),
        ("ml", 5000.0, "L", "L"),
    ],
)
async def test_e_carton_content_drives_quantity_coverage_description_and_total(
    sales_unit, unit, quantity, pack_unit, comparison_unit
):
    result = await offer(
        record=product(sales_unit=sales_unit, pack_size={"value": 0.2, "unit": pack_unit}),
        body=request(quantity=quantity, unit=unit),
        prices=[carton_price(unit=comparison_unit)],
    )
    # Valio's rounded prices imply 4.001 to the specified three decimal places.
    assert result.pack_size.value == 4.001
    assert result.pack_size.unit == pack_unit
    assert result.quantity_sales_units == 2
    assert result.quantity_covered.value == pytest.approx(8.002 * quantity / 5)
    assert result.pack_description == f"4.001 {pack_unit.lower()}"
    assert result.quantity_text == f"2 x 4.001 {pack_unit.lower()}"
    assert result.line_total == 82.82
    assert result.line_total_with_tax == 94.0
    assert "OVERBUY" in result.flags


@pytest.mark.parametrize(
    "changes,unit_price,comparison,expected_pack",
    [
        ({}, "3", "10", 0.2),  # Exactly 1.5 times the catalogue pack.
        ({}, "2", "10", 0.2),
        ({}, "0", "10", 0.2),
        ({}, "-1", "10", 0.2),
        ({}, "40", "0", 0.2),
        ({}, "40", "-10", 0.2),
        ({"comparisonUnit": "L"}, "40", "10", 0.2),
        ({"comparisonUnit": None}, "40", "10", 0.2),
        ({"comparisonPrice": None}, "40", "10", 0.2),
        ({"catalogPrice": None}, "40", "10", 0.2),
        ({"error": True}, "40", "10", 0.2),
    ],
)
async def test_e_keeps_catalogue_pack_without_matching_positive_price_evidence(
    changes, unit_price, comparison, expected_pack
):
    evidence = carton_price(unit_price=unit_price, comparison=comparison)
    evidence = type(evidence).model_validate({**evidence.model_dump(by_alias=True), **changes})
    result = await offer(
        record=product(sales_unit="KI", pack_size={"value": 0.2, "unit": "kg"}),
        body=request(quantity=5.0),
        prices=[evidence],
    )
    assert result.pack_size.value == expected_pack
    assert result.quantity_sales_units == 25


@pytest.mark.parametrize("unit", ["kg", "L"])
async def test_e_price_ratio_does_not_replace_physical_sales_unit(unit):
    result = await offer(
        record=product(sales_unit=unit.upper(), pack_size=None, allowed_lot_size=0.01),
        body=request(quantity=25.0, unit=unit),
        prices=[carton_price(unit=unit.upper())],
    )
    assert result.pack_size.value == 1
    assert result.quantity_sales_units == result.quantity_covered.value == 25


def test_e_corrected_carton_survives_pick_budget_comparison_and_locked_quote(tmp_path):
    records = [
        product("990001", names={"en": "Invented butter 1 kg"}),
        product(
            "990002",
            names={"en": "Invented butter 10 g"},
            sales_unit="KI",
            pack_size={"value": 0.01, "unit": "kg"},
        ),
    ]
    selection = {
        "choices": [{"item_id": "it_1", "product_id": "990001", "suitable_ids": ["990002"]}]
    }
    with environment(tmp_path, records, [selection, EXPLANATION]) as client:
        client.app.state.valio.prices = [
            carton_price("990001", "20", "20"),
            carton_price("990002", "23.42", "5.855"),
        ]
        detail = submit(
            client,
            items=[{"description": "butter", "quantity": 5.0, "unit": "kg", "constraints": []}],
            budget=90.0,
        )
        assert detail.error is None and detail.quote is not None
        assert detail.quote.lines[0].product_id == "990001" and detail.quote.total == 100
        candidate = next(c for c in detail.candidates if c.product_id == "990002")
        assert candidate.quantity_sales_units == 2
        assert candidate.line_total == 46.84
        plan = plan_item(detail.intent.items[0], REQUESTED, detail.candidates)
        replacement = cheaper_option(detail.quote, [plan], {"it_1": ItemContext()}, REQUESTED)
        assert replacement is not None
        assert replacement[0][0].product_id == "990002"
        assert replacement[0][0].quantity_sales_units == 2
        options = client.get(f"/approvals/{detail.approval_id}").json()["options"]
        cheaper = next(option for option in options if option["lines"][0]["product_id"] == "990002")
        assert cheaper["lines"][0]["product_id"] == "990002"
        assert cheaper["lines"][0]["quantity_sales_units"] == 2
        assert cheaper["lines"][0]["quantity_text"].endswith(" x 4 kg")
        assert cheaper["lines"][0]["quantity_covered"]["value"] == 8
        assert cheaper["total"] == 46.84


@pytest.mark.parametrize("unit", ["KG", "L"])
@pytest.mark.parametrize("lot,expected", [(0.01, 1), (0.3, 1.2), (2, 2)])
async def test_f_unknown_piece_conversion_orders_at_least_one_sales_unit(unit, lot, expected):
    result = await offer(
        record=product(sales_unit=unit, pack_size=None, allowed_lot_size=lot),
        body=request(quantity=6.0, unit="pcs"),
        prices=[carton_price(unit=unit)],
    )
    assert result.pack_size is None and "PACK_SIZE_UNKNOWN" in result.flags
    assert result.quantity_sales_units == expected
    amount, sales_unit = result.quantity_text.split()
    assert Decimal(amount) == Decimal(str(expected)) and sales_unit == unit
    assert result.line_total == pytest.approx(41.41 * expected, abs=0.005)
    assert result.quantity_covered.value == 0


@pytest.mark.parametrize(
    "description,quantity",
    [("lemon", 6.0), ("onion", 6.0), ("garlic", 6.0), ("red pepper", 6.0), ("potatoes", None)],
)
def test_f_piece_needs_and_unspecified_amounts_keep_whole_kg_in_quote(
    tmp_path, description, quantity
):
    facts = {
        "items": [
            {
                "description": description,
                "description_fi": description,
                "quantity": quantity,
                "unit": "pcs" if quantity else None,
            }
        ]
    }
    records = [
        product(
            "990001",
            names={"en": f"Invented {description}"},
            sales_unit="KG",
            pack_size=None,
            allowed_lot_size=0.01,
        )
    ]
    with environment(tmp_path, records, [facts, picks("990001"), EXPLANATION]) as client:
        detail = submit(client, text=f"Invented {description} order")
        assert detail.error is None and detail.quote is not None
        line = detail.quote.lines[0]
        assert line.quantity_sales_units == 1
        amount, sales_unit = line.quantity_text.split()
        assert Decimal(amount) == 1 and sales_unit == "KG"
        assert line.line_total == 3.35
        assert line.quantity_covered.value == 0 and "PACK_SIZE_UNKNOWN" in line.flags


def test_g_raw_ingredient_instruction_reaches_pick_llm(tmp_path):
    records = [
        product(
            "990001",
            names={
                "en": "Invented freeze-dried blueberry 50g",
                "fi": "Keksitty pakastekuivattu mustikka 50g",
            },
            pack_size={"value": 0.05, "unit": "kg"},
        ),
        product(
            "990002",
            names={"fi": "Keksitty mustikka 2,5KG Puola pakaste"},
            pack_size={"value": 2.5, "unit": "kg"},
        ),
    ]
    facts = {
        "items": [
            {
                "description": "blueberries",
                "description_fi": "mustikka",
                "quantity": 2.52,
                "unit": "kg",
            }
        ]
    }
    with environment(tmp_path, records, [facts, picks("990002"), EXPLANATION]) as client:
        detail = submit(client, text="Invented blueberries order")
        assert detail.error is None and detail.quote.lines[0].product_id == "990002"
        call = client.app.state.llm.calls[1]
        instruction = call.config.system_instruction
        assert "For a raw ingredient, prefer the plain fresh or frozen form" in instruction
        for form in ("dried", "freeze-dried", "powdered", "puréed", "juice", "jam", "ready-made"):
            assert form in instruction
        assert "unless the item names that form" in instruction
        payload = json.JSONDecoder().raw_decode(call.contents[0].parts[0].text)[0]
        assert {o["product_id"] for o in payload[0]["offers"]} == {"990001", "990002"}


def test_g_documents_plural_token_gap_and_frozen_result_truncation():
    frozen = product(
        "990099", names={"en": "BLUEBERRY 2,5KG", "fi": "MUSTIKKA 2,5KG PUOLA PAKASTE"}
    )
    assert tokenize("blueberries") != tokenize("blueberry")
    catalogue = Catalog([frozen])
    assert catalogue.search("blueberries") == []
    assert catalogue.search("mustikka") == [frozen]
    shorter = [product(f"99000{i}", names={"fi": f"Mustikka variant{i}"}) for i in range(8)]
    catalogue = Catalog([frozen, *shorter])
    assert frozen in catalogue.search("mustikka", limit=20)
    assert frozen not in catalogue.search("mustikka", limit=8)
