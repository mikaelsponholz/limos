"""Offline event expansion, exact arithmetic and gateway integration."""

import json
from datetime import date
from fractions import Fraction
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from agent.expansion import EventExpansion, build_event_items, expand_event
from agent.llm import FakeLLM, LLMOutputError
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.runner import AGENT_ID
from gateway.schemas import Event, EventDish, IntentItem, Preference, Unit
from gateway.settings import Settings
from gateway.store import MemoryStore
from scripts.eval_expansion import Evaluation, format_report, main
from tests.agent_loop_support import EXPLANATION, bind_client, purchase
from tests.engine_support import BUYER, mandate
from tests.test_e2e import poll
from tests.test_offers import NOW, FakeValio, price, product, stock


def event(guests: int = 10) -> Event:
    return Event(type="invented_lunch", date=date(2026, 11, 21), guests=guests)


def ingredient(
    quantity: float, unit: Unit = "g", description: str = "flour"
) -> dict[str, JsonValue]:
    return {
        "description": description,
        "description_fi": "jauho",
        "quantity_per_serving": quantity,
        "unit": unit,
    }


def proposal(*dishes: tuple[str, list[dict[str, JsonValue]]]) -> dict[str, JsonValue]:
    return {"dishes": [{"name": name, "ingredients": items} for name, items in dishes]}


def output(*dishes: tuple[str, list[dict[str, JsonValue]]]) -> EventExpansion:
    return EventExpansion.model_validate_json(json.dumps(proposal(*dishes)))


def test_scaling_default_waste_normalisation_and_merged_shares() -> None:
    dishes = [EventDish(name="Cake", servings=4), EventDish(name="Bread", servings=None)]
    expanded = build_event_items(
        output(("Cake", [ingredient(100)]), ("Bread", [ingredient(0.02, "kg", " FLOUR  ")])),
        event(),
        dishes,
    )
    assert [dish.servings for dish in expanded.dishes] == [4, 10]
    assert len(expanded.items) == 1
    item = expanded.items[0]
    assert (item.item_id, item.quantity, item.unit) == ("it_1", 0.63, "kg")
    assert [(use.dish_id, use.quantity) for use in item.used_in] == [("d_1", 0.42), ("d_2", 0.21)]
    assert expanded.assumptions == ["5 % waste added to expanded ingredients."]
    assert dishes[1].servings is None  # Pure conversion leaves input untouched.


@pytest.mark.parametrize("waste,expected", [(None, 1.05), (0, 1.0), (12.5, 1.125)])
def test_mandate_waste_and_default(waste: float | None, expected: float) -> None:
    expanded = build_event_items(
        output(("Cake", [ingredient(100)])),
        event(),
        [EventDish(name="Cake", servings=None)],
        waste_percent=waste,
    )
    assert expanded.items[0].quantity == expected


def test_volume_normalisation_and_unit_families_stay_separate() -> None:
    expanded = build_event_items(
        output(
            ("Cake", [ingredient(25, "ml", "cream"), ingredient(0.01, "kg", "cream")]),
            ("Bread", [ingredient(0.025, "L", "cream")]),
        ),
        event(),
        [EventDish(name=name, servings=None) for name in ("Cake", "Bread")],
    )
    assert [(item.quantity, item.unit) for item in expanded.items] == [(0.525, "L"), (0.105, "kg")]
    assert [use.quantity for use in expanded.items[0].used_in] == [0.263, 0.262]


@pytest.mark.parametrize("unit,expected", [("kg", 0.002), ("L", 0.002), ("pcs", 1.0)])
def test_round_once_after_merging_and_apportion_shares(unit: Unit, expected: float) -> None:
    expanded = build_event_items(
        output(("Cake", [ingredient(0.0006, unit)]), ("Bread", [ingredient(0.0006, unit)])),
        event(1),
        [EventDish(name=name, servings=None) for name in ("Cake", "Bread")],
        waste_percent=0,
    )
    item = expanded.items[0]
    assert item.quantity == expected
    assert sum(Fraction(str(use.quantity)) for use in item.used_in) == Fraction(str(expected))


def test_explicit_items_merge_without_waste_and_keep_constraints() -> None:
    explicit = IntentItem(
        item_id="it_1",
        description=" Flour ",
        description_fi="jauho",
        quantity=100.0,
        unit="g",
        constraints=["organic"],
        used_in=[],
    )
    result = build_event_items(
        output(("Cake", [ingredient(100)])),
        event(),
        [EventDish(name="Cake", servings=None)],
        [
            Preference(key="lactose_free", level="require"),
            Preference(key="finnish_origin", level="require"),
            Preference(key="vegan", level="prefer"),
        ],
        ["tree_nuts"],
        explicit_items=[explicit],
    )
    assert len(result.items) == 1
    item = result.items[0]
    assert item.quantity == 1.15 and item.unit == "kg"
    assert item.constraints == ["organic", "lactose_free", "no_tree_nuts"]
    assert [(use.dish_id, use.quantity) for use in item.used_in] == [("d_1", 1.05)]
    assert explicit.quantity == 100 and explicit.unit == "g"


@pytest.mark.parametrize("unit,amount", [("kg", 2.0), ("L", 2.0), ("pcs", 11.0)])
def test_sanity_notes_do_not_cap_quantities(unit: Unit, amount: float) -> None:
    result = build_event_items(
        output(("Cake", [ingredient(amount, unit)])),
        event(),
        [EventDish(name="Cake", servings=None)],
        waste_percent=0,
    )
    assert result.items[0].quantity == amount * 10
    assert any("per guest" in note and "unchanged" in note for note in result.assumptions)


def test_unasked_empty_missing_and_bad_quantities_are_dropped_and_noted() -> None:
    result = build_event_items(
        output(
            ("Unasked", [ingredient(1)]),
            ("Cake", [ingredient(0), ingredient(-4), ingredient(10)]),
            ("Empty", []),
            ("Invalid", [ingredient(0)]),
            ("Blank", [ingredient(2, description=" ")]),
            ("Cake", [ingredient(1)]),  # A repeated response cannot add an extra dish.
        ),
        event(),
        [
            EventDish(name=name, servings=None)
            for name in ("Cake", "Empty", "Invalid", "Blank", "Missing")
        ],
    )
    assert [dish.name for dish in result.dishes] == ["Cake"]
    assert len(result.items) == 1 and result.items[0].quantity == 0.105
    notes = "\n".join(result.assumptions)
    for fragment in ("unasked dish", "empty dish", "missing dish", "quantity must be positive"):
        assert fragment in notes
    assert "descriptions must not be empty" in notes


def test_expansion_model_selection_minimal_prompt_and_repair_metadata() -> None:
    settings = Settings(gemini_model="fake-general", gemini_expansion_model="fake-expansion")
    llm = FakeLLM(
        settings=settings,
        structured_outputs=[{"dishes": "invalid"}, proposal(("Cake", [ingredient(1)]))],
    )
    result = expand_event(
        llm,
        event(),
        [EventDish(name="Cake", servings=None)],
        [Preference(key="lactose_free", level="require")],
        ["tree_nuts"],
    )
    assert result.model_id == "fake-expansion" and result.repaired
    assert len(llm.calls) == 2
    assert {call.model_id for call in llm.calls} == {"fake-expansion"}
    prompt = llm.calls[0].contents[0].parts[0].text
    assert prompt is not None
    assert json.loads(prompt.split("\nReturn JSON")[0]) == {
        "event_type": "invented_lunch",
        "guests": 10,
        "dishes": [{"name": "Cake", "servings": None}],
        "preferences": ["lactose_free"],
        "exclude_allergens": ["tree_nuts"],
    }
    for private in ("catalogue", "history", "900101", "buyer_id", "2026-11-21"):
        assert private not in prompt


@pytest.mark.parametrize(
    "bad",
    [
        proposal(("Cake", [ingredient(1, "kg")] * 13)),
        {"dishes": [{"name": "Cake", "ingredients": [{**ingredient(1), "unit": "box"}]}]},
        proposal(("Cake", [ingredient(float("inf"))])),
        {"dishes": [{"name": "Cake", "ingredients": []}] * 25},
    ],
)
def test_invalid_units_nonfinite_numbers_and_unbounded_output_fail(
    bad: dict[str, JsonValue],
) -> None:
    llm = FakeLLM(structured_outputs=[bad, bad])
    with pytest.raises(LLMOutputError):
        expand_event(llm, event(), [EventDish(name="Cake", servings=None)], [], [])
    assert len(llm.calls) == 2


def client_for(tmp_path: Path, llm: FakeLLM | None, waste: float = 10) -> TestClient:
    ids = count(1)
    client = TestClient(
        create_app(
            Settings(
                agent_mode="tools", gemini_api_key="", gemini_model="", gemini_expansion_model=""
            ),
            catalog=Catalog([product()]),
            valio=FakeValio([price()], [stock()]),
            store=MemoryStore(),
            clock=lambda: NOW,
            id_generator=lambda: f"invented_{next(ids)}",
            mandates={BUYER: mandate(allowed_agents=(AGENT_ID,), waste_percent=waste)},
            customers_dir=tmp_path,
            customer_overlay_dir=tmp_path / "overlays",
            llm=llm,
        )
    )
    return bind_client(client, llm)


@pytest.mark.parametrize("from_text", [False, True])
@pytest.mark.parametrize("with_explicit", [False, True])
def test_event_runs_to_search_and_quote_with_explicit_items(
    tmp_path: Path, from_text: bool, with_explicit: bool
) -> None:
    outputs = []
    if from_text:
        outputs.append({"event_type": "invented_lunch", "guests": 10, "dishes": [{"name": "Cake"}]})
    outputs.append(proposal(("Cake", [ingredient(200)])))
    outputs.append(EXPLANATION)
    llm = FakeLLM(structured_outputs=outputs, tool_responses=purchase("900101"))
    body: dict[str, JsonValue] = {
        "buyer_id": BUYER,
        "requested_date": "2026-11-20",
    }
    if with_explicit:
        body["items"] = [{"description": "flour", "quantity": 800, "unit": "g", "constraints": []}]
    if from_text:
        body["text"] = "Invented lunch for ten with cake"
    else:
        body["event"] = {
            **event().model_dump(mode="json"),
            "dishes": [{"name": "Cake", "servings": None}],
        }
    with client_for(tmp_path, llm) as client:
        response = client.post("/requests", json=body)
        assert response.status_code == 202, response.text
        result = poll(client, response.json()["request_id"])
    assert result.status == ("ORDERED" if with_explicit else "WAITING_APPROVAL"), result.error
    assert result.quote is not None and len(result.quote.lines) == 1
    assert len(result.intent.items) == 1
    assert result.intent.items[0].quantity == (3 if with_explicit else 2.2)
    assert result.intent.items[0].used_in[0].quantity == 2.2
    expanded = next(step for step in result.steps if step.type == "EVENT_EXPANDED")
    assert expanded.actor == "agent" and expanded.model == "fake"
    assert expanded.detail == {"dish_count": 1, "item_count": 1}
    assert any(step.detail.get("tool") == "search_products" for step in result.steps)
    assert verify_chain(result.steps)


@pytest.mark.parametrize("answer,guests", [("60", 60), (None, 30)])
def test_expansion_waits_for_answer_then_uses_resolved_servings(
    tmp_path: Path, answer: str | None, guests: int
) -> None:
    llm = FakeLLM(
        structured_outputs=[
            {"event_type": "invented_lunch", "dishes": [{"name": "Cake"}]},
            {"text": "How many guests?"},
            proposal(("Cake", [ingredient(100)])),
            EXPLANATION,
        ],
        tool_responses=purchase("900101"),
    )
    with client_for(tmp_path, llm, waste=0) as client:
        response = client.post(
            "/requests",
            json={"buyer_id": BUYER, "text": "Cake for lunch", "requested_date": "2026-11-20"},
        )
        detail = poll(client, response.json()["request_id"])
        assert detail.status == "NEEDS_INPUT" and detail.question is not None
        assert len(llm.calls) == 2
        assert not any(step.type == "EVENT_EXPANDED" for step in detail.steps)
        response = client.post(
            f"/requests/{detail.request_id}/answer",
            json={"question_id": detail.question.question_id, "answer": answer},
        )
        assert response.status_code == 202, response.text
        result = poll(client, detail.request_id)
    assert result.quote is not None and result.intent.items[0].quantity == guests / 10
    assert result.intent.dishes[0].servings == guests
    assert sum(step.type == "CLARIFICATION_REQUESTED" for step in result.steps) == 1
    assert sum(step.type == "EVENT_EXPANDED" for step in result.steps) == 1
    assert len(llm.calls) == 8 and verify_chain(result.steps)


@pytest.mark.parametrize("mode", ["no_llm", "invalid", "empty"])
def test_event_fails_clearly_without_llm_or_valid_output(tmp_path: Path, mode: str) -> None:
    llm = (
        None
        if mode == "no_llm"
        else FakeLLM(structured_outputs=[{"dishes": "invalid"}] * 2)
        if mode == "invalid"
        else FakeLLM(structured_outputs=[proposal(("Unasked", [ingredient(1)]))])
    )
    with client_for(tmp_path, llm) as client:
        response = client.post(
            "/requests",
            json={
                "buyer_id": BUYER,
                "requested_date": "2026-11-20",
                "event": {
                    **event().model_dump(mode="json"),
                    "dishes": [{"name": "Cake", "servings": None}],
                },
            },
        )
        result = poll(client, response.json()["request_id"])
    assert result.status == "FAILED" and result.error is not None
    assert (
        result.error.code
        == {
            "no_llm": "AGENT_NOT_AVAILABLE",
            "invalid": "LLM_OUTPUT_ERROR",
            "empty": "NO_EVENT_ITEMS",
        }[mode]
    )
    assert result.quote is None and verify_chain(result.steps)


def test_report_formats_items_retry_failure_and_skipped_item_preset() -> None:
    items = build_event_items(
        output(("Cake", [ingredient(100)])), event(), [EventDish(name="Cake", servings=None)]
    ).items
    report = format_report(
        [
            Evaluation("invented_event", "fake", items, ["Check amount."], 1.2345, "repair retry"),
            Evaluation("invented_event", "fake-other", validation="failed after repair retry"),
        ],
        ["invented_item_list"],
    )
    for text in (
        "Skipped invented_item_list: item-list preset, no event.",
        "| invented_event | fake | 1 | 1.234 | repair retry |",
        "flour (jauho): 1.05 kg",
        "Check amount.",
        "failed after repair retry",
    ):
        assert text in report


def test_live_eval_requires_environment_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_MODEL", "")
    assert main(["--models", "fake", "fake-other"]) == 1
    assert "Set GEMINI_API_KEY and GEMINI_MODEL" in capsys.readouterr().out
