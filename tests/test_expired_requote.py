"""Expired approvals refresh once with mocked services and a controllable clock."""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from starlette.requests import Request

from gateway.api.dependencies import Resources
from gateway.audit import verify_chain
from gateway.quotes import compute_content_hash
from gateway.requote import requote_expired
from gateway.runner import AGENT_ID
from gateway.schemas import Quote
from gateway.settings import Settings
from gateway.valio.client import ValioClient, ValioTransportError
from tests.engine_support import BUYER, mandate
from tests.test_budget_options import CUSTOMERS_DIR, set_price
from tests.test_e2e import Clock, poll
from tests.test_exception_gateway import environment
from tests.test_offers import NOW, REQUESTED, stock

IDENTITY = {"buyer_id": BUYER, "agent_id": AGENT_ID}


def prepare(client, *, alternatives: bool = True):
    client.app.state.mandates[BUYER] = mandate(
        allowed_agents=(AGENT_ID,),
        always_require_approval=True,
    )
    lines = [{"item_id": "it_flour", "product_id": "900701", "quantity_sales_units": 1.0}]
    if alternatives:
        lines.append(
            {
                "item_id": "it_flour",
                "product_id": "900702",
                "quantity_sales_units": 2.0,
                "substitute_for": "900701",
                "substitute_source": "agent",
            }
        )
    body = {
        **IDENTITY,
        "requested_date": "2026-11-20",
        "lines": lines,
        "option_label": "Invented recommended",
        "budget": 100.0,
    }
    response = client.post("/gateway/quotes", json=body)
    assert response.status_code == 200, response.text
    recommended = response.json()
    old = [recommended]
    if alternatives:
        for code in ("900702", "900703"):
            response = client.post(
                "/gateway/quotes",
                json={
                    **body,
                    "request_id": recommended["request_id"],
                    "lines": [
                        {"item_id": "it_flour", "product_id": code, "quantity_sales_units": 3.0}
                    ],
                    "option_label": f"Invented option {code}",
                },
            )
            assert response.status_code == 200, response.text
            old.append(response.json())
    response = client.post(
        f"/gateway/quotes/{recommended['quote_id']}/request-approval",
        json={
            **IDENTITY,
            "explanation": "Review invented options",
            "alternative_quote_ids": [quote["quote_id"] for quote in old[1:]],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["approval_id"], old


def approve(client, approval_id: str, quote: dict):
    return client.post(
        f"/approvals/{approval_id}/approve",
        json={
            "approver": "Invented Approver",
            "quote_id": quote["quote_id"],
            "content_hash": quote["content_hash"],
            "comment": "Invented approval",
        },
    )


def line_identity(line: dict):
    return tuple(
        line[key]
        for key in (
            "item_id",
            "product_id",
            "quantity_sales_units",
            "requested_date",
            "quantity_requested",
            "is_split",
            "is_substitute",
            "substitute_for",
            "substitute_source",
        )
    )


def test_expired_approval_returns_410_and_refreshes_all_options_once() -> None:
    with environment(CUSTOMERS_DIR, mode="normal") as client:
        clock = Clock()
        client.app.state.clock = clock
        approval_id, old = prepare(client)
        clock.now += timedelta(minutes=31)
        client.app.state.valio.prices = [
            set_price("900701", 4),
            set_price("900702", 3.35),
            set_price("900703", 3.35),
        ]
        before = len(client.app.state.valio.calls)
        response = approve(client, approval_id, old[0])
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "QUOTE_EXPIRED"
        assert client.get(f"/approvals/{approval_id}").json()["status"] == "EXPIRED"
        assert client.app.state.store.get_approval(approval_id, BUYER).status == "PENDING"
        assert approval_id in {
            entry["approval_id"] for entry in client.get("/approvals?status=EXPIRED").json()
        }
        assert approval_id not in {
            entry["approval_id"] for entry in client.get("/approvals?status=PENDING").json()
        }
        detail = poll(client, old[0]["request_id"])
        assert detail.status == "WAITING_APPROVAL" and detail.error is None
        assert detail.order is None and detail.approval_id != approval_id
        fresh = client.get(f"/approvals/{detail.approval_id}").json()
        assert fresh["status"] == "PENDING" and len(fresh["options"]) == 3
        assert (
            detail.quote.quote_id == fresh["quote"]["quote_id"] == fresh["options"][0]["quote_id"]
        )
        for previous, current in zip(old, fresh["options"], strict=True):
            assert current["quote_id"] != previous["quote_id"]
            assert current["request_id"] == previous["request_id"]
            assert current["buyer_id"] == previous["buyer_id"]
            assert current["agent_id"] == previous["agent_id"]
            assert current["option_label"] == previous["option_label"]
            assert current["budget"] == previous["budget"]
            assert [line_identity(line) for line in current["lines"]] == [
                line_identity(line) for line in previous["lines"]
            ]
            quote = Quote.model_validate_json(json.dumps(current))
            assert quote.content_hash == compute_content_hash(quote)
        changed = [
            rule
            for rule in fresh["quote"]["policy"]["rules_fired"]
            if rule["rule_id"] == "PRICE_CHANGED"
        ]
        assert len(changed) == 1 and changed[0]["product_id"] == "900701"
        assert "3.35 EUR" in changed[0]["description"] and "4.00 EUR" in changed[0]["description"]
        assert fresh["quote"]["total"] == 10.7
        calls = client.app.state.valio.calls[before:]
        assert calls == [
            (endpoint, codes, REQUESTED)
            for codes in (["900701", "900702"], ["900702"], ["900703"])
            for endpoint in ("price", "availability")
        ]
        assert sum(step.type == "REQUOTED" and step.actor == "agent" for step in detail.steps) == 1
        assert verify_chain(detail.steps)
        assert approve(client, approval_id, old[0]).status_code == 410
        assert client.app.state.valio.calls[before:] == calls
        assert len(client.app.state.store.list_approvals(BUYER)) == 2
        assert client.app.state.store.order_for_request(detail.request_id, BUYER) is None
        # A human can subsequently choose and order the fresh quote.
        approved = approve(client, detail.approval_id, fresh["quote"])
        assert approved.status_code == 200, approved.text
        assert approved.json()["order"]["approved_by"] == "Invented Approver"


@pytest.mark.asyncio
async def test_concurrent_requote_attempts_claim_once(monkeypatch: pytest.MonkeyPatch) -> None:
    with environment(CUSTOMERS_DIR, mode="normal") as client:
        clock = Clock()
        client.app.state.clock = clock
        approval_id, old = prepare(client, alternatives=False)
        clock.now += timedelta(minutes=31)
        env = Resources(Request({"type": "http", "app": client.app}))
        approval = env.store.get_approval(approval_id, BUYER)
        valio = client.app.state.valio
        original_price = valio.price

        async def yield_during_price(*args, **kwargs):
            await asyncio.sleep(0)
            return await original_price(*args, **kwargs)

        monkeypatch.setattr(valio, "price", yield_during_price)
        before = len(valio.calls)
        await asyncio.gather(requote_expired(approval, env), requote_expired(approval, env))
        assert [call[0] for call in valio.calls[before:]] == ["price", "availability"]
        assert len(env.store.list_approvals(BUYER)) == 2
        assert env.store.get_approval(approval_id, BUYER).status == "PENDING"
        assert client.get(f"/approvals/{approval_id}").json()["status"] == "EXPIRED"
        steps = env.store.get_steps(old[0]["request_id"], BUYER)
        assert sum(step.type == "QUOTE_EXPIRED" for step in steps) == 1
        assert sum(step.type == "REQUOTED" for step in steps) == 1
        assert verify_chain(steps)


def test_auto_requote_waits_for_a_human_and_rejects_direct_agent_order() -> None:
    with environment(CUSTOMERS_DIR, mode="normal") as client:
        clock = Clock()
        client.app.state.clock = clock
        approval_id, old = prepare(client, alternatives=False)
        client.app.state.mandates[BUYER] = mandate(allowed_agents=(AGENT_ID,))
        clock.now += timedelta(minutes=31)
        assert approve(client, approval_id, old[0]).status_code == 410
        detail = poll(client, old[0]["request_id"])
        assert detail.status == "WAITING_APPROVAL" and detail.order is None
        assert detail.quote.policy.outcome == "AUTO" and not detail.quote.policy.rules_fired
        assert detail.quote.status == "PENDING_APPROVAL"
        denied = client.post(
            "/gateway/orders",
            json={
                **IDENTITY,
                "quote_id": detail.quote.quote_id,
                "idempotency_key": "invented-direct",
            },
        )
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "NEEDS_APPROVAL"
        fresh = client.get(f"/approvals/{detail.approval_id}").json()
        assert approve(client, detail.approval_id, fresh["quote"]).status_code == 200


@pytest.mark.parametrize("failure", ["transport", "unavailable", "alternative_unavailable"])
def test_failed_requote_leaves_clear_error_and_orders_nothing(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    with environment(CUSTOMERS_DIR, mode="normal") as client:
        clock = Clock()
        client.app.state.clock = clock
        approval_id, old = prepare(client)
        clock.now += timedelta(minutes=31)
        valio = client.app.state.valio
        if failure == "transport":

            async def failed_price(*args, **kwargs):
                raise ValioTransportError("invented transport failure")

            monkeypatch.setattr(valio, "price", failed_price)
        else:
            index = 0 if failure == "unavailable" else 2
            valio.stock[index] = stock(valio.stock[index].product_id, scheduleLines=[])
        assert approve(client, approval_id, old[0]).status_code == 410
        detail = poll(client, old[0]["request_id"])
        assert detail.status == "FAILED" and detail.error is not None
        assert "No order was placed" in detail.error.message
        assert detail.order is None
        assert len(client.app.state.store.list_approvals(BUYER)) == 1
        assert client.app.state.store.get_approval(approval_id, BUYER).status == "PENDING"
        assert client.get(f"/approvals/{approval_id}").json()["status"] == "EXPIRED"
        assert client.app.state.store.order_for_request(detail.request_id, BUYER) is None
        assert len(client.app.state.store.list_quotes(detail.request_id, BUYER)) == len(old)
        assert any(step.type == "ERROR" and step.actor == "agent" for step in detail.steps)
        assert verify_chain(detail.steps)
        calls = list(valio.calls)
        assert approve(client, approval_id, old[0]).status_code == 410
        assert valio.calls == calls


def test_freshness_bypasses_real_valio_client_cache() -> None:
    calls = []
    moved = False

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body["products"]))
        if request.url.path == "/api/price":
            return httpx.Response(
                200,
                json={
                    "customerId": BUYER,
                    "currency": "EUR",
                    "pricingDate": body["requestedDate"],
                    "products": [
                        set_price(code, 4 if moved else 3.35).model_dump(mode="json", by_alias=True)
                        for code in body["products"]
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "timeNow": NOW.isoformat(),
                "requestedDate": body["requestedDate"],
                "products": [
                    stock(code).model_dump(mode="json", by_alias=True) for code in body["products"]
                ],
            },
        )

    with environment(CUSTOMERS_DIR, mode="normal") as client:
        clock = Clock()
        client.app.state.clock = clock
        client.app.state.valio = ValioClient(
            Settings(valio_base_url="https://valio.invalid", valio_api_key="invented-test-key"),
            transport=httpx.MockTransport(handler),
            clock=lambda: 0,
        )
        client.app.state.owns_valio = True
        approval_id, old = prepare(client, alternatives=False)
        assert len(calls) == 2
        moved = True
        clock.now += timedelta(minutes=31)
        assert approve(client, approval_id, old[0]).status_code == 410
        detail = poll(client, old[0]["request_id"])
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert detail.quote.lines[0].unit_price == 4
        assert len(calls) == 4
        assert {rule.rule_id for rule in detail.quote.policy.rules_fired} >= {"PRICE_CHANGED"}


def test_normal_auto_quote_still_cannot_request_human_approval() -> None:
    with environment(CUSTOMERS_DIR, mode="normal") as client:
        response = client.post(
            "/gateway/quotes",
            json={
                **IDENTITY,
                "requested_date": "2026-11-20",
                "lines": [
                    {"item_id": "it_flour", "product_id": "900701", "quantity_sales_units": 1.0},
                ],
            },
        )
        quote = response.json()
        assert quote["policy"]["outcome"] == "AUTO"
        response = client.post(
            f"/gateway/quotes/{quote['quote_id']}/request-approval",
            json={
                **IDENTITY,
                "explanation": "Invented unnecessary approval",
            },
        )
        assert response.status_code == 403


def test_expiry_during_availability_recheck_also_requotes(monkeypatch: pytest.MonkeyPatch) -> None:
    with environment(CUSTOMERS_DIR, mode="normal") as client:
        clock = Clock()
        client.app.state.clock = clock
        approval_id, old = prepare(client, alternatives=False)
        valio = client.app.state.valio
        availability = valio.availability

        async def expire_during_check(*args, **kwargs):
            result = await availability(*args, **kwargs)
            clock.now = NOW + timedelta(minutes=31)
            return result

        monkeypatch.setattr(valio, "availability", expire_during_check)
        assert approve(client, approval_id, old[0]).status_code == 410
        detail = poll(client, old[0]["request_id"])
        assert detail.status == "WAITING_APPROVAL" and detail.order is None
        assert detail.approval_id != approval_id
        assert len(client.app.state.store.list_approvals(BUYER)) == 2
