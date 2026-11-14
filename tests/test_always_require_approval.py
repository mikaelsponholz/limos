"""Buyer mandate approval policy with invented products and the existing HTTP fakes."""

import json
from collections.abc import Iterator
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.policy import ItemContext, get_mandate, load_mandates
from gateway.runner import AGENT_ID
from gateway.schemas import ApproveResponse, Outcome, Quote
from gateway.settings import Settings
from gateway.store import MemoryStore
from tests.engine_support import Engine
from tests.test_e2e import approval_body, audit, poll
from tests.test_offers import NOW, FakeValio, price, product, stock
from tests.test_policy import offer


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    records = [
        product(
            "900901",
            names={"en": "Invented lactose-free milk"},
            pack_size={"value": 1, "unit": "L"},
            claims=["lactose_free"],
            allergens={"free_from": ["lactose"], "data_available": True},
        ),
        product("900902", names={"en": "Invented butter"}),
        product("900903", names={"en": "Invented potatoes"}, country_of_origin="fi"),
        product("900904", names={"en": "Invented eggs"}, pack_size={"value": 30, "unit": "pcs"}),
    ]
    ids = count(1)
    app = create_app(
        Settings(store="memory", valio_api_key="", gemini_api_key="", database_url=""),
        catalog=Catalog(records),
        valio=FakeValio(
            [price(p.product_id) for p in records], [stock(p.product_id) for p in records]
        ),
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        customer_overlay_dir=tmp_path / "overlays",
    )
    with TestClient(app) as result:
        yield result


@pytest.mark.parametrize(
    ("buyer_id", "outcome", "rules"),
    [
        ("ab-oy", "NEEDS_APPROVAL", ["APPROVAL_ALWAYS_REQUIRED"]),
        ("bistro-example", "AUTO", []),
    ],
)
def test_same_quote_follows_buyer_mandate(
    client: TestClient, buyer_id: str, outcome: Outcome, rules: list[str]
) -> None:
    identity = {"buyer_id": buyer_id, "agent_id": AGENT_ID}
    response = client.post(
        "/gateway/quotes",
        json={
            **identity,
            "requested_date": "2026-11-20",
            "lines": [{"item_id": "it_butter", "product_id": "900902", "quantity_sales_units": 2}],
        },
    )
    assert response.status_code == 200, response.text
    quote = Quote.model_validate_json(response.text)
    assert quote.total < quote.policy.mandate.line_limit < quote.policy.mandate.auto_limit
    assert quote.policy.outcome == outcome
    assert [rule.rule_id for rule in quote.policy.rules_fired] == rules
    assert quote.policy.mandate.always_require_approval is (buyer_id == "ab-oy")
    order_body = {
        **identity,
        "quote_id": quote.quote_id,
        "idempotency_key": f"invented-order-{quote.quote_id}",
    }
    response = client.post("/gateway/orders", json=order_body)
    if outcome == "AUTO":
        assert response.status_code == 200, response.text
        assert response.json()["approved_by"] is None
        return

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NEEDS_APPROVAL"
    response = client.post(
        f"/gateway/quotes/{quote.quote_id}/request-approval",
        json={**identity, "explanation": "This buyer approves every order personally."},
    )
    assert response.status_code == 200, response.text
    url = f"/approvals/{response.json()['approval_id']}/approve"
    body = {
        "approver": "Invented Approver",
        "quote_id": quote.quote_id,
        "content_hash": quote.content_hash,
        "comment": "Approved invented order",
    }
    wrong = client.post(url, json={**body, "content_hash": "sha256:wrong"})
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "QUOTE_CHANGED"
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    result = ApproveResponse.model_validate_json(response.text)
    assert result.order.quote_id == quote.quote_id
    assert result.order.approved_by == "Invented Approver"


@pytest.mark.parametrize(
    ("line_count", "outcome", "rules"),
    [
        (7, "NEEDS_APPROVAL", ["SPEND_SOFT_LIMIT", "APPROVAL_ALWAYS_REQUIRED"]),
        (21, "BLOCK", ["SPEND_HARD_CAP", "SPEND_SOFT_LIMIT"]),
    ],
)
async def test_specific_limits_precede_buyer_approval_and_block_suppresses_it(
    line_count: int, outcome: Outcome, rules: list[str]
) -> None:
    lines = [
        offer(
            item_id=f"it_invented_{index}",
            quantity_sales_units=24.0,
            quantity_requested={"value": 24.0, "unit": "kg"},
            quantity_covered={"value": 24.0, "unit": "kg"},
            line_total=240.0,
            line_total_with_tax=272.4,
        )
        for index in range(line_count)
    ]
    quote = await Engine().quote(
        lines=lines,
        buyer_id="ab-oy",
        agent_id=AGENT_ID,
        mandate=get_mandate("ab-oy"),
        item_context={line.item_id: ItemContext() for line in lines},
    )
    assert quote.policy.outcome == outcome
    assert [rule.rule_id for rule in quote.policy.rules_fired] == rules
    assert quote.policy.mandate.always_require_approval is True


def test_mandate_loader_defaults_to_false(tmp_path: Path) -> None:
    entry = get_mandate("ab-oy").model_dump(mode="json", exclude={"always_require_approval"})
    path = tmp_path / "invented-mandates.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    loaded = load_mandates(path)["ab-oy"]
    assert loaded.always_require_approval is False
    assert loaded.contract.always_require_approval is False


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_mandate_loader_rejects_non_boolean(tmp_path: Path, value: Any) -> None:
    entry = get_mandate("ab-oy").model_dump(mode="json")
    entry["always_require_approval"] = value
    path = tmp_path / "invented-mandates.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_mandates(path)


def test_pena_item_request_waits_for_hash_bound_human_approval(client: TestClient) -> None:
    response = client.post(
        "/requests",
        json={
            "customer_id": "c_pena",
            "requested_date": "2026-11-20",
            "items": [{"description": "butter", "quantity": 2, "unit": "kg", "constraints": []}],
        },
    )
    assert response.status_code == 202, response.text
    detail = poll(client, response.json()["request_id"])
    assert detail.status == "WAITING_APPROVAL", detail.error
    assert detail.buyer_id == "ab-oy" and detail.customer_id == "c_pena"
    assert detail.quote is not None and detail.approval_id is not None and detail.order is None
    assert detail.quote.policy.outcome == "NEEDS_APPROVAL"
    assert [rule.rule_id for rule in detail.quote.policy.rules_fired] == [
        "APPROVAL_ALWAYS_REQUIRED"
    ]
    assert detail.quote.policy.mandate.always_require_approval is True
    response = client.post(f"/approvals/{detail.approval_id}/approve", json=approval_body(detail))
    assert response.status_code == 200, response.text
    ordered = poll(client, detail.request_id)
    assert ordered.status == "ORDERED" and ordered.order is not None
    assert ordered.order.approved_by == "Invented Approver"
    assert "AVAILABILITY_RECHECKED" in [
        step.type for step in audit(client, detail.request_id).steps
    ]


def test_chef_weekly_order_stays_auto(client: TestClient) -> None:
    response = client.post(
        "/requests",
        json={
            "customer_id": "c_chef",
            "buyer_id": "bistro-example",
            "preset_id": "restaurant_weekly",
        },
    )
    assert response.status_code == 202, response.text
    detail = poll(client, response.json()["request_id"])
    assert detail.status == "ORDERED", detail.error
    assert detail.buyer_id == "bistro-example" and detail.customer_id == "c_chef"
    assert detail.quote is not None and detail.order is not None
    assert detail.quote.policy.outcome == "AUTO" and detail.quote.policy.rules_fired == []
    assert detail.quote.policy.mandate.always_require_approval is False
    assert detail.approval_id is None and detail.order.approved_by is None
    assert audit(client, detail.request_id).steps[-1].type == "ORDER_PLACED"
