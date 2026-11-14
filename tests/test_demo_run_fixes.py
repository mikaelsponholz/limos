"""Live-demo regressions with invented catalogues and offline LLM/Valio clients."""

import json

import pytest

from gateway.audit import verify_chain
from tests.engine_support import BUYER
from tests.test_e2e import poll
from tests.test_offers import offer, product, request
from tests.test_pipeline import EXPLANATION, environment, picks


def submit(client, **changes):
    response = client.post(
        "/requests",
        json={"buyer_id": BUYER, "requested_date": "2026-11-20", **changes},
    )
    assert response.status_code == 202, response.text
    detail = poll(client, response.json()["request_id"])
    assert verify_chain(detail.steps)
    return detail


@pytest.mark.parametrize("missing_pick", [False, True])
@pytest.mark.parametrize(
    "text,names,amounts,units",
    [
        (
            "kananmunia 200 kpl ja voita 10 kg",
            [("eggs", "kananmuna"), ("butter", "voi")],
            [200.0, 10.0],
            ["pcs", "kg"],
        ),
        (
            "maitoo ja kermaa huomiseks",
            [("milk", "maito"), ("cream", "kerma")],
            [None, None],
            [None, None],
        ),
    ],
)
def test_a_finnish_base_forms_reach_search_and_preserve_amounts(
    tmp_path, monkeypatch, text, names, amounts, units, missing_pick
):
    records = [
        product(
            f"99000{index}",
            names={"fi": f"Keksitty {fi}"},
            pack_size={"value": 10 if unit == "pcs" else 1, "unit": unit or "L"},
        )
        for index, ((_, fi), unit) in enumerate(zip(names, units, strict=True), 1)
    ]
    facts = {
        "items": [
            {"description": en, "description_fi": fi, "quantity": amount, "unit": unit}
            for (en, fi), amount, unit in zip(names, amounts, units, strict=True)
        ]
    }
    selection = picks() if missing_pick else picks("990001", "990002")
    with environment(tmp_path, records, [facts, selection, EXPLANATION]) as client:
        catalogue = client.app.state.catalog
        original_search = catalogue.search
        queries = []

        def search(query, **kwargs):
            queries.append(query)
            return original_search(query, **kwargs)

        monkeypatch.setattr(catalogue, "search", search)
        detail = submit(client, text=text)
        assert detail.error is None and detail.quote is not None
        assert [(i.description, i.description_fi) for i in detail.intent.items] == names
        assert queries == [term for pair in names for term in pair]
        assert [i.quantity for i in detail.intent.items] == [a or 1 for a in amounts]
        assert [i.unit for i in detail.intent.items] == [u or "pcs" for u in units]
        assert len(detail.quote.lines) == 2
        assert detail.question is None
        assert sum("quoting one pack" in a for a in detail.intent.assumptions) == (
            2 if amounts[0] is None else 0
        )
        prompt = client.app.state.llm.calls[0].config.system_instruction
        for rule in ("spoken Finnish", "plain English", "nominative singular", "maitoo -> maito"):
            assert rule in prompt


def test_a_missing_finnish_description_falls_back_and_search_is_deduplicated(tmp_path, monkeypatch):
    facts = {"items": [{"description": "flour", "quantity": 2.0, "unit": "kg"}]}
    with environment(
        tmp_path, [product("990001")], [facts, picks("990001"), EXPLANATION]
    ) as client:
        original_search = client.app.state.catalog.search
        queries = []

        def search(query, **kwargs):
            queries.append(query)
            return original_search(query, **kwargs)

        monkeypatch.setattr(client.app.state.catalog, "search", search)
        detail = submit(client, text="Invented flour order")
        assert detail.error is None and detail.quote is not None
        assert detail.intent.items[0].description_fi == "flour"
        assert queries == ["flour"]


@pytest.mark.parametrize("overlap", [False, True])
def test_b_bilingual_results_interleave_deduplicate_and_reach_pick_and_batch(
    tmp_path, monkeypatch, overlap
):
    records = [product(f"99000{i}", names={"en": "Invented milk jug"}) for i in range(1, 6)] + [
        product(f"99100{i}", names={"fi": "Keksitty maito"}) for i in range(1, 6)
    ]
    english = records[:5]
    finnish = ([english[0]] if overlap else []) + records[5:]
    expected = (
        ["990001", "990002", "991001", "990003", "991002", "990004", "991003", "990005"]
        if overlap
        else ["990001", "991001", "990002", "991002", "990003", "991003", "990004", "991004"]
    )
    expansion = {
        "dishes": [
            {
                "name": "Invented muffins",
                "ingredients": [
                    {
                        "description": "milk",
                        "description_fi": "maito",
                        "quantity_per_serving": 0.1,
                        "unit": "kg",
                        "search_terms": ["milk", "maito"],
                    }
                ],
            }
        ]
    }
    # Pick the last candidate to prove that all eight survive relevance validation.
    with environment(tmp_path, records, [expansion, picks(expected[-1]), EXPLANATION]) as client:
        queries = []

        def search(query, **kwargs):
            queries.append(query)
            return (english if query == "milk" else finnish)[: kwargs["limit"]]

        monkeypatch.setattr(client.app.state.catalog, "search", search)
        detail = submit(
            client,
            event={
                "type": "lunch",
                "date": "2026-11-21",
                "guests": 10,
                "dishes": [{"name": "Invented muffins", "servings": None}],
            },
        )
        assert detail.error is None and detail.quote is not None
        assert queries == ["milk", "maito"]
        assert [c.product_id for c in detail.candidates] == expected
        pick_input = json.JSONDecoder().raw_decode(
            client.app.state.llm.calls[1].contents[0].parts[0].text
        )[0]
        assert [o["product_id"] for o in pick_input[0]["offers"]] == expected
        assert detail.quote.lines[0].product_id == expected[-1]
        assert [c[0] for c in client.app.state.valio.calls] == ["price", "availability"]
        assert all(c[1] == expected for c in client.app.state.valio.calls)


@pytest.mark.parametrize(
    "text,event_type,guests,dishes",
    [
        (
            "tarvitaan 30 henkilön lounas, kasvisvaihtoehto ja gluteeniton",
            "lunch",
            30,
            ["Invented vegetable stew", "Invented lentil main", "Invented salad"],
        ),
        (
            "joulupöytä 40 hengelle",
            "Christmas buffet",
            40,
            ["Invented casserole", "Invented roast", "Invented salad", "Invented dessert"],
        ),
    ],
)
def test_c_event_without_dishes_proposes_then_expands_visible_menu(
    tmp_path, text, event_type, guests, dishes
):
    preferences = [{"key": "vegetarian", "level": "require"}]
    facts = {
        "event_type": event_type,
        "guests": guests,
        "preferences": preferences,
        "exclude_allergens": ["gluten"],
    }
    expansion = {
        "dishes": [
            {
                "name": name,
                "ingredients": [
                    {
                        "description": "rice",
                        "description_fi": "riisi",
                        "quantity_per_serving": 0.1,
                        "unit": "kg",
                        "search_terms": ["rice", "riisi"],
                    }
                ],
            }
            for name in dishes
        ]
    }
    records = [
        product(
            "990001",
            names={"fi": "Keksitty riisi"},
            claims=["vegetarian", "gluten_free"],
            allergens={"free_from": ["gluten"], "data_available": True},
        )
    ]
    outputs = [facts, {"dishes": dishes}, expansion, picks("990001"), EXPLANATION]
    with environment(tmp_path, records, outputs) as client:
        detail = submit(client, text=text)
        assert detail.error is None and detail.quote is not None
        assert detail.question is None
        assert [d.name for d in detail.intent.dishes] == dishes
        assert [d.servings for d in detail.intent.dishes] == [guests] * len(dishes)
        assert {use.dish_id for use in detail.intent.items[0].used_in} == {
            f"d_{i}" for i in range(1, len(dishes) + 1)
        }
        assert (
            f"No dishes were named; proposed {', '.join(dishes[:-1])} and {dishes[-1]}. "
            "Change them before approving."
        ) in detail.intent.assumptions
        calls = client.app.state.llm.calls
        assert len(calls) == 5  # Parse, menu proposal, expansion, pick, explanation.
        menu_input = json.JSONDecoder().raw_decode(calls[1].contents[0].parts[0].text)[0]
        assert menu_input == {
            "event_type": event_type,
            "guests": guests,
            "preferences": preferences,
            "exclude_allergens": ["gluten"],
        }
        expansion_input = json.JSONDecoder().raw_decode(calls[2].contents[0].parts[0].text)[0]
        assert expansion_input["dishes"] == [{"name": d, "servings": guests} for d in dishes]
        assert expansion_input["preferences"] == ["vegetarian"]
        assert expansion_input["exclude_allergens"] == ["gluten"]


@pytest.mark.parametrize("unit", ["kg", "L"])
@pytest.mark.parametrize("quantity,expected", [(25.0, 25.0), (25.001, 25.01)])
async def test_d_loose_sales_units_use_physical_quantity_and_fractional_lot(
    unit, quantity, expected
):
    result = await offer(
        record=product(
            names={"en": "Invented loose product"},
            sales_unit=unit.upper(),
            base_unit=unit.upper(),
            pack_size=None,
            allowed_lot_size=0.01,
        ),
        body=request(quantity=quantity, unit=unit),
    )
    assert result.pack_size.value == 1 and result.pack_size.unit == unit
    assert result.quantity_sales_units == expected
    assert result.quantity_covered.value == expected
    assert "PACK_SIZE_UNKNOWN" not in result.flags


@pytest.mark.parametrize("with_conversion", [False, True])
async def test_d_incompatible_weight_uses_named_litre_pack_without_density(with_conversion):
    result = await offer(
        record=product(
            names={"en": "Invented whole milk 1 l lactose free UHT"},
            pack_size={"value": 1.027, "unit": "kg"},
            unit_conversions=(
                [
                    {
                        "synkkaPackagingType": "BASE_UNIT_OR_EACH",
                        "netWeight": {"value": 1.027, "unit": "kg"},
                    }
                ]
                if with_conversion
                else []
            ),
        ),
        body=request(quantity=40.0, unit="L"),
    )
    assert result.pack_size.value == 1 and result.pack_size.unit == "L"
    assert result.quantity_sales_units == result.quantity_covered.value == 40
    assert "PACK_SIZE_UNKNOWN" not in result.flags


async def test_d_incompatible_weight_without_volume_evidence_stays_unknown():
    result = await offer(
        record=product(names={"en": "Invented milk"}, pack_size={"value": 1.027, "unit": "kg"}),
        body=request(quantity=40.0, unit="L"),
    )
    assert result.pack_size is None and "PACK_SIZE_UNKNOWN" in result.flags
    assert result.quantity_covered.value == 0


@pytest.mark.parametrize(
    "description,quantity,unit,sales_unit,pack,expected",
    [
        ("potatoes", 25.0, "kg", "KG", None, 25.0),
        ("potatoes", 25.001, "kg", "KG", None, 25.01),
        ("milk", 40.0, "L", "PRK", {"value": 1.027, "unit": "kg"}, 40.0),
    ],
)
def test_d_correct_physical_quantities_survive_picking_and_locked_quote(
    tmp_path, description, quantity, unit, sales_unit, pack, expected
):
    records = [
        product(
            "990001",
            names={"en": f"Invented {description} 1 {unit}"},
            sales_unit=sales_unit,
            pack_size=pack,
            allowed_lot_size=0.01 if sales_unit == "KG" else None,
        )
    ]
    with environment(tmp_path, records, [picks("990001"), EXPLANATION]) as client:
        detail = submit(
            client,
            items=[
                {
                    "description": description,
                    "quantity": quantity,
                    "unit": unit,
                    "constraints": [],
                }
            ],
        )
        assert detail.error is None and detail.quote is not None
        assert detail.quote.lines[0].quantity_sales_units == expected
        assert detail.quote.lines[0].quantity_covered.value == expected
        assert "PACK_SIZE_UNKNOWN" not in detail.quote.lines[0].flags
