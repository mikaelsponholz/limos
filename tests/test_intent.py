"""Offline text resolution and HTTP flows with invented history and products."""

import json
from datetime import UTC, date, datetime
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue
from starlette.requests import Request

from agent.intent import TextFacts, helsinki_today, parse_text, resolve_request
from agent.llm import FakeLLM, GeminiLLM
from gateway.api.dependencies import Resources
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.runner import AGENT_ID
from gateway.schemas import Customer, RequestCreate, RequestDetail
from gateway.settings import Settings
from gateway.store import MemoryStore
from tests.engine_support import BUYER, mandate
from tests.test_e2e import poll
from tests.test_offers import NOW, FakeValio, price, product, stock

TODAY = date(2026, 11, 14)


def profile() -> Customer:
    return Customer.model_validate_json(
        json.dumps(
            {
                "customer_id": "invented-customer",
                "name": "Invented Buyer",
                "company": "Invented Kitchen",
                "role": "Executive",
                "buyer_id": BUYER,
                "age_group": "55_plus",
                "playful": False,
                "preferences": [{"key": "organic", "level": "prefer"}],
                "exclude_allergens": ["tree_nuts"],
                "defaults": {"priority": "quality", "typical_guests": 50, "budget": 200.0},
                "order_history": [
                    {
                        "order_ref": ref,
                        "date": day,
                        "title": title,
                        "event_type": kind,
                        "guests": guests,
                        "lines": [
                            {
                                "product_id": "900701",
                                "name": "Invented pantry flour",
                                "quantity_sales_units": quantity,
                                "sales_unit": "PRK",
                                "quantity_text": f"{quantity} x 1 kg",
                                "unit_price": 3.35,
                                "line_total": round(quantity * 3.35, 2),
                            }
                        ],
                        "total": round(quantity * 3.35, 2),
                        "source": "seeded",
                        "simulated": True,
                    }
                    for ref, day, title, kind, guests, quantity in [
                        (
                            "invented-board",
                            "2026-10-01",
                            "Invented board meeting",
                            "company_meeting",
                            30,
                            2.25,
                        ),
                        (
                            "invented-coffee",
                            "2026-11-01",
                            "Invented team coffee",
                            "staff_coffee",
                            20,
                            5.0,
                        ),
                    ]
                ],
                "simulated": True,
            }
        )
    )


def request(**values: JsonValue) -> RequestCreate:
    return RequestCreate.model_validate_json(
        json.dumps(
            {
                "buyer_id": BUYER,
                "text": "Invented board lunch",
                **values,
            }
        )
    )


def facts(**values: JsonValue) -> TextFacts:
    return parse_text(FakeLLM(structured_outputs=[values]), "Invented request", TODAY).value


def gap_sources(detail: RequestDetail) -> dict[str, str]:
    gaps = detail.intent.filled_gaps
    assert len({gap.field for gap in gaps}) == len(gaps)
    return {gap.field: gap.source for gap in gaps}


def test_form_wins_over_text_including_explicit_empty_lists_and_null_budget() -> None:
    body = request(
        requested_date="2026-11-20",
        budget=None,
        priority="balanced",
        preferences=[],
        exclude_allergens=[],
        items=[],
        event={"type": "form_meeting", "date": "2026-11-21", "guests": 70, "dishes": []},
    )
    parsed = facts(
        event_type="wedding",
        event_date="2026-12-01",
        guests=120,
        deliver_by="2026-11-30",
        budget=900.0,
        priority="price",
        dishes=[{"name": "Invented cake"}],
        items=[{"description": "flour", "quantity": 2.0, "unit": "kg"}],
        preferences=[{"key": "vegan", "level": "require"}],
        exclude_allergens=["milk"],
    )
    resolved = resolve_request(body, profile(), parsed, TODAY)
    assert resolved.intent.event is not None
    assert (resolved.intent.event.type, resolved.intent.event.guests) == ("form_meeting", 70)
    assert resolved.intent.event.date == date(2026, 11, 21)
    assert resolved.intent.deliver_by == date(2026, 11, 20)
    assert resolved.intent.budget is None and resolved.intent.priority == "balanced"
    assert resolved.intent.preferences == resolved.intent.exclude_allergens == []
    assert resolved.intent.dishes == resolved.intent.items == []
    assert resolved.intent.filled_gaps == []


def test_long_text_beats_history_and_profile_and_preserves_input() -> None:
    text = (
        "Wedding for 120 guests on 21 November, delivery 20 November. "
        "Wedding cake and sandwiches, lactose free required, budget 900 EUR, balanced."
    )
    llm = FakeLLM(
        structured_outputs=[
            {
                "event_type": "wedding",
                "event_date": "2026-11-21",
                "guests": 120,
                "dishes": [{"name": "Wedding cake", "servings": 120}, {"name": "Sandwiches"}],
                "preferences": [{"key": "lactose_free", "level": "require"}],
                "exclude_allergens": ["milk"],
                "budget": 900.0,
                "priority": "balanced",
                "deliver_by": "2026-11-20",
            }
        ]
    )
    body, customer = request(text=text), profile()
    before = (body.model_dump(), customer.model_dump())
    parsed = parse_text(llm, text, TODAY)
    resolved = resolve_request(body, customer, parsed.value, TODAY)
    intent = resolved.intent
    assert intent.event is not None and intent.event.guests == 120
    assert intent.event.type == "wedding" and intent.event.date == date(2026, 11, 21)
    assert [dish.name for dish in intent.dishes] == ["Wedding cake", "Sandwiches"]
    assert [dish.servings for dish in intent.dishes] == [120, 120]
    assert intent.budget == 900 and intent.priority == "balanced"
    assert [(p.key, p.level) for p in intent.preferences] == [("lactose_free", "require")]
    assert intent.exclude_allergens == ["milk"]
    assert all(
        gap.source == "text" for gap in intent.filled_gaps if not gap.field.endswith(".servings")
    )
    assert next(g for g in intent.filled_gaps if g.field.endswith(".servings")).source == "default"
    assert len({gap.field for gap in intent.filled_gaps}) == len(intent.filled_gaps)
    assert (body.model_dump(), customer.model_dump()) == before
    assert parsed.model_id == "fake" and llm.calls[0].model_id == "fake"
    prompt = llm.calls[0].contents[0].parts[0].text
    assert prompt is not None and text in prompt and TODAY.isoformat() in prompt
    for private in ("Invented Buyer", "invented-board", "900701", "Invented pantry flour"):
        assert private not in prompt and private not in (
            llm.calls[0].config.system_instruction or ""
        )


@pytest.mark.parametrize("source,guests", [("history", 30), ("profile", 50), ("default", 30)])
def test_vague_text_history_then_profile_then_defaults(source: str, guests: int) -> None:
    customer = profile() if source != "default" else None
    if source == "profile":
        assert customer is not None
        customer.order_history.clear()
    resolved = resolve_request(request(), customer, facts(event_type="company_meeting"), TODAY)
    assert resolved.intent.event is not None and resolved.intent.event.guests == guests
    gaps = {gap.field: gap for gap in resolved.intent.filled_gaps}
    assert len(gaps) == len(resolved.intent.filled_gaps)
    assert gaps["event.guests"].source == source
    assert gaps["deliver_by"].source == "default"
    assert gaps["priority"].source == ("default" if customer is None else "profile")
    if source == "history":
        assert "Invented board meeting" in gaps["event.guests"].note
        assert gaps["budget"].source == gaps["preferences.organic"].source == "profile"


def test_explicit_item_text_has_no_event_and_applies_requirements() -> None:
    resolved = resolve_request(
        request(),
        None,
        facts(
            items=[{"description": "flour", "quantity": 2.0, "unit": "kg"}],
            preferences=[{"key": "gluten_free", "level": "require"}],
            exclude_allergens=["milk"],
        ),
        TODAY,
    )
    assert resolved.intent.event is None and resolved.intent.dishes == []
    assert resolved.intent.items[0].constraints == ["gluten_free", "no_milk"]
    assert {g.field: g.source for g in resolved.intent.filled_gaps}["items"] == "text"


@pytest.mark.parametrize("day", ["2026-11-13", "2027-11-15"])
def test_invalid_text_values_are_dropped_with_assumptions(day: str) -> None:
    parsed = facts(
        event_date=day,
        deliver_by=day,
        guests=0,
        budget=-5.0,
        items=[{"description": "flour", "quantity": 0.0, "unit": "kg"}],
        dishes=[{"name": "Invented cake", "servings": -1}],
    )
    resolved = resolve_request(request(), profile(), parsed, TODAY)
    assert resolved.intent.event is not None and resolved.intent.event.guests == 20
    assert resolved.intent.event.date == resolved.intent.deliver_by == date(2026, 11, 15)
    assert resolved.intent.budget == 200 and resolved.intent.items == []
    assert len(resolved.intent.assumptions) == 6
    assert all("Ignored text" in note for note in resolved.intent.assumptions)
    assert not any(g.source == "text" and g.field != "dishes" for g in resolved.intent.filled_gaps)
    assert parsed.guests == 0 and parsed.budget == -5.0  # Validation is pure.


@pytest.mark.parametrize("with_customer", [False, True])
def test_reorder_hint_without_history_is_an_assumption(with_customer: bool) -> None:
    customer = profile() if with_customer else None
    if customer:
        customer.order_history.clear()
    resolved = resolve_request(request(), customer, facts(reorder={"repeat": True}), TODAY)
    assert resolved.reorder_target is None
    assert len(resolved.intent.assumptions) == 1
    assert "No past order" in resolved.intent.assumptions[0]


def client_for(tmp_path: Path, llm: FakeLLM | None, *, now: datetime = NOW) -> TestClient:
    customer = profile()
    customer.exclude_allergens.clear()
    customer.preferences.clear()
    (tmp_path / "invented-customer.json").write_text(customer.model_dump_json(), encoding="utf-8")
    ids = count(1)
    return TestClient(
        create_app(
            Settings(store="memory", gemini_api_key="", gemini_model="", valio_api_key=""),
            llm=llm,
            store=MemoryStore(),
            catalog=Catalog([product("900701")]),
            valio=FakeValio([price("900701")], [stock("900701")]),
            clock=lambda: now,
            id_generator=lambda: f"invented_{next(ids)}",
            mandates={BUYER: mandate(allowed_agents=(AGENT_ID,))},
            customers_dir=tmp_path,
            customer_overlay_dir=tmp_path / "overlays",
        )
    )


def post_text(client: TestClient, **body: JsonValue) -> RequestDetail:
    response = client.post("/requests", json={"buyer_id": BUYER, "text": "Invented order", **body})
    assert response.status_code == 202, response.text
    return poll(client, response.json()["request_id"])


@pytest.mark.parametrize(
    "hint,target,units,scale",
    [
        ({"repeat": True}, "invented-coffee", 20, 4.0),
        ({"repeat": True, "event_type": "company_meeting"}, "invented-board", 6, 80 / 30),
        ({"repeat": True, "title_words": ["board"]}, "invented-board", 6, 80 / 30),
        ({"repeat": True, "title_words": ["unmatched"]}, "invented-coffee", 20, 4.0),
    ],
)
def test_text_reorder_uses_code_scaling_and_customer_history(
    tmp_path: Path, hint: dict[str, JsonValue], target: str, units: int, scale: float
) -> None:
    llm = FakeLLM(structured_outputs=[{"guests": 80, "reorder": hint}])
    with client_for(tmp_path, llm) as client:
        detail = post_text(
            client,
            text="Same as last time but for 80 people",
            customer_id="invented-customer",
            requested_date="2026-11-20",
        )
        assert detail.status == "ORDERED", detail.error
        assert detail.quote is not None and detail.intent.based_on is not None
        assert detail.intent.based_on.order_ref == target
        assert detail.intent.based_on.guests_now == 80 and detail.intent.based_on.scale == scale
        assert detail.quote.lines[0].quantity_sales_units == units
        sources = gap_sources(detail)
        assert sources["reorder_of"] == "history" and sources["event.guests"] == "text"
        step = next(step for step in detail.steps if step.type == "INTENT_PARSED")
        assert step.actor == "agent" and step.model == "fake"
        assert "event.guests" in step.detail["text_fields"]
        assert sum(step.type == "INTENT_PARSED" for step in detail.steps) == 1
        assert verify_chain(detail.steps)


def test_text_items_run_existing_order_path_and_helsinki_date(tmp_path: Path) -> None:
    llm = FakeLLM(
        structured_outputs=[
            {
                "items": [{"description": "flour", "quantity": 2.0, "unit": "kg"}],
                "deliver_by": "2026-11-20",
            }
        ]
    )
    with client_for(tmp_path, llm, now=datetime(2026, 11, 13, 23, tzinfo=UTC)) as client:
        detail = post_text(client, text="2 kg flour by 20 November")
        assert detail.status == "ORDERED", detail.error
        assert detail.quote is not None and detail.quote.lines[0].quantity_sales_units == 2
        assert gap_sources(detail)["items"] == "text"
        prompt = llm.calls[0].contents[0].parts[0].text
        assert prompt is not None and TODAY.isoformat() in prompt


@pytest.mark.parametrize(
    "outputs,code",
    [
        (
            [{"event_type": "wedding", "guests": 120, "event_date": "2026-11-20"}],
            "AGENT_NOT_AVAILABLE",
        ),
        ([{"guests": "many"}, {"guests": "many"}], "LLM_OUTPUT_ERROR"),
        ([{"exclude_allergens": ["invented-code"]}] * 2, "LLM_OUTPUT_ERROR"),
    ],
)
def test_unavailable_expansion_and_invalid_output_fail_clearly(
    tmp_path: Path, outputs: list[dict[str, JsonValue]], code: str
) -> None:
    llm = FakeLLM(structured_outputs=outputs)
    with client_for(tmp_path, llm) as client:
        detail = post_text(client)
        assert detail.status == "FAILED" and detail.error is not None
        assert detail.error.code == code and detail.question is None
        if code == "AGENT_NOT_AVAILABLE":
            assert detail.intent.event is not None
            assert any(step.model == "fake" for step in detail.steps)
        else:
            assert len(llm.calls) == 2
        assert verify_chain(detail.steps)


def test_text_without_llm_fails_with_unavailable_code(tmp_path: Path) -> None:
    with client_for(tmp_path, None) as client:
        detail = post_text(client)
        assert detail.status == "FAILED" and detail.error is not None
        assert detail.error.code == "AGENT_NOT_AVAILABLE"


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-01-01T21:30:00+00:00", "2026-01-01"),
        ("2026-01-01T22:30:00+00:00", "2026-01-02"),
        ("2026-07-01T21:30:00+00:00", "2026-07-02"),
        ("2026-03-28T21:30:00+00:00", "2026-03-28"),
        ("2026-03-29T21:30:00+00:00", "2026-03-30"),
        ("2026-10-24T21:30:00+00:00", "2026-10-25"),
        ("2026-10-25T21:30:00+00:00", "2026-10-25"),
    ],
)
def test_helsinki_today_without_iana_database(
    monkeypatch: pytest.MonkeyPatch, instant: str, expected: str
) -> None:
    from zoneinfo import ZoneInfoNotFoundError

    def missing_zone(key: str) -> None:
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr("agent.intent.ZoneInfo", missing_zone)
    assert helsinki_today(datetime.fromisoformat(instant)) == date.fromisoformat(expected)


def test_text_validation_repairs_duplicate_preferences() -> None:
    llm = FakeLLM(
        structured_outputs=[
            {
                "preferences": [
                    {"key": "organic", "level": "require"},
                    {"key": "organic", "level": "prefer"},
                ]
            },
            {"preferences": [{"key": "organic", "level": "require"}]},
        ]
    )
    parsed = parse_text(llm, "Require organic", TODAY)
    assert len(llm.calls) == 2 and len(parsed.value.preferences) == 1
    resolved = resolve_request(request(), profile(), parsed.value, TODAY)
    gaps = [gap for gap in resolved.intent.filled_gaps if gap.field == "preferences.organic"]
    assert len(gaps) == 1 and gaps[0].source == "text"


@pytest.mark.parametrize(
    "key,model,configured",
    [
        ("", "", False),
        ("unused-test-value", "", False),
        ("", "fake", False),
        ("unused-test-value", "fake", True),
    ],
)
def test_llm_is_lazy_and_requires_both_settings(key: str, model: str, configured: bool) -> None:
    app = create_app(Settings(gemini_api_key=key, gemini_model=model))
    assert app.state.llm is None
    env = Resources(Request({"type": "http", "app": app}))
    llm = env.llm
    if configured:
        assert isinstance(llm, GeminiLLM) and llm._client is None
        assert env.llm is llm
    else:
        assert llm is None
