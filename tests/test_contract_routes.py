"""HTTP contracts with invented catalogue records, isolated profiles and no network."""

import json
from collections.abc import Iterator
from datetime import timedelta
from itertools import count
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.schemas import (
    ApprovalDetail,
    ApprovalListEntry,
    ApproveResponse,
    Audit,
    Customer,
    CustomerSummary,
    OffersResponse,
    Order,
    Preference,
    Preset,
    Quote,
    RequestDetail,
    RequestListEntry,
    SearchResponse,
)
from gateway.settings import Settings
from gateway.store import MemoryStore
from gateway.valio.client import ValioClient, ValioTransportError
from tests.test_offers import NOW, FakeValio, price, product, schedule, stock

IDENTITY = {"buyer_id": "demo-catering", "agent_id": "limos-runner"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    ids = count(1)
    records = [
        product(
            "900101",
            names={"en": "Invented lactose-free milk"},
            pack_size={"value": 1, "unit": "L"},
            claims=["lactose_free"],
            allergens={"free_from": ["lactose"], "data_available": True},
        ),
        product("900102", names={"en": "Invented butter"}),
        product("900103", names={"en": "Invented potatoes"}, country_of_origin="fi"),
        product("900104", names={"en": "Invented eggs"}, pack_size={"value": 30, "unit": "pcs"}),
    ]
    fake = FakeValio([price(p.product_id) for p in records], [stock(p.product_id) for p in records])
    profile = Customer.model_validate_json("""{
      "customer_id":"invented-customer","name":"Invented Cook","company":"Invented Kitchen",
      "role":"Chef","buyer_id":"demo-catering","age_group":"35_to_54","playful":false,
      "preferences":[],"exclude_allergens":[],"defaults":{"priority":"quality",
      "typical_guests":10,"budget":null},"order_history":[{"order_ref":"invented-past",
      "date":"2026-10-01","title":"Invented staples","event_type":"lunch","guests":10,
      "lines":[{"product_id":"900102","name":"Invented butter","quantity_sales_units":2,
      "sales_unit":"PRK","quantity_text":"2 x 1 kg","unit_price":3.35,"line_total":6.7}],
      "total":6.7,"source":"seeded","simulated":true}],"simulated":true}""")
    (tmp_path / "invented-customer.json").write_text(profile.model_dump_json(), encoding="utf-8")
    app = create_app(
        Settings(store="memory", valio_api_key="", catalog_path=str(tmp_path / "missing.json")),
        catalog=Catalog(records),
        valio=fake,
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: str(next(ids)),
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )
    with TestClient(app) as result:
        yield result


def quote_body(**changes: Any) -> dict[str, Any]:
    return {
        **IDENTITY,
        "requested_date": "2026-11-17",
        "lines": [{"item_id": "it_1", "product_id": "900102", "quantity_sales_units": 2}],
        **changes,
    }


def pending(client: TestClient) -> tuple[dict[str, Any], str]:
    response = client.post(
        "/gateway/quotes",
        json=quote_body(
            lines=[{"item_id": "it_1", "product_id": "900102", "quantity_sales_units": 80}]
        ),
    )
    assert response.status_code == 200, response.text
    quote = response.json()
    assert quote["policy"]["outcome"] == "NEEDS_APPROVAL"
    response = client.post(
        f"/gateway/quotes/{quote['quote_id']}/request-approval",
        json={**IDENTITY, "explanation": "The line needs human approval."},
    )
    assert response.status_code == 200, response.text
    return quote, response.json()["approval_id"]


def approve_body(quote: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "approver": "Invented Approver",
        "quote_id": quote["quote_id"],
        "content_hash": quote["content_hash"],
        "comment": "Approved",
        **changes,
    }


def test_presets_and_preset_runner(client: TestClient) -> None:
    presets = client.get("/presets")
    assert presets.status_code == 200
    assert {Preset.model_validate_json(json.dumps(p)).preset_id for p in presets.json()} == {
        "restaurant_weekly",
        "wedding_120",
        "birthday_kids_25",
        "company_lunch_60",
    }
    response = client.post("/requests", json={"preset_id": "restaurant_weekly"})
    assert response.status_code == 202, response.text
    request_id = response.json()["request_id"]
    response = client.get(f"/requests/{request_id}")
    detail = RequestDetail.model_validate_json(response.text)
    assert detail.status == "ORDERED", detail.error
    assert detail.intent.event is None and detail.intent.dishes == []
    assert detail.intent.assumptions == []
    assert [item.item_id for item in detail.intent.items] == ["it_1", "it_2", "it_3", "it_4"]
    assert len(detail.candidates) == 4
    assert detail.order and detail.order.simulated
    audit = Audit.model_validate_json(client.get(f"/audit/{request_id}").text)
    assert audit.chain_valid and audit.steps == detail.steps
    types = [step.type for step in audit.steps]
    assert types[:2] == ["REQUEST_RECEIVED", "INTENT_PARSED"]
    assert types.count("TOOL_CALL") == 10
    assert types[-3:] == ["POLICY_DECISION", "TOOL_CALL", "ORDER_PLACED"]
    listing = client.get("/requests", params={"buyer_id": IDENTITY["buyer_id"]})
    entry = RequestListEntry.model_validate_json(json.dumps(listing.json()[0]))
    assert entry.request_id == request_id and entry.status == "ORDERED"


@pytest.mark.parametrize(
    "body",
    [
        {"text": "Invented lunch"},
        {"event": {"type": "lunch", "date": "2026-11-20", "guests": 20, "dishes": []}},
    ],
)
def test_agent_not_available(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/requests", json={"buyer_id": "demo-catering", **body})
    assert response.status_code == 202
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["status"] == "FAILED" and detail["error"]["code"] == "AGENT_NOT_AVAILABLE"
    assert detail["intent"]["deliver_by"] == "2026-11-15"
    assert any(
        gap["field"] == "deliver_by" and gap["source"] == "default"
        for gap in detail["intent"]["filled_gaps"]
    )
    assert detail["steps"][-1]["type"] == "ERROR"


def test_customers_and_reorder_history(client: TestClient) -> None:
    response = client.get("/customers")
    assert response.status_code == 200
    assert CustomerSummary.model_validate_json(json.dumps(response.json()[0])).simulated
    customer = Customer.model_validate_json(client.get("/customers/invented-customer").text)
    assert len(customer.order_history) == 1
    response = client.post(
        "/requests",
        json={
            "customer_id": customer.customer_id,
            "reorder_of": "invented-past",
            "requested_date": "2026-11-17",
            "event": {"type": "lunch", "date": "2026-11-18", "guests": 20, "dishes": []},
        },
    )
    assert response.status_code == 202, response.text
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["status"] == "ORDERED", detail["error"]
    assert detail["intent"]["based_on"]["scale"] == 2
    assert detail["order"]["lines"][0]["quantity_sales_units"] == 4
    assert detail["intent"]["priority"] == "quality"
    history = client.get("/customers/invented-customer").json()["order_history"]
    assert len(history) == 2 and history[0]["source"] == "limos"
    assert history[0]["guests"] == 20
    listing = client.get("/requests", params={"customer_id": customer.customer_id})
    assert len(listing.json()) == 1
    assert "CUSTOMER_LOADED" in [s["type"] for s in detail["steps"]]


def test_gateway_happy_paths_and_buyer_isolation(client: TestClient) -> None:
    search = client.post(
        "/gateway/search",
        json={
            **IDENTITY,
            "query": "butter",
            "constraints": [],
            "exclude_allergens": [],
            "limit": 1,
        },
    )
    assert search.status_code == 200
    assert SearchResponse.model_validate_json(search.text).products[0].product_id == "900102"
    offers = client.post(
        "/gateway/offers",
        json={
            **IDENTITY,
            "product_ids": ["900102"],
            "item_id": "it_1",
            "quantity": 2,
            "unit": "kg",
            "requested_date": "2026-11-17",
            "constraints": [],
            "exclude_allergens": [],
        },
    )
    assert offers.status_code == 200, offers.text
    assert len(OffersResponse.model_validate_json(offers.text).offers) == 1
    response = client.post("/gateway/quotes", json=quote_body())
    quote = Quote.model_validate_json(response.text)
    body = {**IDENTITY, "quote_id": quote.quote_id, "idempotency_key": "invented-key"}
    for endpoint, payload in [
        ("/gateway/orders", {**body, "buyer_id": "ab-oy"}),
        (
            f"/gateway/quotes/{quote.quote_id}/request-approval",
            {**IDENTITY, "buyer_id": "ab-oy", "explanation": "Invented"},
        ),
    ]:
        assert client.post(endpoint, json=payload).status_code == 404
    response = client.post("/gateway/orders", json=body)
    assert response.status_code == 200, response.text
    order = Order.model_validate_json(response.text)
    assert client.post("/gateway/orders", json=body).json() == response.json()
    assert (
        client.get(f"/gateway/orders/{order.order_id}", params=IDENTITY).json() == response.json()
    )
    assert (
        client.get(
            f"/gateway/orders/{order.order_id}", params={**IDENTITY, "buyer_id": "ab-oy"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/gateway/quotes", json=quote_body(request_id=quote.request_id, buyer_id="ab-oy")
        ).status_code
        == 404
    )


@pytest.mark.parametrize("identity", [{"buyer_id": "unknown"}, {"agent_id": "not-permitted"}])
@pytest.mark.parametrize(
    "endpoint,body",
    [
        (
            "/gateway/search",
            {"query": "butter", "constraints": [], "exclude_allergens": [], "limit": 5},
        ),
        (
            "/gateway/offers",
            {
                "product_ids": ["900102"],
                "item_id": "it_1",
                "quantity": 2,
                "unit": "kg",
                "requested_date": "2026-11-17",
                "constraints": [],
                "exclude_allergens": [],
            },
        ),
        ("/gateway/quotes", {"requested_date": "2026-11-17", "lines": []}),
        ("/gateway/quotes/missing/request-approval", {"explanation": "Invented"}),
        ("/gateway/orders", {"quote_id": "missing", "idempotency_key": "key"}),
    ],
)
def test_agent_permissions(
    client: TestClient, identity: dict[str, str], endpoint: str, body: dict[str, Any]
) -> None:
    response = client.post(endpoint, json={**IDENTITY, **body, **identity})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AGENT_NOT_PERMITTED"
    response = client.get("/gateway/orders/missing", params={**IDENTITY, **identity})
    assert response.status_code == 403


def test_approval_happy_path(client: TestClient) -> None:
    quote, approval_id = pending(client)
    request_id = quote["request_id"]
    entries = client.get("/approvals", params={"status": "PENDING", "buyer_id": "demo-catering"})
    assert len(entries.json()) == 1
    ApprovalListEntry.model_validate_json(json.dumps(entries.json()[0]))
    assert client.get("/approvals", params={"buyer_id": "ab-oy"}).json() == []
    detail = ApprovalDetail.model_validate_json(client.get(f"/approvals/{approval_id}").text)
    assert detail.status == "PENDING" and detail.request.request_id == request_id
    blocked = client.post(
        "/gateway/orders",
        json={**IDENTITY, "quote_id": quote["quote_id"], "idempotency_key": "early"},
    )
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "NEEDS_APPROVAL"
    fake = client.app.state.valio
    before = len(fake.calls)
    response = client.post(f"/approvals/{approval_id}/approve", json=approve_body(quote))
    assert response.status_code == 200, response.text
    result = ApproveResponse.model_validate_json(response.text)
    assert result.order.approved_by == "Invented Approver"
    assert result.order.placed_by.name == quote["agent_id"]
    assert result.order.idempotency_key == f"{request_id}-{quote['quote_id']}"
    assert [call[0] for call in fake.calls[before:]] == ["availability"]
    assert client.get(f"/requests/{request_id}").json()["status"] == "ORDERED"
    audit = client.get(f"/audit/{request_id}").json()
    assert audit["chain_valid"]
    assert [s["type"] for s in audit["steps"]][-3:] == [
        "AVAILABILITY_RECHECKED",
        "APPROVED",
        "ORDER_PLACED",
    ]
    assert client.post(
        "/gateway/orders",
        json={
            **IDENTITY,
            "quote_id": quote["quote_id"],
            "idempotency_key": result.order.idempotency_key,
        },
    ).json() == result.order.model_dump(mode="json")
    assert (
        client.post(f"/approvals/{approval_id}/approve", json=approve_body(quote)).status_code
        == 409
    )


@pytest.mark.parametrize(
    "change", ["hash", "quote_id", "content", "expired", "stock", "sales_block", "late"]
)
def test_approval_error_codes(client: TestClient, change: str) -> None:
    quote, approval_id = pending(client)
    body = approve_body(quote)
    expected_status, expected_code = 409, "QUOTE_CHANGED"
    if change in {"hash", "quote_id"}:
        body["content_hash" if change == "hash" else "quote_id"] = "changed"
    elif change == "content":
        locked = client.app.state.store.get_quote(quote["quote_id"], "demo-catering")
        locked.lines[0].line_total += 1
        client.app.state.store.save_quote(locked)
    elif change == "expired":
        client.app.state.clock = lambda: NOW + timedelta(hours=1)
        expected_status, expected_code = 410, "QUOTE_EXPIRED"
    else:
        fake = client.app.state.valio
        if change == "stock":
            fake.stock = [stock("900102", scheduleLines=[schedule(79)])]
        elif change == "sales_block":
            fake.stock = [stock("900102", salesBlock=True)]
        else:
            fake.stock = [
                stock("900102", scheduleLines=[schedule(100, availableDate="2026-11-25")])
            ]
        expected_code = "AVAILABILITY_CHANGED"
    before = len(client.app.state.valio.calls)
    response = client.post(f"/approvals/{approval_id}/approve", json=body)
    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == expected_code
    assert client.app.state.store.order_for_request(quote["request_id"], "demo-catering") is None
    assert client.app.state.store.get_approval(approval_id, "demo-catering").status == "PENDING"
    assert len(client.app.state.valio.calls) - before == (
        1 if expected_code == "AVAILABILITY_CHANGED" else 0
    )


def test_reject(client: TestClient) -> None:
    quote, approval_id = pending(client)
    response = client.post(
        f"/approvals/{approval_id}/reject",
        json={"approver": "Invented Approver", "reason": "Over budget"},
    )
    assert response.status_code == 200 and response.json() == {"status": "REJECTED"}
    assert client.get(f"/requests/{quote['request_id']}").json()["status"] == "REJECTED"
    assert client.get(f"/approvals/{approval_id}").json()["status"] == "REJECTED"


def test_search_filters_before_limit(client: TestClient) -> None:
    client.app.state.catalog = Catalog(
        [
            product("900201", allergens={"contains": ["milk"]}),
            product("900202", claims=["low_lactose"]),
            product("900203", sales_block=True),
            product("900204", country_of_origin="se"),
            product("900205", claims=["lactose_free"], country_of_origin="fi"),
            product("900206", claims=["lactose_free"], country_of_origin="fi"),
        ]
    )
    response = client.post(
        "/gateway/search",
        json={
            **IDENTITY,
            "query": "flour",
            "constraints": ["lactose_free", "finnish_origin"],
            "exclude_allergens": ["milk"],
            "limit": 1,
        },
    )
    assert response.status_code == 200
    assert [p["product_id"] for p in response.json()["products"]] == ["900205"]
    assert client.app.state.valio.calls == []


@pytest.mark.parametrize(
    "path", ["/customers/missing", "/requests/missing", "/approvals/missing", "/audit/missing"]
)
def test_missing_resources(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    if path.startswith("/customers"):
        assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"


def test_missing_catalogue_and_validation(client: TestClient) -> None:
    client.app.state.catalog = None
    assert client.get("/health").status_code == 200
    for path, body in [
        (
            "/gateway/search",
            {**IDENTITY, "query": "butter", "constraints": [], "exclude_allergens": [], "limit": 5},
        ),
        ("/gateway/quotes", quote_body()),
    ]:
        response = client.post(path, json=body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CATALOG_UNAVAILABLE"
    assert client.post("/requests", json={"buyer_id": "demo-catering"}).status_code == 422


def test_invalid_quantities_and_missing_product(client: TestClient) -> None:
    for units, code in [(0, "900102"), (-1, "900102"), (1.5, "900102"), (1, "900999")]:
        response = client.post(
            "/gateway/quotes",
            json=quote_body(
                lines=[{"item_id": "it_1", "product_id": code, "quantity_sales_units": units}]
            ),
        )
        assert response.status_code == (404 if code == "900999" else 422)


def test_runner_unexpected_failure(client: TestClient) -> None:
    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Private failure details")

    client.app.state.valio.price = fail
    response = client.post("/requests", json={"preset_id": "restaurant_weekly"})
    assert response.status_code == 202
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["status"] == "FAILED" and detail["error"]["code"] == "INTERNAL_ERROR"
    assert "Private" not in str(detail)


def test_valio_error_is_sanitized(client: TestClient) -> None:
    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValioTransportError("Private upstream details")

    client.app.state.valio.price = fail
    response = client.post("/gateway/quotes", json=quote_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "VALIO_UNAVAILABLE"
    assert "Private" not in response.text


async def test_fresh_availability_bypasses_cache() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "timeNow": NOW.isoformat(),
                "requestedDate": "2026-11-17",
                "products": [stock("900102").model_dump(mode="json", by_alias=True)],
            },
        )

    async with ValioClient(
        Settings(valio_api_key="invented", valio_base_url="https://invented.invalid"),
        transport=httpx.MockTransport(handle),
    ) as valio:
        await valio.availability(["900102"], NOW.date())
        await valio.availability(["900102"], NOW.date())
        assert len(calls) == 1
        await valio.availability(["900102"], NOW.date(), refresh=True)
        assert len(calls) == 2


def test_presets_match_fixtures_and_overrides_win(client: TestClient) -> None:
    expected = json.loads(Path("fixtures/api/presets.json").read_text(encoding="utf-8"))
    assert sorted(client.get("/presets").json(), key=lambda p: p["preset_id"]) == sorted(
        expected, key=lambda p: p["preset_id"]
    )
    response = client.post(
        "/requests",
        json={
            "preset_id": "restaurant_weekly",
            "requested_date": "2026-11-18",
            "priority": "quality",
            "items": [{"description": "butter", "quantity": 2, "unit": "kg", "constraints": []}],
        },
    )
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["status"] == "ORDERED"
    assert len(detail["intent"]["items"]) == 1
    assert detail["intent"]["deliver_by"] == "2026-11-18"
    assert detail["intent"]["priority"] == "quality"


def test_customer_history_after_human_approval(client: TestClient) -> None:
    response = client.post(
        "/requests",
        json={
            "customer_id": "invented-customer",
            "requested_date": "2026-11-17",
            "items": [{"description": "butter", "quantity": 80, "unit": "kg", "constraints": []}],
        },
    )
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["status"] == "WAITING_APPROVAL"
    response = client.post(
        f"/approvals/{detail['approval_id']}/approve", json=approve_body(detail["quote"])
    )
    assert response.status_code == 200, response.text
    history = client.get("/customers/invented-customer").json()["order_history"]
    assert len(history) == 2 and history[0]["source"] == "limos"
    order = response.json()["order"]
    retry = client.post(
        "/gateway/orders",
        json={
            **IDENTITY,
            "quote_id": order["quote_id"],
            "idempotency_key": order["idempotency_key"],
        },
    )
    assert retry.status_code == 200
    assert len(client.get("/customers/invented-customer").json()["order_history"]) == 2


def test_reorder_skips_missing_product(client: TestClient) -> None:
    client.app.state.catalog = Catalog([])
    response = client.post(
        "/requests", json={"customer_id": "invented-customer", "reorder_of": "invented-past"}
    )
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["status"] == "FAILED" and detail["order"] is None
    assert any(s["type"] == "EXCEPTION" and "900102" in s["title"] for s in detail["steps"])
    assert client.get(f"/audit/{detail['request_id']}").json()["chain_valid"]


@pytest.mark.parametrize("failure", ["not_deliverable", "price_missing", "sales_block"])
def test_runner_fallback_reaches_blocked_quote(client: TestClient, failure: str) -> None:
    fake = client.app.state.valio
    if failure == "not_deliverable":
        fake.stock = [stock("900102", scheduleLines=[])]
    elif failure == "sales_block":
        fake.stock = [stock("900102", salesBlock=True)]
    else:
        fake.prices = [price("900102", error=True, netPriceFound=False)]
    response = client.post(
        "/requests",
        json={
            "buyer_id": "demo-catering",
            "requested_date": "2026-11-17",
            "items": [{"description": "butter", "quantity": 2, "unit": "kg", "constraints": []}],
        },
    )
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["status"] == "BLOCKED", detail["error"]
    assert detail["quote"]["policy"]["outcome"] == "BLOCK" and detail["order"] is None
    response = client.post(
        "/gateway/orders",
        json={**IDENTITY, "quote_id": detail["quote"]["quote_id"], "idempotency_key": "blocked"},
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "BLOCKED"


def test_gateway_expired_quote_and_idempotency_conflict(client: TestClient) -> None:
    quote = client.post("/gateway/quotes", json=quote_body()).json()
    response = client.post(
        "/gateway/orders",
        json={**IDENTITY, "quote_id": quote["quote_id"], "idempotency_key": "used"},
    )
    assert response.status_code == 200
    other = client.post("/gateway/quotes", json=quote_body()).json()
    response = client.post(
        "/gateway/orders",
        json={**IDENTITY, "quote_id": other["quote_id"], "idempotency_key": "used"},
    )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    client.app.state.clock = lambda: NOW + timedelta(hours=1)
    response = client.post(
        "/gateway/orders",
        json={**IDENTITY, "quote_id": other["quote_id"], "idempotency_key": "expired"},
    )
    assert response.status_code == 410 and response.json()["error"]["code"] == "QUOTE_EXPIRED"


def test_approval_rechecks_combined_quantity_for_repeated_product(client: TestClient) -> None:
    response = client.post(
        "/gateway/quotes",
        json=quote_body(
            lines=[
                {"item_id": "it_1", "product_id": "900102", "quantity_sales_units": 80},
                {"item_id": "it_2", "product_id": "900102", "quantity_sales_units": 30},
            ]
        ),
    )
    quote = response.json()
    response = client.post(
        f"/gateway/quotes/{quote['quote_id']}/request-approval",
        json={**IDENTITY, "explanation": "The line limit needs approval."},
    )
    approval_id = response.json()["approval_id"]
    response = client.post(f"/approvals/{approval_id}/approve", json=approve_body(quote))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AVAILABILITY_CHANGED"
    assert client.app.state.valio.calls[-1][1] == ["900102"]


def test_profile_constraints_and_gap_provenance(client: TestClient) -> None:
    path = client.app.state.customers_dir / "invented-customer.json"
    customer = Customer.model_validate_json(path.read_bytes())
    customer.preferences = [Preference(key="vegan", level="require")]
    customer.exclude_allergens = ["milk"]
    path.write_text(customer.model_dump_json(), encoding="utf-8")
    response = client.post(
        "/requests",
        json={
            "customer_id": customer.customer_id,
            "requested_date": "2026-11-17",
            "items": [
                {"description": "butter", "quantity": 2, "unit": "kg", "constraints": ["organic"]}
            ],
        },
    )
    detail = client.get(f"/requests/{response.json()['request_id']}").json()
    assert detail["intent"]["items"][0]["constraints"] == ["organic", "vegan", "no_milk"]
    assert detail["status"] == "WAITING_APPROVAL"
    assert {g["field"] for g in detail["intent"]["filled_gaps"]} >= {
        "buyer_id",
        "preferences.vegan",
        "exclude_allergens",
        "priority",
    }
    assert all(g["source"] == "profile" for g in detail["intent"]["filled_gaps"])


def test_missing_offers_catalogue(client: TestClient) -> None:
    client.app.state.catalog = None
    response = client.post(
        "/gateway/offers",
        json={
            **IDENTITY,
            "product_ids": ["900102"],
            "item_id": "it_1",
            "quantity": 2,
            "unit": "kg",
            "requested_date": "2026-11-17",
            "constraints": [],
            "exclude_allergens": [],
        },
    )
    assert response.status_code == 503 and response.json()["error"]["code"] == "CATALOG_UNAVAILABLE"
