"""Plain REST flows use the real gateway with invented, injected resources only."""

import json
from collections.abc import Iterator
from itertools import count
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from clients.plain_client import AGENT_ID, main, run_flow
from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.settings import Settings
from gateway.store import MemoryStore
from tests.engine_support import mandate
from tests.test_offers import NOW, FakeValio, price, product, stock

BUYER = "demo-catering"


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[tuple[TestClient, MemoryStore, list[httpx.Request]]]:
    records = [
        product("900701", names={"en": "Invented unavailable butter"}),
        product("900702", names={"en": "Invented dairy butter"}),
        product(
            "900703",
            names={"en": "Invented lactose-free milk"},
            pack_size={"value": 1, "unit": "L"},
            claims=["lactose_free"],
        ),
        product(
            "900704",
            names={"en": "Invented milk with lactose"},
            pack_size={"value": 1, "unit": "L"},
            allergens={"contains": ["lactose"], "data_available": True},
        ),
    ]
    store = MemoryStore()
    ids = count(1)
    app = create_app(
        Settings(store="memory", valio_api_key="", catalog_path=str(tmp_path / "missing.json")),
        catalog=Catalog(records),
        valio=FakeValio(
            [price(record.product_id) for record in records],
            [
                stock("900701", scheduleLines=[]),
                stock("900702", closingTime="2026-11-13T17:00:00+02:00"),
                stock("900703"),
                stock("900704"),
            ],
        ),
        store=store,
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={BUYER: mandate(buyer_id=BUYER, allowed_agents=(AGENT_ID,))},
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )
    requests: list[httpx.Request] = []
    with TestClient(app) as client:
        client.event_hooks["request"].append(requests.append)
        yield client, store, requests


@pytest.mark.parametrize("outcome", ["AUTO", "NEEDS_APPROVAL", "BLOCK"])
def test_order_flow(
    environment: tuple[TestClient, MemoryStore, list[httpx.Request]],
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    client, store, requests = environment
    if outcome != "AUTO":
        client.app.state.mandates[BUYER] = mandate(
            buyer_id=BUYER,
            allowed_agents=(AGENT_ID,),
            auto_limit=100.0,
            hard_cap=100.0 if outcome == "BLOCK" else 5000.0,
        )
    assert main([], client=client) == (1 if outcome == "BLOCK" else 0)
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith(f"Outcome: {outcome}\n")
    assert "Invented dairy butter: 5.0 x 1 kg | 16.75 EUR net" in output.out
    assert "Invented lactose-free milk: 40.0 x 1 l | 134.00 EUR net" in output.out
    assert "Total: 150.75 EUR net" in output.out
    assert "Delivery date: 2026-11-17" in output.out
    assert "Order deadline: 2026-11-19T17:00:00+02:00" in output.out
    assert "Invented unavailable butter" not in output.out
    assert "Invented milk with lactose" not in output.out
    assert "product_id" not in output.out

    payloads = [json.loads(request.content) for request in requests]
    assert all(body["buyer_id"] == BUYER and body["agent_id"] == AGENT_ID for body in payloads)
    offers = [
        body
        for request, body in zip(requests, payloads, strict=True)
        if request.url.path.endswith("/offers")
    ]
    assert offers[0]["quantity"] == 5.0 and offers[0]["unit"] == "kg"
    assert offers[1]["quantity"] == 40.0 and offers[1]["unit"] == "L"
    assert offers[1]["constraints"] == ["lactose_free"]
    assert offers[1]["product_ids"] == ["900703"]
    paths = [request.url.path for request in requests]
    assert paths[:5] == [
        "/gateway/search",
        "/gateway/offers",
        "/gateway/search",
        "/gateway/offers",
        "/gateway/quotes",
    ]
    quotes = store.list_requests(BUYER)
    assert len(quotes) == 1
    detail = quotes[0]
    quote = detail.quote
    assert quote is not None
    assert [line.product_id for line in quote.lines] == ["900702", "900703"]
    assert quote.policy.outcome == outcome
    if outcome == "AUTO":
        assert "Rules fired: none" in output.out
        order = store.order_for_request(detail.request_id, BUYER)
        assert order is not None and order.simulated
        assert f"Order id: {order.order_id} (simulated)" in output.out
        assert paths[5:] == ["/gateway/orders"]
        assert quote.quote_id in payloads[-1]["idempotency_key"]
        # Retrying the actual client's payload must return the same order.
        retry = client.post("/gateway/orders", json=payloads[-1])
        assert retry.status_code == 200
        assert retry.json()["order_id"] == order.order_id
        assert store.list_approvals(BUYER) == []
    elif outcome == "NEEDS_APPROVAL":
        assert "Rules fired: SPEND_SOFT_LIMIT:" in output.out
        approvals = store.list_approvals(BUYER)
        assert len(approvals) == 1 and approvals[0].status == "PENDING"
        assert f"Approval id: {approvals[0].approval_id}" in output.out
        assert paths[5:] == [f"/gateway/quotes/{quote.quote_id}/request-approval"]
        assert store.order_for_request(detail.request_id, BUYER) is None
    else:
        assert "SPEND_HARD_CAP:" in output.out
        assert len(paths) == 5
        assert "Order id:" not in output.out and "Approval id:" not in output.out
        assert store.order_for_request(detail.request_id, BUYER) is None
        assert store.list_approvals(BUYER) == []


def test_gateway_error(
    environment: tuple[TestClient, MemoryStore, list[httpx.Request]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, store, requests = environment
    assert main(["--buyer-id", "invented-unpermitted-buyer"], client=client) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "AGENT_NOT_PERMITTED: Agent is not permitted for this buyer.\n"
    assert len(requests) == 1
    assert store.list_requests(BUYER) == []


@pytest.mark.parametrize("empty_search", [True, False])
def test_no_usable_product(
    environment: tuple[TestClient, MemoryStore, list[httpx.Request]],
    capsys: pytest.CaptureFixture[str],
    empty_search: bool,
) -> None:
    client, store, requests = environment
    client.app.state.catalog = Catalog(
        [] if empty_search else [product("900701", names={"en": "Invented unavailable butter"})]
    )
    assert run_flow(client) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("NO_CANDIDATES:" if empty_search else "NO_USABLE_OFFERS:")
    assert all(request.url.path in {"/gateway/search", "/gateway/offers"} for request in requests)
    assert store.list_requests(BUYER) == []
