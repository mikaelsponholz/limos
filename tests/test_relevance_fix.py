"""Suitability regressions with invented products, FakeLLM and fake Valio only."""

import json

import pytest

from agent.exceptions import cheaper_option, plan_item, quote_options
from agent.expansion import PipelineExpansion, build_event_items
from agent.relevance import Pick, validated
from gateway.catalog.index import Catalog, tokenize
from gateway.customers import CUSTOMERS_DIR, load_customers
from gateway.policy import ItemContext
from gateway.policy.engine import evaluate
from gateway.schemas import Event, EventDish, IntentItem, Quantity
from gateway.settings import Settings
from scripts import find_demo_cases as finder
from scripts.seed_customers import resolve_customers
from tests.engine_support import AGENT, Engine, mandate
from tests.test_e2e import poll
from tests.test_offers import REQUESTED, FakeValio, offer, price, product, request, schedule, stock
from tests.test_pipeline import EXPLANATION, codes, environment, item, start
from tests.test_pipeline_presets import environment as preset_environment


def choice(item_id, product_id, suitable_ids=()):
    return {"item_id": item_id, "product_id": product_id, "suitable_ids": list(suitable_ids)}


def picks(*entries):
    return {"choices": list(entries)}


def pick_calls(client):
    return [
        call
        for call in client.app.state.llm.calls
        if "choices" in (call.config.response_json_schema or {}).get("properties", {})
    ]


def prompt(call):
    return json.loads(call.contents[0].parts[0].text.split("\nReturn JSON")[0])


@pytest.mark.parametrize("query", ["egg", "eggs", "EGGS!"])
def test_eggs_rank_before_egg_noodles(query):
    records = [
        product("990001", names={"en": "Invented egg noodles"}),
        product("990099", names={"en": "Invented eggs"}),
    ]
    assert [r.product_id for r in Catalog(reversed(records)).search(query)] == ["990099", "990001"]


@pytest.mark.parametrize(
    "singular,plural",
    [("box", "boxes"), ("potato", "potatoes"), ("noodle", "noodles"), ("egg", "eggs")],
)
def test_normalisation_is_shared_by_index_and_query(singular, plural):
    assert tokenize(singular) == tokenize(plural)
    for indexed, query in [(singular, plural), (plural, singular)]:
        catalog = Catalog([product("990099", names={"en": f"Invented {indexed}"})])
        assert catalog.search(query)[0].product_id == "990099"
    assert tokenize("glass swiss") == {"glass", "swiss"}


def test_specificity_then_code_ties():
    records = [
        product("990001", names={"en": "Invented eggs extra words"}),
        product("990099", names={"en": "Invented eggs"}),
        product("990098", names={"en": "Invented eggs"}),
    ]
    assert [r.product_id for r in Catalog(records).search("egg")] == ["990098", "990099", "990001"]


def test_bilingual_name_does_not_lose_specificity_to_noodles():
    catalog = Catalog(
        [
            product("990001", names={"en": "Invented egg noodles"}),
            product(
                "990099", names={"en": "Invented eggs", "fi": "Keksityt kananmunat pakkauksessa"}
            ),
        ]
    )
    assert catalog.search("egg")[0].product_id == "990099"


def test_expansion_keeps_a_finnish_search_term():
    dish = EventDish(name="Invented salad", servings=10)
    event = Event(type="lunch", date=REQUESTED, guests=10)
    output = PipelineExpansion.model_validate(
        {
            "dishes": [
                {
                    "name": dish.name,
                    "ingredients": [
                        {
                            "description": "cucumber",
                            "description_fi": "kurkku",
                            "unit": "kg",
                            "quantity_per_serving": 0.1,
                            "search_terms": ["cucumber", "cucumbers"],
                        }
                    ],
                }
            ]
        }
    )
    result = build_event_items(output, event, [dish])
    assert result.search_terms == {"it_1": ["cucumber", "kurkku"]}


def test_null_is_final_and_missing_item_is_named_for_the_human(tmp_path):
    records = [product("990001"), product("990002", names={"en": "Invented chilli paste"})]
    outputs = [picks(choice("it_1", "990001"), choice("it_2", None, ["990002"])), EXPLANATION]
    with environment(tmp_path, records, outputs) as client:
        detail = start(client, items=[item(), item("sugar paste")])
        assert detail.status == "WAITING_APPROVAL"
        assert [line.product_id for line in detail.quote.lines] == ["990001"]
        expected = "No suitable product found for the sugar paste; a person needs to choose one."
        assert (
            next(r.description for r in detail.quote.policy.rules_fired if r.item_id == "it_2")
            == expected
        )
        assert expected in detail.quote.agent_explanation
        assert any(
            s.detail.get("code") == "NOT_SUITABLE" and s.detail["item_id"] == "it_2"
            for s in detail.steps
        )
        assert len(pick_calls(client)) == 1
        assert "FALLBACK_PICK" not in codes(detail)
        assert expected in prompt_explanation(client)


def prompt_explanation(client):
    call = next(
        call
        for call in client.app.state.llm.calls
        if "text" in (call.config.response_json_schema or {}).get("properties", {})
    )
    return call.contents[0].parts[0].text


def test_missing_pick_repaired_once_for_only_missing_items(tmp_path):
    records = [product("990001"), product("990002", names={"en": "Invented carrots"})]
    outputs = [picks(choice("it_1", "990001")), picks(choice("it_2", "990002")), EXPLANATION]
    with environment(tmp_path, records, outputs) as client:
        detail = start(client, items=[item(), item("carrots")])
        assert len(detail.quote.lines) == 2
        calls = pick_calls(client)
        assert len(calls) == 2
        assert [row["item_id"] for row in prompt(calls[1])] == ["it_2"]
        assert "MISSING_PICK" not in codes(detail)


@pytest.mark.parametrize(
    "name,description,found",
    [
        ("Invented sugar paste", "sugar paste", True),
        ("Invented chilli paste", "sugar paste", False),
        ("Invented eggs", "egg", True),
        ("Invented kalaliemi", "kalaliemi", True),
    ],
)
def test_still_missing_falls_back_only_with_full_name_tokens(tmp_path, name, description, found):
    outputs = [picks(), picks(), EXPLANATION] if found else [picks(), picks()]
    with environment(tmp_path, [product("990001", names={"fi": name})], outputs) as client:
        detail = start(client, items=[item(description)])
        assert len(pick_calls(client)) == 2
        assert "MISSING_PICK" in codes(detail)
        assert ("FALLBACK_PICK" in codes(detail)) is found
        if found:
            assert detail.quote.lines[0].product_id == "990001"
            step = next(s for s in detail.steps if s.detail.get("code") == "FALLBACK_PICK")
            assert step.detail["product_id"] == "990001"
        else:
            assert detail.status == "BLOCKED" and detail.quote is None
            assert "NOT_SUITABLE" in codes(detail)


def test_unknown_ids_do_not_grant_suitability():
    from tests.test_policy import offer as policy_offer

    accepted = validated(
        [Pick(item_id="it_1", product_id="900301", suitable_ids=["900301", "unknown"])],
        [policy_offer()],
    )
    assert accepted.suitable_ids == ["900301"]
    assert validated([Pick(item_id="it_1", product_id="unknown")], [policy_offer()]) is None


@pytest.mark.parametrize("accepted", [False, True])
async def test_splits_alternatives_and_cheaper_options_use_only_suitable_ids(accepted):
    original = await offer(
        body=request(quantity=3.0), availability=stock(scheduleLines=[schedule(1)])
    )
    original.name = "Invented sugar paste"
    unsuitable = original.model_copy(deep=True)
    unsuitable.product_id = "900102"
    unsuitable.name = "Invented chilli paste"
    unsuitable.unit_price = 0.1
    unsuitable.quantity_covered = Quantity(value=3, unit="kg")
    unsuitable.quantity_sales_units = 3
    allowed = [original.product_id, unsuitable.product_id] if accepted else [original.product_id]
    wanted = IntentItem(
        item_id="it_flour",
        description="sugar paste",
        description_fi="sokerimassa",
        quantity=3,
        unit="kg",
        constraints=[],
        used_in=[],
    )
    plan = plan_item(wanted, REQUESTED, [original, unsuitable], suitable_ids=allowed)
    assert (len(plan.recommended) == 2) is accepted
    assert bool(plan.alternatives) is accepted
    assert {o.product_id for o in plan.offers} <= set(allowed)
    quote = await Engine().quote(lines=[original], budget=1.0)
    cheaper = cheaper_option(quote, [plan], {"it_flour": ItemContext()}, REQUESTED)
    assert (cheaper is not None) is accepted
    if not accepted:
        # Even a stale variant cannot reintroduce an unapproved id.
        plan.alternatives = [[plan.recommended[0].model_copy(update={"product_id": "900102"})]]
        assert len(quote_options([wanted], [plan], REQUESTED)) == 1


@pytest.mark.parametrize("accepted", [False, True])
def test_valio_substitutes_are_batched_marked_and_filtered(tmp_path, accepted):
    records = [
        product(
            "990001",
            names={"en": "Invented sugar paste"},
            substitutions=[{"product": "990001", "substituteProduct": "990002"}],
        ),
        product("990002", names={"en": "Invented icing"}),
    ]
    outputs = [picks(choice("it_1", "990001", ["990002"] if accepted else [])), EXPLANATION]
    availability = [stock("990001", scheduleLines=[schedule(1)]), stock("990002")]
    with environment(tmp_path, records, outputs, availability=availability) as client:
        detail = start(client, items=[item("sugar paste", 3)])
        assert detail.status == "WAITING_APPROVAL"
        rows = prompt(pick_calls(client)[0])[0]["offers"]
        assert next(row for row in rows if row["product_id"] == "990002")["source"] == "valio"
        assert len(client.app.state.valio.calls) == 2
        assert all(set(call[1]) == {"990001", "990002"} for call in client.app.state.valio.calls)
        variants = client.get(f"/approvals/{detail.approval_id}").json()["options"]
        lines = [line for quote in variants for line in quote["lines"]]
        assert any(line["product_id"] == "990002" for line in lines) is accepted
        if accepted:
            assert any(line["substitute_source"] == "valio" for line in lines)


def test_picked_valio_product_keeps_preference_and_provenance(tmp_path):
    records = [
        product(
            "990001",
            names={"en": "Invented sugar paste"},
            substitutions=[{"product": "990001", "substituteProduct": "990002"}],
        ),
        product("990002", names={"en": "Invented icing"}),
    ]
    outputs = [picks(choice("it_1", "990002", ["990001"])), EXPLANATION]
    with environment(
        tmp_path,
        records,
        outputs,
        availability=[
            stock("990001", scheduleLines=[schedule(1)]),
            stock("990002"),
        ],
    ) as client:
        detail = start(client, items=[item("sugar paste", 3)])
        assert detail.status == "WAITING_APPROVAL"
        assert len(detail.quote.lines) == 1
        line = detail.quote.lines[0]
        assert (line.product_id, line.substitute_for, line.substitute_source) == (
            "990002",
            "990001",
            "valio",
        )


def test_substitute_from_another_candidate_keeps_its_catalogue_origin(tmp_path):
    records = [
        product("990001", names={"en": "Invented sugar paste"}),
        product(
            "990002",
            names={"en": "Invented sugar paste alternate"},
            substitutions=[{"product": "990002", "substituteProduct": "990009"}],
        ),
        product("990009", names={"en": "Invented icing"}),
    ]
    outputs = [picks(choice("it_1", "990001", ["990009"])), EXPLANATION]
    with environment(
        tmp_path,
        records,
        outputs,
        availability=[
            stock("990001", scheduleLines=[schedule(1)]),
            stock("990002", scheduleLines=[]),
            stock("990009"),
        ],
    ) as client:
        detail = start(client, items=[item("sugar paste", 3)])
        assert detail.status == "WAITING_APPROVAL", detail.error
        line = next(line for line in detail.quote.lines if line.product_id == "990009")
        assert line.substitute_for == "990002" and line.substitute_source == "valio"


@pytest.mark.parametrize("accepted", [False, True])
def test_allergen_safe_options_need_explicit_suitability(tmp_path, accepted):
    nuts = ["peanuts", "tree_nuts"]
    records = [
        product("990001", allergens={"may_contain": nuts, "data_available": True}),
        product("990002", allergens={"free_from": nuts, "data_available": True}),
    ]
    outputs = [picks(choice("it_1", "990001", ["990002"] if accepted else [])), EXPLANATION]
    with environment(tmp_path, records, outputs) as client:
        detail = start(client, exclude_allergens=nuts)
        assert detail.status == "WAITING_APPROVAL"
        variants = client.get(f"/approvals/{detail.approval_id}").json()["options"]
        assert bool(variants) is accepted
        if accepted:
            assert any(
                all(line["product_id"] == "990002" for line in quote["lines"]) for quote in variants
            )


@pytest.mark.parametrize("accepted", [False, True])
def test_agent_substitutes_get_one_batched_relevance_call(tmp_path, accepted):
    records = [
        product("990001", names={"en": "Invented sugar paste"}),
        product("990002", names={"en": "Invented icing"}),
        product("990003", names={"en": "Invented cake"}),
        product("990004", names={"en": "Invented gateau"}),
    ]
    initial = picks(choice("it_1", "990001"), choice("it_2", "990003"))
    judged = picks(
        choice("it_1", "990002" if accepted else None),
        choice("it_2", "990004" if accepted else None),
    )
    outputs = [initial, {"query": "icing"}, {"query": "gateau"}, judged]
    if accepted:
        outputs += [EXPLANATION]
    availability = [
        stock(code, scheduleLines=[]) if code in {"990001", "990003"} else stock(code)
        for code in [r.product_id for r in records]
    ]
    with environment(tmp_path, records, outputs, availability=availability) as client:
        detail = start(client, items=[item("sugar paste"), item("cake")])
        calls = pick_calls(client)
        assert len(calls) == 2
        assert [row["item_id"] for row in prompt(calls[1])] == ["it_1", "it_2"]
        if accepted:
            assert detail.status == "WAITING_APPROVAL"
            assert {line.product_id for line in detail.quote.lines} == {"990002", "990004"}
            assert all(line.substitute_source == "agent" for line in detail.quote.lines)
        else:
            assert detail.status == "BLOCKED" and detail.quote is None
            assert not any(o.product_id in {"990002", "990004"} for o in detail.candidates)


@pytest.mark.parametrize("mismatch", [False, True])
async def test_unknown_coverage_has_only_pack_warning(mismatch):
    result = await offer(
        record=product(pack_size={"value": 1, "unit": "kg"} if mismatch else None),
        body=request(unit="pcs" if mismatch else "kg"),
    )
    decision = evaluate([result], {"it_flour": ItemContext()}, mandate(), AGENT)
    assert "PACK_SIZE_UNKNOWN" in {r.rule_id for r in decision.rules_fired}
    assert "PARTIAL_QUANTITY" not in {r.rule_id for r in decision.rules_fired}
    short = await offer(availability=stock(scheduleLines=[schedule(1)]))
    assert "PARTIAL_QUANTITY" in {
        r.rule_id
        for r in evaluate([short], {"it_flour": ItemContext()}, mandate(), AGENT).rules_fired
    }


def test_pena_scaled_reorder_shortage_offers_valio_split(tmp_path):
    # Real committed profile, invented recorded products and fake services.
    with preset_environment(tmp_path, "company_lunch_60") as client:
        original = client.app.state.catalog.lookup("900310")
        replacement = product("990099", names={"en": "Invented layer cake replacement"})
        original.pack_size = replacement.pack_size
        original.substitutions = replacement.__class__.model_validate(
            {
                **original.model_dump(),
                "substitutions": [{"product": "900310", "substituteProduct": "990099"}],
            }
        ).substitutions
        client.app.state.catalog = Catalog([original, replacement])
        from agent.llm import FakeLLM
        from tests.test_offers import FakeValio, price

        client.app.state.valio = FakeValio(
            [price("900310"), price("990099")],
            [stock("900310", scheduleLines=[schedule(1)]), stock("990099")],
        )
        client.app.state.llm = FakeLLM(
            structured_outputs=[
                picks(choice("it_1", "900310", ["990099"])),
                EXPLANATION,
            ]
        )
        response = client.post(
            "/requests",
            json={
                "customer_id": "c_pena",
                "reorder_of": "ph_pena_01",
                "event": {
                    "type": "company_meeting",
                    "guests": 40,
                    "date": "2026-11-21",
                    "dishes": [],
                },
                "requested_date": "2026-11-20",
            },
        )
        assert response.status_code == 202, response.text
        detail = poll(client, response.json()["request_id"])
        assert detail.status == "WAITING_APPROVAL" and detail.intent.based_on.scale == 40 / 30
        assert detail.intent.items[0].quantity == 3
        assert {line.product_id for line in detail.quote.lines} == {"900310", "990099"}
        assert any(line.substitute_source == "valio" for line in detail.quote.lines)


def cake_catalog():
    return Catalog(
        [
            product(
                "990001",
                names={"fi": "Example täytekakku"},
                substitutions=[{"product": "990001", "substituteProduct": "990002"}],
            ),
            product("990002", names={"en": "Example coffee drink"}),
            product("990003", names={"fi": "Example peruna"}),
            product(
                "990004",
                names={"en": "Example oat drink iKaffe 1L"},
                pack_size={"value": 1, "unit": "L"},
            ),
        ]
    )


@pytest.mark.parametrize("companion_unit", ["kg", "L", None])
def test_offline_finder_reorder_shortage_requires_same_pack_unit_for_any_product_type(
    companion_unit,
):
    catalog = cake_catalog()
    companion = catalog.lookup("990002")
    companion.pack_size = (
        companion.pack_size.model_copy(update={"unit": companion_unit}) if companion_unit else None
    )
    rows = [row for row in finder.find_cases(catalog) if row["case"] == "reorder_shortage"]
    if companion_unit != "kg":
        assert rows == []
        return
    assert len(rows) == 1
    assert rows[0]["product_id"] == "990001" and rows[0]["companion_product_id"] == "990002"
    assert rows[0]["quantity_sales_units"] == 6 and rows[0]["status"] == "catalogue_candidate"
    assert rows[0]["history_product_id"] == "900310" and rows[0]["unit"] == "kg"
    assert "observed" not in rows[0]


@pytest.mark.parametrize(
    "amount,replacement_stock,priced,verified",
    [
        (1, 100, True, True),
        (0, 100, True, True),
        (6, 100, True, False),
        (1, 0, True, False),
        (1, 100, False, False),
    ],
)
async def test_finder_live_checks_with_fake_valio_only(
    monkeypatch,
    amount,
    replacement_stock,
    priced,
    verified,
):
    class OfflineValio(FakeValio):
        def __init__(self, settings):
            super().__init__(
                [price("990001"), price("990002", error=not priced)],
                [
                    stock("990001", scheduleLines=[schedule(amount)]),
                    stock("990002", scheduleLines=[schedule(replacement_stock)]),
                ],
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def ping(self):
            pass

    monkeypatch.setattr(finder, "ValioClient", OfflineValio)
    catalog = cake_catalog()
    rows = [row for row in finder.find_cases(catalog) if row["case"] == "reorder_shortage"]
    await finder.verify_live(rows, catalog, Settings(valio_api_key=""), REQUESTED)
    assert (rows[0]["status"] == "verified") is verified


@pytest.mark.parametrize(
    "live,status,expected",
    [
        (True, "verified", "990002"),
        (False, "verified", "990001"),
        (True, "catalogue_candidate", "990001"),
    ],
)
def test_pena_seed_override_uses_only_first_verified_live_case(tmp_path, live, status, expected):
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / "c_pena.json").write_bytes((CUSTOMERS_DIR / "c_pena.json").read_bytes())
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            {
                "live": live,
                "cases": [
                    {
                        "case": "reorder_shortage",
                        "status": status,
                        "product_id": "990002",
                        "history_product_id": "900310",
                    },
                    {
                        "case": "reorder_shortage",
                        "status": status,
                        "product_id": "990001",
                        "history_product_id": "900310",
                    },
                ],
            }
        )
    )
    assert resolve_customers(
        cake_catalog(), customers_dir=seeds, out=tmp_path / "out", demo_cases=cases
    ) == (1, 2)
    customer = load_customers(customers_dir=tmp_path / "out", overlay_dir=None)["c_pena"]
    assert [line.product_id for order in customer.order_history for line in order.lines] == [
        expected,
        "990004",
    ]
    assert (
        json.loads((seeds / "c_pena.json").read_bytes())["order_history"][0]["lines"][0][
            "product_id"
        ]
        == "900310"
    )
