"""Pipeline regression checks use invented products and offline structured outputs."""

import json
from datetime import date
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.llm import FakeLLM
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.runner import AGENT_ID
from gateway.settings import Settings
from gateway.store import MemoryStore
from gateway.valio.client import ValioTransportError
from tests.engine_support import BUYER, mandate
from tests.test_e2e import poll
from tests.test_offers import NOW, FakeValio, price, product, schedule, stock

EXPLANATION = {"text": "The selected products cover the available items. The quote is ready."}


def environment(tmp_path, records, outputs, *, availability=None, playful=False, approval=False):
    if playful:
        profile = {
            "customer_id": "invented-chef",
            "name": "Invented Chef",
            "company": "Invented Kitchen",
            "role": "Chef",
            "buyer_id": BUYER,
            "age_group": "under_35",
            "playful": True,
            "preferences": [],
            "exclude_allergens": [],
            "defaults": {"priority": "balanced", "typical_guests": None, "budget": None},
            "order_history": [],
            "simulated": True,
        }
        (tmp_path / "invented-chef.json").write_text(json.dumps(profile))
    llm = FakeLLM(structured_outputs=outputs)
    valio = FakeValio(
        [price(record.product_id) for record in records],
        availability
        if availability is not None
        else [stock(record.product_id) for record in records],
    )
    ids = count(1)
    app = create_app(
        Settings(
            agent_mode="pipeline",
            store="memory",
            gemini_api_key="",
            gemini_model="",
            valio_api_key="",
            database_url="",
        ),
        llm=llm,
        catalog=Catalog(records),
        valio=valio,
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={BUYER: mandate(allowed_agents=(AGENT_ID,), always_require_approval=approval)},
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )
    return TestClient(app)


def item(description="flour", quantity=2.0):
    return {"description": description, "quantity": quantity, "unit": "kg", "constraints": []}


def picks(*ids):
    return {
        "choices": [
            {"item_id": f"it_{index}", "product_id": code} for index, code in enumerate(ids, 1)
        ]
    }


def start(client, **changes):
    response = client.post(
        "/requests",
        json={
            "buyer_id": BUYER,
            "requested_date": "2026-11-20",
            "items": [item()],
            **changes,
        },
    )
    assert response.status_code == 202, response.text
    detail = poll(client, response.json()["request_id"])
    assert verify_chain(detail.steps)
    return detail


def codes(detail):
    return [step.detail.get("code") for step in detail.steps if step.type in {"ERROR", "EXCEPTION"}]


def test_fourteen_item_event_uses_two_purchase_calls_and_one_wire_batch(tmp_path: Path):
    records = [product(f"990{i:03}", names={"en": f"Invented ingredient{i:02}"}) for i in range(14)]
    expansion = {
        "dishes": [
            {
                "name": name,
                "ingredients": [
                    {
                        "description": f"ingredient{i:02}",
                        "description_fi": f"ingredient{i:02}",
                        "quantity_per_serving": 0.1,
                        "unit": "kg",
                        "search_terms": [f"ingredient{i:02}"],
                    }
                    for i in indexes
                ],
            }
            for name, indexes in [("Invented main", range(7)), ("Invented side", range(7, 14))]
        ]
    }
    with environment(
        tmp_path, records, [expansion, picks(*(r.product_id for r in records)), EXPLANATION]
    ) as client:
        detail = start(
            client,
            items=[],
            event={
                "type": "lunch",
                "date": "2026-11-21",
                "guests": 10,
                "dishes": [
                    {"name": "Invented main", "servings": None},
                    {"name": "Invented side", "servings": None},
                ],
            },
        )
        assert detail.error is None and detail.quote is not None
        assert len(detail.quote.lines) == 14
        calls = client.app.state.llm.calls
        properties = [call.config.response_json_schema["properties"] for call in calls]
        assert sum("dishes" in schema for schema in properties) == 1
        assert sum("choices" in schema for schema in properties) == 1
        wire = client.app.state.valio.calls
        assert [call[0] for call in wire] == ["price", "availability"]
        assert all(len(call[1]) == 14 for call in wire)
        checks = [
            s
            for s in detail.steps
            if s.type == "TOOL_CALL" and s.detail.get("tool") == "check_offers"
        ]
        assert len(checks) == 1


@pytest.mark.parametrize(
    "output,code",
    [
        ({"choices": []}, "MISSING_PICK"),
        (picks("invented-unknown"), "INVALID_PICK"),
        ({"choices": [{"item_id": "it_1", "product_id": "990001"}] * 2}, "INVALID_PICK"),
    ],
)
def test_missing_invalid_and_duplicate_picks_fall_back_in_search_order(tmp_path, output, code):
    records = [product("990001"), product("990002")]
    with environment(tmp_path, records, [output, EXPLANATION]) as client:
        detail = start(client)
        assert detail.quote.lines[0].product_id == "990001"
        assert code in codes(detail)


def test_incomplete_product_is_logged_and_skipped(tmp_path):
    records = [product("990001"), product("990002")]
    availability = [stock("990001", logisticModel=None), stock("990002")]
    with environment(
        tmp_path, records, [picks("990001"), EXPLANATION], availability=availability
    ) as client:
        detail = start(client)
        assert detail.error is None and detail.quote.lines[0].product_id == "990002"
        skipped = next(s for s in detail.steps if s.detail.get("code") == "OFFER_DATA_INVALID")
        assert skipped.detail["product_id"] == "990001"
        assert "990001" in skipped.title


def test_absent_item_has_zero_coverage_rule_and_explanation(tmp_path):
    with environment(tmp_path, [product("990001")], [picks("990001"), EXPLANATION]) as client:
        detail = start(client, items=[item(), item("unfindable")])
        assert detail.status == "WAITING_APPROVAL" and len(detail.quote.lines) == 1
        partial = next(
            rule for rule in detail.quote.policy.rules_fired if rule.rule_id == "PARTIAL_QUANTITY"
        )
        assert partial.item_id == "it_2" and partial.description == (
            "No suitable product found for the unfindable; a person needs to choose one."
        )
        assert "unfindable" in partial.description
        assert "unfindable" in detail.quote.agent_explanation
        assert "NO_USABLE_OFFER" in codes(detail)


@pytest.mark.parametrize("unusable", [False, True])
def test_no_usable_products_blocks_instead_of_failing(tmp_path, unusable):
    records = [product("990001")] if unusable else []
    outputs = [picks()]
    if unusable:
        # Exception planning can ask for an agent substitute after exhausted candidates.
        outputs += [{"query": "unfindable"}]
    with environment(
        tmp_path,
        records,
        outputs,
        availability=[stock("990001", scheduleLines=[])] if unusable else [],
    ) as client:
        detail = start(client)
        assert detail.status == "BLOCKED" and detail.quote is None
        assert detail.error.code == "NO_USABLE_PRODUCTS"
        assert "flour" in detail.error.message


def test_missing_date_moves_delivery_to_latest_earliest_available_date(tmp_path):
    records = [product("990001"), product("990002", names={"en": "Invented carrots"})]
    availability = [
        stock("990001", scheduleLines=[schedule(availableDate="2026-11-18")]),
        stock("990002", scheduleLines=[schedule(availableDate="2026-11-19")]),
    ]
    outputs = [
        picks("990001", "990002"),
        EXPLANATION,
    ]
    with environment(tmp_path, records, outputs, availability=availability) as client:
        # Busy buyers are not asked for a date; the default moves to Valio's earliest date.
        detail = start(client, requested_date=None, items=[item(), item("carrots")])
        assert detail.question is None
        assert detail.error is None and detail.intent.deliver_by == date(2026, 11, 19)
        gap = next(g for g in detail.intent.filled_gaps if g.field == "deliver_by")
        assert gap.source == "default" and gap.value == "2026-11-19"
        assert "the earliest date Valio offers for every item" in gap.note
        assert "LATE_DELIVERY" not in {rule.rule_id for rule in detail.quote.policy.rules_fired}
        assert {line.requested_date for line in detail.quote.lines} == {date(2026, 11, 19)}
        calls = client.app.state.valio.calls
        assert len(calls) == 4 and [c[2] for c in calls[-2:]] == [date(2026, 11, 19)] * 2


def test_explicit_delivery_is_never_moved(tmp_path):
    with environment(tmp_path, [product("990001")], [picks("990001"), EXPLANATION]) as client:
        detail = start(client, requested_date="2026-11-15")
        assert detail.intent.deliver_by == date(2026, 11, 15)
        assert "LATE_DELIVERY" in {rule.rule_id for rule in detail.quote.policy.rules_fired}


@pytest.mark.parametrize("failure", ["valio", "llm", "internal"])
def test_failed_calls_log_safe_error_codes_and_exception_types(tmp_path, monkeypatch, failure):
    outputs = ["invalid-json", "invalid-json"] if failure == "llm" else [picks("990001")]
    with environment(tmp_path, [product("990001")], outputs) as client:
        if failure == "valio":

            async def fail(*args):
                raise ValioTransportError("invented-sensitive-response")

            monkeypatch.setattr(client.app.state.valio, "price", fail)
        elif failure == "internal":

            def fail(*args, **kwargs):
                raise RuntimeError("invented-sensitive-response")

            monkeypatch.setattr(client.app.state.catalog, "search", fail)
        detail = start(client)
        assert detail.status == "FAILED"
        expected = {
            "valio": "VALIO_TRANSPORT_ERROR",
            "llm": "LLM_OUTPUT_ERROR",
            "internal": "INTERNAL_ERROR",
        }[failure]
        assert expected in codes(detail)
        assert "invented-sensitive-response" not in detail.model_dump_json()
        if failure == "internal":
            assert detail.steps[-1].detail["exception_type"] == "RuntimeError"


@pytest.mark.parametrize(
    "ideas,kept,drop",
    [
        ([], 0, None),
        (
            [
                {
                    "kind": "seasonal",
                    "title": "Invented carrots",
                    "reason": "Optional vegetables",
                    "replaces_item_id": None,
                    "query": "carrots",
                }
            ],
            1,
            None,
        ),
        (
            [
                {
                    "kind": "seasonal",
                    "title": "Invented swap",
                    "reason": "Optional idea",
                    "replaces_item_id": "unknown",
                    "query": "carrots",
                }
            ],
            0,
            "UNKNOWN_ITEM",
        ),
        (
            [
                {
                    "kind": "seasonal",
                    "title": "Invented duplicate",
                    "reason": "Optional idea",
                    "replaces_item_id": None,
                    "query": "flour",
                }
            ],
            0,
            "DUPLICATE",
        ),
    ],
)
def test_playful_approval_always_logs_suggestion_counts(tmp_path, ideas, kept, drop):
    records = [product("990001"), product("990002", names={"en": "Invented carrots"})]
    with environment(
        tmp_path,
        records,
        [picks("990001"), EXPLANATION, {"suggestions": ideas}],
        playful=True,
        approval=True,
    ) as client:
        detail = start(client, customer_id="invented-chef")
        assert detail.status == "WAITING_APPROVAL"
        step = next(s for s in detail.steps if s.type == "SUGGESTIONS_PROPOSED")
        assert len(detail.suggestions) == step.detail["count"] == kept
        if drop:
            assert drop in step.detail["drop_reasons"]
        assert len(detail.quote.lines) == 1


def test_suggestion_llm_failure_keeps_approval_and_logs_zero(tmp_path):
    with environment(
        tmp_path,
        [product("990001")],
        [picks("990001"), EXPLANATION, "bad-json", "bad-json"],
        playful=True,
        approval=True,
    ) as client:
        detail = start(client, customer_id="invented-chef")
        assert detail.status == "WAITING_APPROVAL" and detail.error is None
        step = next(s for s in detail.steps if s.type == "SUGGESTIONS_PROPOSED")
        assert step.detail["count"] == 0 and "LLM_ERROR" in step.detail["drop_reasons"]


def test_birthday_full_stock_risk_single_candidate_and_absent_item(tmp_path):
    nuts = ["peanuts", "tree_nuts"]
    records = [
        product("990001", allergens={"may_contain": nuts, "data_available": True}),
        product("990002", allergens={"free_from": nuts, "data_available": True}),
        product(
            "990003",
            names={"en": "Invented carrots"},
            allergens={"free_from": nuts, "data_available": True},
        ),
    ]
    recorded_pick = picks("990001", "990003")
    recorded_pick["choices"][0]["suitable_ids"] = ["990002"]
    with environment(tmp_path, records, [recorded_pick, EXPLANATION]) as client:
        detail = start(
            client,
            items=[item(), item("carrots"), item("unfindable")],
            exclude_allergens=nuts,
        )
        assert detail.status == "WAITING_APPROVAL" and detail.error is None
        rules = {r.rule_id for r in detail.quote.policy.rules_fired}
        assert {"ALLERGEN_MAY_CONTAIN", "PARTIAL_QUANTITY"} <= rules
        variants = client.get(f"/approvals/{detail.approval_id}").json()["options"]
        assert any(
            "ALLERGEN_MAY_CONTAIN" not in {r["rule_id"] for r in q["policy"]["rules_fired"]}
            for q in variants[1:]
        )


def test_reorder_skips_incomplete_past_product_and_researches_by_name(tmp_path):
    records = [product("990001"), product("990002")]
    with environment(
        tmp_path,
        records,
        [picks("990002"), EXPLANATION],
        playful=True,
        availability=[stock("990001", logisticModel=None), stock("990002")],
    ) as client:
        profile_path = tmp_path / "invented-chef.json"
        profile = json.loads(profile_path.read_text())
        profile["order_history"] = [
            {
                "order_ref": "invented-past",
                "date": "2026-10-01",
                "title": "Invented flour order",
                "event_type": "lunch",
                "guests": 10,
                "lines": [
                    {
                        "product_id": "990001",
                        "name": "Invented pantry flour",
                        "quantity_sales_units": 2.0,
                        "sales_unit": "PRK",
                        "quantity_text": "2 x 1 kg",
                        "unit_price": 3.35,
                        "line_total": 6.7,
                    }
                ],
                "total": 6.7,
                "source": "seeded",
                "simulated": True,
            }
        ]
        profile_path.write_text(json.dumps(profile))
        detail = start(
            client,
            customer_id="invented-chef",
            items=[],
            reorder_of="invented-past",
            event={"type": "lunch", "date": "2026-11-21", "guests": 20, "dishes": []},
        )
        assert detail.error is None and detail.quote is not None
        assert detail.intent.based_on.scale == 2
        assert [(line.product_id, line.quantity_sales_units) for line in detail.quote.lines] == [
            ("990002", 4),
        ]
        assert {"OFFER_DATA_INVALID", "REORDER_RESEARCH"} <= set(codes(detail))
        assert not any(step.type == "EVENT_EXPANDED" for step in detail.steps)


def test_agent_mode_defaults_and_rejects_invalid_values(monkeypatch):
    monkeypatch.delenv("AGENT_MODE", raising=False)
    assert Settings().agent_mode == "pipeline"
    monkeypatch.setenv("AGENT_MODE", "tools")
    assert Settings().agent_mode == "tools"
    with pytest.raises(ValueError, match="AGENT_MODE"):
        Settings(agent_mode="unknown")
