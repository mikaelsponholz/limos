"""Offline Gemini purchases exercise the real HTTP tools and deterministic engine."""

import json
from itertools import count
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from agent.gateway_client import AGENT_ID, GatewayClient, GatewayClientError
from agent.llm import FakeLLM, ToolRequest
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.quotes import compute_content_hash
from gateway.runner import AGENT_ID as RUNNER_ID
from gateway.schemas import RequestDetail, SearchBody
from gateway.settings import Settings
from gateway.store import MemoryStore
from tests.agent_loop_support import EXPLANATION, bind_client, purchase
from tests.engine_support import BUYER, mandate
from tests.test_e2e import poll
from tests.test_offers import NOW, FakeValio, price, product, stock


def environment(tmp_path: Path, llm: FakeLLM, *, auto_limit: float = 1500) -> TestClient:
    ids = count(1)
    app = create_app(
        Settings(
            agent_mode="tools",
            gemini_api_key="",
            gemini_model="",
            gemini_expansion_model="",
            store="memory",
        ),
        llm=llm,
        catalog=Catalog(
            [
                product("900701", names={"en": "Invented flour decoy"}),
                product("900702", names={"en": "Invented baking flour"}),
                product("900703", names={"en": "Invented unavailable flour"}),
                product("900799", names={"en": "Invented unrelated carrots"}),
            ]
        ),
        valio=FakeValio(
            [price(code) for code in ("900701", "900702", "900703", "900799")],
            [
                stock(code, scheduleLines=[]) if code == "900703" else stock(code)
                for code in ("900701", "900702", "900703", "900799")
            ],
        ),
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={BUYER: mandate(allowed_agents=(RUNNER_ID, AGENT_ID), auto_limit=auto_limit)},
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )
    return bind_client(TestClient(app), llm)


def start(client: TestClient, *, two_items: bool = False) -> RequestDetail:
    items = [{"description": "flour", "quantity": 2.0, "unit": "kg", "constraints": []}]
    if two_items:
        items.append({**items[0], "quantity": 3.0})
    response = client.post(
        "/requests",
        json={
            "buyer_id": BUYER,
            "requested_date": "2026-11-20",
            "items": items,
        },
    )
    assert response.status_code == 202, response.text
    return poll(client, response.json()["request_id"])


def responses(llm: FakeLLM) -> list[dict[str, JsonValue]]:
    return [
        part.function_response.response
        for call in llm.calls
        for part in call.contents[-1].parts or []
        if part.function_response is not None
    ]


@pytest.mark.parametrize("auto_limit,status", [(1500, "ORDERED"), (1, "WAITING_APPROVAL")])
def test_http_purchase_chooses_over_decoy_and_stores_explanation(
    tmp_path: Path, auto_limit: float, status: str
) -> None:
    text = "The total is 6,70 EUR. Delivery is 2026-11-20."
    turns = purchase("900702")
    turns[1] = [
        ToolRequest("check_offers", {"item_id": "it_1", "product_ids": ["900701", "900702"]})
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[{"text": text}])
    with environment(tmp_path, llm, auto_limit=auto_limit) as client:
        detail = start(client)
        assert detail.status == status, detail.error
        quote = detail.quote
        assert quote is not None and quote.agent_id == AGENT_ID
        assert quote.lines[0].product_id == "900702" and quote.lines[0].quantity_sales_units == 2
        assert quote.agent_explanation == text and compute_content_hash(quote) == quote.content_hash
        stored = client.app.state.store.get_quote(quote.quote_id, BUYER)
        assert stored.agent_explanation == text
        if detail.approval_id:
            approval = client.app.state.store.get_approval(detail.approval_id, BUYER)
            assert approval.explanation == text and approval.agent_id == AGENT_ID
        steps = [step for step in detail.steps if step.model is not None]
        assert {step.type for step in steps} == {"TOOL_CALL", "CANDIDATES", "CHOICE"}
        assert all(step.actor == "agent" and step.model == "fake" for step in steps)
        assert len([step for step in steps if step.type == "TOOL_CALL"]) == 3
        assert verify_chain(detail.steps)
        initial = json.loads(llm.calls[0].contents[0].parts[0].text)
        assert len(initial) == 1 and initial[0]["item_id"] == "it_1"
        for call in llm.calls:
            assert "900799" not in str(call.contents)
            assert "Invented unrelated carrots" not in str(call.contents)
        assert len(detail.candidates) == 2


@pytest.mark.parametrize(
    "bad,expected",
    [
        (ToolRequest("search_products", {"item_id": "unknown", "query": "flour"}), "UNKNOWN_ITEM"),
        (
            ToolRequest("check_offers", {"item_id": "it_1", "product_ids": ["900799"]}),
            "PRODUCT_NOT_SEARCHED",
        ),
        (
            ToolRequest(
                "request_quote", {"choices": [{"item_id": "it_1", "product_id": "900799"}]}
            ),
            "PRODUCT_NOT_OFFERED",
        ),
        (
            ToolRequest(
                "request_quote", {"choices": [{"item_id": "unknown", "product_id": "900702"}]}
            ),
            "UNKNOWN_ITEM",
        ),
        (
            ToolRequest("search_products", {"item_id": "it_1", "query": "flour", "quantity": 99}),
            "Invalid arguments",
        ),
        (
            ToolRequest(
                "request_quote",
                {"choices": [{"item_id": "it_1", "product_id": "900702", "unit_price": 0.01}]},
            ),
            "Invalid arguments",
        ),
        (
            ToolRequest(
                "request_quote",
                {
                    "choices": [
                        {"item_id": "it_1", "product_id": "900702", "quantity_sales_units": 99}
                    ]
                },
            ),
            "Invalid arguments",
        ),
    ],
)
def test_invalid_calls_recover(tmp_path: Path, bad: ToolRequest, expected: str) -> None:
    turns = purchase("900702")
    turns[1] = [
        ToolRequest("check_offers", {"item_id": "it_1", "product_ids": ["900702", "900703"]})
    ]
    turns.insert(2, [bad])
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION])
    with environment(tmp_path, llm) as client:
        detail = start(client)
        assert detail.status == "ORDERED", detail.error
        assert detail.quote.lines[0].quantity_sales_units == 2
        assert expected in str(responses(llm))
        assert sum(step.type == "TOOL_CALL" and step.model == "fake" for step in detail.steps) == 4
        assert verify_chain(detail.steps)


def test_missing_item_recovers_and_quotes_all_items(tmp_path: Path) -> None:
    turns = purchase("900702")[:2] + purchase("900702", "it_2")[:2]
    turns += [
        purchase("900702")[2],
        [
            ToolRequest(
                "request_quote",
                {
                    "choices": [
                        {"item_id": "it_1", "product_id": "900702"},
                        {"item_id": "it_2", "product_id": "900702"},
                    ]
                },
            )
        ],
        "Done.",
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION])
    with environment(tmp_path, llm) as client:
        detail = start(client, two_items=True)
        assert detail.status == "ORDERED", detail.error
        assert [line.quantity_sales_units for line in detail.quote.lines] == [2, 3]
        assert "MISSING_OR_DUPLICATE_ITEMS" in str(responses(llm))
        assert sum(step.type == "CHOICE" for step in detail.steps) == 2


@pytest.mark.parametrize("mode", ["no_quote", "max_steps", "invalid_output"])
def test_failed_agent_has_clear_code(tmp_path: Path, mode: str) -> None:
    turns = (
        ["Finished without a quote."]
        if mode == "no_quote"
        else (
            [[ToolRequest("search_products", {"item_id": "unknown", "query": "flour"})]] * 8
            if mode == "max_steps"
            else [""]
        )
    )
    llm = FakeLLM(tool_responses=turns)
    with environment(tmp_path, llm) as client:
        detail = start(client)
    assert detail.status == "FAILED" and detail.error.code == "AGENT_FAILED"
    assert detail.quote is None and detail.order is None
    assert sum(step.type == "TOOL_CALL" for step in detail.steps) == (
        7 if mode == "max_steps" else 0  # 4 * one item + 3
    )
    assert verify_chain(detail.steps)


def test_item_without_products_reports_no_candidates(tmp_path: Path) -> None:
    turns = [
        [ToolRequest("search_products", {"item_id": "it_1", "query": "zzqx dragon scales"})]
    ] * 8
    llm = FakeLLM(tool_responses=turns)
    with environment(tmp_path, llm) as client:
        detail = start(client)
    assert detail.status == "FAILED" and detail.error.code == "NO_CANDIDATES"
    assert detail.quote is None and detail.order is None
    assert verify_chain(detail.steps)


def test_irrelevant_results_only_report_no_candidates(tmp_path: Path) -> None:
    # The search finds something, but the agent never finds it worth an offer check.
    turns = [[ToolRequest("search_products", {"item_id": "it_1", "query": "flour"})]] * 8
    llm = FakeLLM(tool_responses=turns)
    with environment(tmp_path, llm) as client:
        detail = start(client)
    assert detail.status == "FAILED" and detail.error.code == "NO_CANDIDATES"


def test_missing_catalogue_stops_the_agent_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(self: GatewayClient, body: SearchBody) -> object:
        raise GatewayClientError("CATALOG_UNAVAILABLE")

    monkeypatch.setattr(GatewayClient, "search_products", unavailable)
    turns = [[ToolRequest("search_products", {"item_id": "it_1", "query": "flour"})]] * 8
    llm = FakeLLM(tool_responses=turns)
    with environment(tmp_path, llm) as client:
        detail = start(client)
    assert detail.status == "FAILED" and detail.error.code == "CATALOG_UNAVAILABLE"
    assert sum(step.type == "TOOL_CALL" for step in detail.steps) == 1
    assert verify_chain(detail.steps)


@pytest.mark.parametrize(
    "text",
    [
        "The total is 999 EUR. Delivery is confirmed.",
        "Delivery is 2026-20-11. The products are selected.",
    ],
)
def test_explanation_repairs_then_falls_back(tmp_path: Path, text: str) -> None:
    llm = FakeLLM(tool_responses=purchase("900702"), structured_outputs=[{"text": text}] * 2)
    with environment(tmp_path, llm) as client:
        detail = start(client)
        assert detail.status == "ORDERED", detail.error
        assert detail.quote.agent_explanation != text
        assert "6.70 EUR" in detail.quote.agent_explanation
        assert "2026-11-20" in detail.quote.agent_explanation
        fallback = next(step for step in detail.steps if step.type == "EXCEPTION")
        assert fallback.model == "fake" and fallback.detail["fallback_used"] is True
        assert len(llm.calls) == 6 and verify_chain(detail.steps)


def test_single_quote_and_repaired_explanation(tmp_path: Path) -> None:
    turns = purchase("900702")
    turns.insert(3, turns[2])
    llm = FakeLLM(
        tool_responses=turns,
        structured_outputs=[
            {"text": "The total is 999 EUR. Please review it."},
            EXPLANATION,
        ],
    )
    with environment(tmp_path, llm) as client:
        detail = start(client)
        assert detail.status == "ORDERED", detail.error
        assert "QUOTE_ALREADY_CREATED" in str(responses(llm))
        assert sum(step.type == "QUOTE_CREATED" for step in detail.steps) == 1
        assert detail.quote.agent_explanation == EXPLANATION["text"]
        assert not any(step.type == "EXCEPTION" for step in detail.steps)


@pytest.mark.parametrize(
    "body,code",
    [
        (
            {"error": {"code": "AGENT_NOT_PERMITTED", "message": "private remote message"}},
            "AGENT_NOT_PERMITTED",
        ),
        ({"private": "raw body"}, "GATEWAY_ERROR"),
    ],
)
def test_http_errors_include_code_only(body: object, code: str) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json=body)),
        base_url="http://testserver",
    ) as http:
        client = GatewayClient(Settings(), http)
        with pytest.raises(GatewayClientError) as caught:
            client.search_products(
                SearchBody(
                    buyer_id=BUYER,
                    agent_id="ignored",
                    query="flour",
                    constraints=[],
                    exclude_allergens=[],
                    limit=5,
                )
            )
    assert caught.value.code == code
    assert "private" not in str(caught.value) and "raw body" not in str(caught.value)
