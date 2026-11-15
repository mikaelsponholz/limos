import hashlib
import json
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from gateway import schemas

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "api"
FIXTURE_MODELS: dict[str, type[BaseModel]] = {
    "answer-body.json": schemas.AnswerBody,
    "approval-detail.json": schemas.ApprovalDetail,
    "approval-list.json": schemas.ApprovalList,
    "approve-body.json": schemas.ApproveBody,
    "approve-response.json": schemas.ApproveResponse,
    "audit.json": schemas.Audit,
    "customer.json": schemas.Customer,
    "customers.json": schemas.Customers,
    "error.json": schemas.ErrorEnvelope,
    "health.json": schemas.Health,
    "offer.json": schemas.Offer,
    "order.json": schemas.Order,
    "policy-decision.json": schemas.PolicyDecision,
    "presets.json": schemas.Presets,
    "quote.json": schemas.Quote,
    "request-create.json": schemas.RequestCreate,
    "request-created.json": schemas.RequestCreated,
    "request-chef.json": schemas.RequestDetail,
    "request-list.json": schemas.RequestList,
    "request-needs-input.json": schemas.RequestDetail,
    "request.json": schemas.RequestDetail,
    "steps.json": schemas.Steps,
}


def test_fixture_mapping_is_complete() -> None:
    assert {path.name for path in FIXTURES.iterdir() if path.is_file()} == set(FIXTURE_MODELS)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda path: path.name)
def test_fixture_validates_and_round_trips(path: Path) -> None:
    assert path.name in FIXTURE_MODELS, f"Add {path.name} to FIXTURE_MODELS."
    raw = path.read_text(encoding="utf-8")
    # JSON strict mode accepts ISO dates/times while rejecting scalar coercion.
    model = FIXTURE_MODELS[path.name].model_validate_json(raw, strict=True)
    assert model.model_dump(mode="json", exclude_unset=True) == json.loads(raw)


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"request_id": "r_example", "status": "RUNNING", "typo": True}, "extra_forbidden"),
        ({"request_id": "r_example", "status": "TYPO"}, "literal_error"),
        ({"status": "RUNNING"}, "missing"),
        ({"request_id": 123, "status": "RUNNING"}, "string_type"),
    ],
)
def test_invalid_contract_data(payload: dict[str, Any], error_type: str) -> None:
    with pytest.raises(ValidationError) as exc:
        schemas.RequestCreated.model_validate_json(json.dumps(payload), strict=True)
    assert error_type in {error["type"] for error in exc.value.errors()}


@pytest.mark.parametrize(
    "source",
    [
        {"preset_id": "wedding_120"},
        {"text": "Milk for the kitchen"},
        {"event": {"type": "lunch", "date": "2026-11-20", "guests": 10, "dishes": []}},
        {"items": [{"description": "milk", "quantity": 2, "unit": "L", "constraints": []}]},
        {"reorder_of": "ph_pena_01"},
    ],
    ids=["preset", "text", "event", "items", "reorder"],
)
def test_each_create_form(source: dict[str, Any]) -> None:
    payload = {"buyer_id": "demo-catering", **source}
    model = schemas.RequestCreate.model_validate_json(json.dumps(payload), strict=True)
    assert model.model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize("source", [{}, {"text": "", "items": []}])
def test_create_requires_input(source: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="At least one"):
        schemas.RequestCreate.model_validate({"buyer_id": "demo-catering", **source})


@pytest.mark.parametrize("buyer", [{}, {"buyer_id": None}, {"buyer_id": "ab-oy"}])
def test_customer_can_supply_buyer(buyer: dict[str, Any]) -> None:
    payload = {"customer_id": "c_pena", "reorder_of": "ph_pena_01", **buyer}
    model = schemas.RequestCreate.model_validate_json(json.dumps(payload), strict=True)
    assert model.model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize(
    "identity",
    [{}, {"buyer_id": None, "customer_id": None}, {"buyer_id": "", "customer_id": ""}],
)
def test_create_requires_buyer_or_customer(identity: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="buyer_id or customer_id"):
        schemas.RequestCreate.model_validate({"text": "Layer cakes", **identity})


def test_customer_alone_is_not_request_input() -> None:
    with pytest.raises(
        ValidationError, match="preset_id, text, event, items, reorder_of or stock_levels"
    ):
        schemas.RequestCreate.model_validate({"customer_id": "c_pena"})


@pytest.mark.parametrize("model_name", ["Customer", "CustomerSummary"])
def test_unknown_age_group(model_name: str) -> None:
    if model_name == "Customer":
        payload = json.loads((FIXTURES / "customer.json").read_text(encoding="utf-8"))
    else:
        payload = json.loads((FIXTURES / "customers.json").read_text(encoding="utf-8"))[0]
    payload["age_group"] = "unknown"
    with pytest.raises(ValidationError) as exc:
        getattr(schemas, model_name).model_validate_json(json.dumps(payload), strict=True)
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [
        (("age_group",), "literal_error")
    ]


@pytest.mark.parametrize("answer", ["30", None])
def test_answer_can_use_explicit_value_or_default(answer: str | None) -> None:
    payload = {"question_id": "qn_r_pena01", "answer": answer}
    model = schemas.AnswerBody.model_validate_json(json.dumps(payload), strict=True)
    assert model.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    [
        (
            "allergens",
            {"contains": ["nuts"], "may_contain": [], "data_available": True},
            "literal_error",
        ),
        (
            "allergens",
            {"contains": [], "may_contain": [], "data_available": True, "typo": 1},
            "extra_forbidden",
        ),
        ("dietary_check", {"no_tree_nut": "confirmed"}, "literal_error"),
        ("unit_price", "7.9", "float_type"),
        ("quantity_sales_units", True, "float_type"),
        ("requested_date", "20 November 2026", "date_parsing"),
        ("closing_time", "2026-11-19T17:00:00", "timezone_aware"),
        ("order_deadline", "2026-11-19T17:00:00", "timezone_aware"),
        ("delivery_date", "20 November 2026", "date_parsing"),
    ],
)
def test_invalid_nested_offer_data(field: str, value: Any, error_type: str) -> None:
    payload = json.loads((FIXTURES / "offer.json").read_text(encoding="utf-8"))
    payload[field] = value
    with pytest.raises(ValidationError) as exc:
        schemas.Offer.model_validate_json(json.dumps(payload), strict=True)
    assert error_type in {error["type"] for error in exc.value.errors()}


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda path: path.name)
def test_fixture_arithmetic_and_delivery(path: Path) -> None:
    # Fixtures have a fixed reference date so their cutoff examples never age with the clock.
    reference = datetime.fromisoformat("2026-11-14T10:00:00+02:00")
    raw = path.read_text(encoding="utf-8")
    assert "\u2014" not in raw
    assert "\u00b7" not in raw

    def money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def check(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                check(child)
        elif isinstance(value, dict):
            for key in ("product_id", "substitute_for"):
                if value.get(key) is not None:
                    assert re.fullmatch(r"900\d{3}", value[key]), (path.name, value[key])
            if "quantity_sales_units" in value and "unit_price" in value:
                assert value["line_total"] == money(
                    value["quantity_sales_units"] * value["unit_price"]
                ), path.name
            if "available_date" in value:
                available = value["available_date"]
                assert value["delivery_date"] == (
                    max(value["requested_date"], available) if available else None
                ), path.name
                closing = value["closing_time"]
                future = closing and datetime.fromisoformat(closing) > reference
                assert value["order_deadline"] == (closing if future else None), path.name
                if closing and not future:
                    assert "CUTOFF_PASSED" in value["flags"], path.name
                if available and available > value["requested_date"]:
                    assert "LATE_DELIVERY" in value["flags"], path.name
                assert value["line_total_with_tax"] == money(
                    value["line_total"] * (1 + value["tax_rate"] / 100)
                ), path.name
            if "lines" in value and "total" in value:
                assert value["total"] == money(
                    sum((line["line_total"] for line in value["lines"]), Decimal(0))
                ), path.name
                if "total_with_tax" in value:
                    assert value["total_with_tax"] == money(
                        sum((line["line_total_with_tax"] for line in value["lines"]), Decimal(0))
                    ), path.name
                    deadlines = [
                        line["order_deadline"]
                        for line in value["lines"]
                        if line["order_deadline"] is not None
                    ]
                    assert value["order_deadline"] == (min(deadlines) if deadlines else None)
                    dates = [line["delivery_date"] for line in value["lines"]]
                    assert value["delivery_date"] == max(dates)
            for child in value.values():
                check(child)

    check(json.loads(raw, parse_float=Decimal, parse_int=Decimal))


def test_original_product_ids() -> None:
    expected = {
        "it_butter": {"900101", "900102", "900103"},
        "it_cream": {"900201", "900202"},
        "it_bread": {"900301", "900302"},
        "it_cucumber": {"900401", "900402"},
    }

    def check(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                check(child)
        elif isinstance(value, dict):
            if value.get("item_id") in expected:
                for key in ("product_id", "substitute_for"):
                    if value.get(key) is not None:
                        assert value[key] in expected[value["item_id"]]
            if "product_ids" in value:
                assert set(value["product_ids"]) == set().union(*expected.values())
            for child in value.values():
                check(child)

    for path in sorted(FIXTURES.iterdir()):
        check(json.loads(path.read_text(encoding="utf-8")))
    request = json.loads((FIXTURES / "request.json").read_text(encoding="utf-8"))
    assert [offer["product_id"] for offer in request["candidates"]] == [
        "900102",
        "900101",
        "900103",
        "900201",
        "900202",
        "900302",
        "900301",
        "900401",
        "900402",
    ]


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda path: path.name)
def test_fixture_hashes(path: Path) -> None:
    def digest(value: dict[str, Any]) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def check(value: Any) -> None:
        if isinstance(value, list):
            previous = None
            for child in value:
                if isinstance(child, dict) and "prev_hash" in child:
                    assert child["prev_hash"] == previous
                    previous = child["hash"]
                check(child)
        elif isinstance(value, dict):
            if "prev_hash" in value:
                assert value["hash"] == digest({k: v for k, v in value.items() if k != "hash"})
            if "content_hash" in value:
                if "lines" in value:
                    fields = ("lines", "total", "total_with_tax", "delivery_date", "expires_at")
                    assert value["content_hash"] == digest({key: value[key] for key in fields})
                else:
                    quote = json.loads((FIXTURES / "quote.json").read_text(encoding="utf-8"))
                    assert value["quote_id"] == quote["quote_id"]
                    assert value["content_hash"] == quote["content_hash"]
            for child in value.values():
                check(child)

    check(json.loads(path.read_text(encoding="utf-8")))


def test_customer_scenarios() -> None:
    customer = schemas.Customer.model_validate_json((FIXTURES / "customer.json").read_text())
    customers = schemas.Customers.model_validate_json((FIXTURES / "customers.json").read_text())
    pending = schemas.RequestDetail.model_validate_json(
        (FIXTURES / "request-needs-input.json").read_text()
    )
    chef = schemas.RequestDetail.model_validate_json((FIXTURES / "request-chef.json").read_text())
    assert (customer.customer_id, customer.name, customer.company) == ("c_pena", "Frank", "Ab Oy")
    assert customer.age_group == "55_plus" and not customer.playful
    assert len(customer.order_history) == 2
    assert customer.order_history == sorted(
        customer.order_history, key=lambda order: order.date, reverse=True
    )
    latest = customer.order_history[0]
    assert latest.guests == 30 and "board meeting" in latest.title
    assert "layer cake" in latest.lines[0].name
    assert customers.root[0].last_order is not None
    assert customers.root[0].last_order.order_ref == latest.order_ref
    assert pending.customer_id == customer.customer_id and pending.status == "NEEDS_INPUT"
    assert pending.question is not None and pending.question.field == "guests"
    assert pending.question.choices == [str(order.guests) for order in customer.order_history]
    assert pending.question.default == str(latest.guests)
    assert {gap.source for gap in pending.intent.filled_gaps} >= {"history", "profile"}
    assert pending.suggestions == []
    chef_summary = next(item for item in customers.root if item.customer_id == "c_chef")
    assert chef_summary.age_group == "under_35" and chef_summary.playful
    assert chef.customer_id == chef_summary.customer_id
    assert chef_summary.last_order is not None
    based_on = chef.intent.based_on
    assert based_on is not None and based_on.order_ref == chef_summary.last_order.order_ref
    assert (based_on.guests_before, based_on.guests_now, based_on.scale) == (40, 80, 2)
    assert len(chef.suggestions) == 1
    suggestion = chef.suggestions[0]
    assert suggestion.kind == "seasonal" and suggestion.status == "OFFERED"
    assert chef.quote is not None and suggestion.offer is not None
    assert suggestion.offer.product_id not in {line.product_id for line in chef.quote.lines}
