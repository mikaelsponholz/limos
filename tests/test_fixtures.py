import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from gateway import schemas

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "api"
FIXTURE_MODELS: dict[str, type[BaseModel]] = {
    "approval-detail.json": schemas.ApprovalDetail,
    "approval-list.json": schemas.ApprovalList,
    "approve-body.json": schemas.ApproveBody,
    "approve-response.json": schemas.ApproveResponse,
    "audit.json": schemas.Audit,
    "error.json": schemas.ErrorEnvelope,
    "health.json": schemas.Health,
    "offer.json": schemas.Offer,
    "order.json": schemas.Order,
    "policy-decision.json": schemas.PolicyDecision,
    "presets.json": schemas.Presets,
    "quote.json": schemas.Quote,
    "request-create.json": schemas.RequestCreate,
    "request-created.json": schemas.RequestCreated,
    "request-list.json": schemas.RequestList,
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
    ],
    ids=["preset", "text", "event", "items"],
)
def test_each_create_form(source: dict[str, Any]) -> None:
    payload = {"buyer_id": "demo-catering", **source}
    model = schemas.RequestCreate.model_validate_json(json.dumps(payload), strict=True)
    assert model.model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize("source", [{}, {"text": "", "items": []}])
def test_create_requires_input(source: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="At least one"):
        schemas.RequestCreate.model_validate({"buyer_id": "demo-catering", **source})


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
    ],
)
def test_invalid_nested_offer_data(field: str, value: Any, error_type: str) -> None:
    payload = json.loads((FIXTURES / "offer.json").read_text(encoding="utf-8"))
    payload[field] = value
    with pytest.raises(ValidationError) as exc:
        schemas.Offer.model_validate_json(json.dumps(payload), strict=True)
    assert error_type in {error["type"] for error in exc.value.errors()}
