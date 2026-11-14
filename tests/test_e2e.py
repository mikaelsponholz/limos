"""Thin HTTP flows with invented products, isolated customers and injected resources."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from time import sleep

import pytest
from fastapi.testclient import TestClient

from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.runner import AGENT_ID
from gateway.schemas import ApproveResponse, Audit, Customer, RequestCreated, RequestDetail
from gateway.settings import Settings
from gateway.store import MemoryStore
from tests.engine_support import BUYER, mandate
from tests.test_offers import NOW, FakeValio, price, product, stock

IDENTITY = {"buyer_id": BUYER, "agent_id": AGENT_ID}
OTHER_BUYER = "invented-other-buyer"


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[tuple[TestClient, Clock]]:
    records = [
        product("900701", names={"en": "Invented pantry flour"}),
        product("900702", names={"en": "Invented pantry oats"}),
    ]
    profile = Customer.model_validate_json(
        json.dumps(
            {
                "customer_id": "invented-customer",
                "name": "Invented Cook",
                "company": "Invented Kitchen",
                "role": "Chef",
                "buyer_id": BUYER,
                "age_group": "35_to_54",
                "playful": False,
                "preferences": [{"key": "organic", "level": "prefer"}],
                "exclude_allergens": [],
                "defaults": {"priority": "quality", "typical_guests": 30, "budget": 90.0},
                "order_history": [
                    {
                        "order_ref": "invented-past",
                        "date": "2026-10-01",
                        "title": "Invented pantry lunch",
                        "event_type": "lunch",
                        "guests": 30,
                        "lines": [
                            {
                                "product_id": record.product_id,
                                "name": record.names.en,
                                "quantity_sales_units": quantity,
                                "sales_unit": "PRK",
                                "quantity_text": f"{quantity:g} x 1 kg",
                                "unit_price": 3.35,
                                "line_total": round(quantity * 3.35, 2),
                            }
                            for record, quantity in zip(records, [2.25, 3.0], strict=True)
                        ],
                        "total": 17.59,
                        "source": "seeded",
                        "simulated": True,
                    }
                ],
                "simulated": True,
            }
        )
    )
    (tmp_path / "invented-customer.json").write_text(profile.model_dump_json(), encoding="utf-8")
    clock = Clock()
    ids = count(1)
    app = create_app(
        Settings(store="memory", valio_api_key="", catalog_path=str(tmp_path / "missing.json")),
        catalog=Catalog(records),
        valio=FakeValio(
            [price(p.product_id) for p in records], [stock(p.product_id) for p in records]
        ),
        store=MemoryStore(),
        clock=clock,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={
            buyer: mandate(buyer_id=buyer, auto_limit=100.0, allowed_agents=(AGENT_ID,))
            for buyer in (BUYER, OTHER_BUYER)
        },
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )
    with TestClient(app) as client:
        yield client, clock


def poll(client: TestClient, request_id: str) -> RequestDetail:
    # TestClient awaits Starlette's background task before returning POST /requests.
    # Keep a bounded HTTP poll for the public lifecycle even if scheduling changes.
    for _ in range(20):
        response = client.get(f"/requests/{request_id}")
        assert response.status_code == 200, response.text
        detail = RequestDetail.model_validate_json(response.text)
        if detail.status != "RUNNING":
            return detail
        sleep(0.001)
    pytest.fail("Request remained RUNNING after 20 polls")


def start(client: TestClient, quantity: float = 2) -> RequestDetail:
    response = client.post(
        "/requests",
        json={
            "buyer_id": BUYER,
            "requested_date": "2026-11-20",
            "items": [
                {"description": "flour", "quantity": quantity, "unit": "kg", "constraints": []}
            ],
        },
    )
    assert response.status_code == 202, response.text
    created = RequestCreated.model_validate_json(response.text)
    return poll(client, created.request_id)


def audit(client: TestClient, request_id: str) -> Audit:
    response = client.get(f"/audit/{request_id}")
    assert response.status_code == 200, response.text
    result = Audit.model_validate_json(response.text)
    assert result.request_id == request_id and result.chain_valid
    return result


def approval_body(detail: RequestDetail) -> dict[str, str]:
    assert detail.quote is not None
    return {
        "approver": "Invented Approver",
        "quote_id": detail.quote.quote_id,
        "content_hash": detail.quote.content_hash,
        "comment": "Approved invented order",
    }


def test_auto_order_and_ordered_audit(environment: tuple[TestClient, Clock]) -> None:
    client, _ = environment
    detail = start(client)
    assert detail.status == "ORDERED", detail.error
    assert detail.quote is not None and detail.order is not None
    assert detail.quote.policy.outcome == "AUTO" and detail.quote.policy.rules_fired == []
    assert detail.approval_id is None
    order = detail.order
    assert order.simulated and order.approved_by is None
    assert order.lines == detail.quote.lines and order.total == 6.7
    assert order.placed_at == NOW
    assert "100.00 EUR auto limit" in order.authority
    assert "250.00 EUR" in order.authority and "5000.00 EUR hard cap" in order.authority
    log = audit(client, detail.request_id)
    assert log.steps == detail.steps
    assert [step.type for step in log.steps] == [
        "REQUEST_RECEIVED",
        "INTENT_PARSED",
        "TOOL_CALL",
        "TOOL_CALL",
        "CANDIDATES",
        "CHOICE",
        "TOOL_CALL",
        "QUOTE_CREATED",
        "POLICY_DECISION",
        "TOOL_CALL",
        "ORDER_PLACED",
    ]
    assert [step.detail["tool"] for step in log.steps if step.type == "TOOL_CALL"] == [
        "search_products",
        "check_offers",
        "request_quote",
        "place_order",
    ]


def test_approval_hash_guard_and_idempotent_order(environment: tuple[TestClient, Clock]) -> None:
    client, _ = environment
    detail = start(client, 40)
    assert detail.status == "WAITING_APPROVAL", detail.error
    assert detail.quote is not None and detail.approval_id is not None and detail.order is None
    quote = detail.quote
    assert quote.total > quote.policy.mandate.auto_limit
    assert [rule.rule_id for rule in quote.policy.rules_fired] == ["SPEND_SOFT_LIMIT"]
    entries = client.get("/approvals", params={"status": "PENDING"})
    assert entries.status_code == 200
    assert [(entry["approval_id"], entry["quote_id"]) for entry in entries.json()] == [
        (detail.approval_id, quote.quote_id)
    ]
    body = {
        **IDENTITY,
        "quote_id": quote.quote_id,
        "idempotency_key": f"{detail.request_id}-{quote.quote_id}",
    }
    early = client.post("/gateway/orders", json=body)
    assert early.status_code == 403 and early.json()["error"]["code"] == "NEEDS_APPROVAL"
    url = f"/approvals/{detail.approval_id}/approve"
    wrong = client.post(url, json={**approval_body(detail), "content_hash": "sha256:wrong"})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "QUOTE_CHANGED"
    unchanged = poll(client, detail.request_id)
    assert unchanged.status == "WAITING_APPROVAL" and unchanged.order is None
    assert client.get(f"/approvals/{detail.approval_id}").json()["status"] == "PENDING"
    assert "ORDER_PLACED" not in [step.type for step in audit(client, detail.request_id).steps]
    response = client.post(url, json=approval_body(detail))
    assert response.status_code == 200, response.text
    approved = ApproveResponse.model_validate_json(response.text)
    assert approved.status == "APPROVED" and approved.order.approved_by == "Invented Approver"
    assert approved.order.simulated and approved.order.idempotency_key == body["idempotency_key"]
    ordered = poll(client, detail.request_id)
    assert ordered.status == "ORDERED" and ordered.order == approved.order
    before = audit(client, detail.request_id)
    assert [step.type for step in before.steps][-3:] == [
        "AVAILABILITY_RECHECKED",
        "APPROVED",
        "ORDER_PLACED",
    ]
    retry = client.post("/gateway/orders", json=body)
    assert retry.status_code == 200 and retry.json() == approved.order.model_dump(mode="json")
    after = audit(client, detail.request_id)
    assert after.steps == before.steps
    assert sum(step.type == "ORDER_PLACED" for step in after.steps) == 1


def test_expired_quote_cannot_be_approved(environment: tuple[TestClient, Clock]) -> None:
    client, clock = environment
    detail = start(client, 40)
    assert detail.status == "WAITING_APPROVAL" and detail.quote is not None
    clock.now = detail.quote.expires_at + timedelta(microseconds=1)
    response = client.post(f"/approvals/{detail.approval_id}/approve", json=approval_body(detail))
    assert response.status_code == 410 and response.json()["error"]["code"] == "QUOTE_EXPIRED"
    assert poll(client, detail.request_id).order is None
    assert client.get(f"/approvals/{detail.approval_id}").json()["status"] == "EXPIRED"
    assert "ORDER_PLACED" not in [step.type for step in audit(client, detail.request_id).steps]


def test_reorder_scales_rounds_and_appends_customer_history(
    environment: tuple[TestClient, Clock],
) -> None:
    client, _ = environment
    response = client.post(
        "/requests",
        json={
            "customer_id": "invented-customer",
            "reorder_of": "invented-past",
            "requested_date": "2026-11-20",
            "event": {"type": "lunch", "date": "2026-11-21", "guests": 60, "dishes": []},
        },
    )
    assert response.status_code == 202, response.text
    detail = poll(client, RequestCreated.model_validate_json(response.text).request_id)
    assert detail.status == "ORDERED", detail.error
    assert detail.order is not None and detail.intent.based_on is not None
    assert detail.intent.based_on.scale == 2.0
    assert (detail.intent.based_on.guests_before, detail.intent.based_on.guests_now) == (30, 60)
    assert [(line.product_id, line.quantity_sales_units) for line in detail.order.lines] == [
        ("900701", 5),  # ceil(2.25 * 2), a whole sales unit
        ("900702", 6),
    ]
    assert {(gap.field, gap.source) for gap in detail.intent.filled_gaps} == {
        ("buyer_id", "profile"),
        ("budget", "profile"),
        ("preferences.organic", "profile"),
        ("priority", "profile"),
    }
    assert detail.intent.priority == "quality" and detail.intent.budget == 90
    response = client.get("/customers/invented-customer")
    assert response.status_code == 200
    customer = Customer.model_validate_json(response.text)
    assert len(customer.order_history) == 2
    newest = customer.order_history[0]
    assert newest.source == "limos" and newest.simulated and newest.guests == 60
    assert newest.order_ref == f"ph_{detail.order.order_id}"
    assert [line.quantity_sales_units for line in newest.lines] == [5, 6]
    assert customer.order_history[1].order_ref == "invented-past"
    assert [step.type for step in audit(client, detail.request_id).steps][:3] == [
        "REQUEST_RECEIVED",
        "CUSTOMER_LOADED",
        "INTENT_PARSED",
    ]


def test_buyer_isolation_and_unpermitted_agent(environment: tuple[TestClient, Clock]) -> None:
    client, _ = environment
    detail = start(client)
    assert detail.status == "ORDERED" and detail.quote is not None and detail.order is not None
    order_body = {
        **IDENTITY,
        "quote_id": detail.quote.quote_id,
        "idempotency_key": detail.order.idempotency_key,
    }
    # There is no GET quote route; requesting approval resolves the quote by buyer.
    quote_url = f"/gateway/quotes/{detail.quote.quote_id}/request-approval"
    hidden_quote = client.post(
        quote_url,
        json={**IDENTITY, "buyer_id": OTHER_BUYER, "explanation": "Invented request"},
    )
    assert hidden_quote.status_code == 404 and hidden_quote.json()["error"]["code"] == "NOT_FOUND"
    hidden_order = client.get(
        f"/gateway/orders/{detail.order.order_id}", params={**IDENTITY, "buyer_id": OTHER_BUYER}
    )
    assert hidden_order.status_code == 404 and hidden_order.json()["error"]["code"] == "NOT_FOUND"
    assert (
        client.post("/gateway/orders", json={**order_body, "buyer_id": OTHER_BUYER}).status_code
        == 404
    )
    for response in (
        client.post("/gateway/orders", json={**order_body, "agent_id": "invented-unpermitted"}),
        client.get(
            f"/gateway/orders/{detail.order.order_id}",
            params={**IDENTITY, "agent_id": "invented-unpermitted"},
        ),
        client.post(
            quote_url,
            json={**IDENTITY, "agent_id": "invented-unpermitted", "explanation": "Invented"},
        ),
    ):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "AGENT_NOT_PERMITTED"
    assert sum(step.type == "ORDER_PLACED" for step in audit(client, detail.request_id).steps) == 1
