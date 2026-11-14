"""Gemini exception decisions preserve fallback ordering and hard allergen exclusions."""

from pathlib import Path

import pytest

from agent.gateway_client import GatewayClient
from agent.llm import FakeLLM, ToolRequest
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.schemas import SearchBody, SearchResponse
from tests.agent_loop_support import EXPLANATION, purchase
from tests.engine_support import BUYER
from tests.test_agent_loop import environment, responses, start
from tests.test_e2e import poll
from tests.test_offers import product


def test_undeliverable_pick_quotes_next_usable_candidate(tmp_path: Path) -> None:
    turns = purchase("900703")
    turns[1] = [
        ToolRequest(
            "check_offers",
            {"item_id": "it_1", "product_ids": ["900703", "900702", "900701"]},
        )
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION])
    with environment(tmp_path, llm) as client:
        detail = start(client)
        assert detail.status == "ORDERED", detail.error
        assert detail.quote is not None
        assert [(line.product_id, line.quantity_sales_units) for line in detail.quote.lines] == [
            ("900702", 2)
        ]
        assert detail.order is not None and detail.order.quote_id == detail.quote.quote_id
        unavailable = next(offer for offer in detail.candidates if offer.product_id == "900703")
        assert "NOT_DELIVERABLE" in unavailable.flags
        assert {"output": {"error": "OFFER_EXCLUDED"}} not in responses(llm)
        decision = next(step for step in detail.steps if step.type == "EXCEPTION")
        assert decision.actor == "agent" and decision.model is None
        assert decision.detail["item_id"] == "it_1"
        assert decision.title == (
            "Replaced unavailable or late Invented unavailable flour with Invented baking flour"
        )
        assert verify_chain(detail.steps)


def test_excluded_allergen_pick_returns_error_without_creating_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_products = GatewayClient.search_products

    def unfiltered_search(self: GatewayClient, body: SearchBody) -> SearchResponse:
        # Let an unsafe product reach the shortlist to exercise request_quote's own guard.
        assert body.exclude_allergens == ["milk"]
        return search_products(
            self, body.model_copy(update={"exclude_allergens": [], "constraints": []})
        )

    monkeypatch.setattr(GatewayClient, "search_products", unfiltered_search)
    turns = purchase("900701")
    turns[1] = [
        ToolRequest("check_offers", {"item_id": "it_1", "product_ids": ["900701", "900702"]})
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION])
    with environment(tmp_path, llm) as client:
        client.app.state.catalog = Catalog(
            [
                product(
                    "900701",
                    names={"en": "Invented milk-containing flour"},
                    allergens={"contains": ["milk"], "data_available": True},
                ),
                product("900702", names={"en": "Invented baking flour"}),
            ]
        )
        response = client.post(
            "/requests",
            json={
                "buyer_id": BUYER,
                "requested_date": "2026-11-20",
                "items": [
                    {"description": "flour", "quantity": 2.0, "unit": "kg", "constraints": []}
                ],
                "exclude_allergens": ["milk"],
            },
        )
        assert response.status_code == 202, response.text
        detail = poll(client, response.json()["request_id"])
        unsafe = next(offer for offer in detail.candidates if offer.product_id == "900701")
        assert unsafe.allergens.contains == ["milk"]
        assert {"output": {"error": "OFFER_EXCLUDED"}} in responses(llm)
        assert detail.status == "FAILED" and detail.error.code == "AGENT_FAILED"
        assert detail.quote is None and detail.order is None and detail.approval_id is None
        assert client.app.state.store.list_quotes(detail.request_id, BUYER) == []
        assert not any(step.type == "QUOTE_CREATED" for step in detail.steps)
        assert verify_chain(detail.steps)
