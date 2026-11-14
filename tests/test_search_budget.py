"""Offline regressions for bounded searches and room to finish with a quote."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.expansion import build_event_items, expand_event
from agent.gateway_client import GatewayClient
from agent.llm import FakeLLM, ToolRequest
from gateway.audit import verify_chain
from gateway.schemas import (
    EventDish,
    OffersBody,
    OffersResponse,
    Preference,
    RequestDetail,
    SearchBody,
    SearchResponse,
)
from tests.agent_loop_support import EXPLANATION, purchase
from tests.engine_support import BUYER
from tests.test_agent_loop import environment, responses
from tests.test_e2e import poll
from tests.test_expansion import event, ingredient, proposal


@pytest.fixture
def gateway_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[list[SearchBody], list[OffersBody]]:
    searches: list[SearchBody] = []
    offers: list[OffersBody] = []
    search = GatewayClient.search_products
    check = GatewayClient.check_offers

    def search_products(self: GatewayClient, body: SearchBody) -> SearchResponse:
        searches.append(body)
        return search(self, body)

    def check_offers(self: GatewayClient, body: OffersBody) -> OffersResponse:
        offers.append(body)
        return check(self, body)

    monkeypatch.setattr(GatewayClient, "search_products", search_products)
    monkeypatch.setattr(GatewayClient, "check_offers", check_offers)
    return searches, offers


def start_items(client: TestClient, count: int) -> RequestDetail:
    response = client.post(
        "/requests",
        json={
            "buyer_id": BUYER,
            "requested_date": "2026-11-20",
            "items": [
                {"description": "flour", "quantity": 2.0, "unit": "kg", "constraints": []}
                for _ in range(count)
            ],
        },
    )
    assert response.status_code == 202, response.text
    return poll(client, response.json()["request_id"])


def quote_call(count: int) -> ToolRequest:
    return ToolRequest(
        "request_quote",
        {
            "choices": [
                {"item_id": f"it_{index}", "product_id": "900702"} for index in range(1, count + 1)
            ]
        },
    )


@pytest.mark.parametrize("auto_limit,status", [(1500, "ORDERED"), (1, "WAITING_APPROVAL")])
def test_six_item_retry_keeps_an_earlier_usable_offer(
    tmp_path: Path,
    gateway_calls: tuple[list[SearchBody], list[OffersBody]],
    auto_limit: float,
    status: str,
) -> None:
    turns = [turn for index in range(1, 7) for turn in purchase("900702", f"it_{index}")[:2]]
    retry = ToolRequest("search_products", {"item_id": "it_6", "query": "organic strawberry jam"})
    turns += [[retry], [retry], [quote_call(6)], "The quote is ready."]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION])
    with environment(tmp_path, llm, auto_limit=auto_limit) as client:
        detail = start_items(client, 6)
    assert detail.status == status, detail.error
    assert detail.quote is not None and len(detail.quote.lines) == 6
    outputs = responses(llm)
    assert outputs[-3]["output"]["products"] == []
    assert outputs[-2]["output"]["error"] == "SEARCH_LIMIT"
    assert "already returned" in outputs[-2]["output"]["message"]
    assert len(gateway_calls[0]) == 7 and len(gateway_calls[1]) == 6
    assert len(detail.candidates) == 6
    assert verify_chain(detail.steps)


def test_item_with_only_empty_searches_preserves_missing_candidate_failure(
    tmp_path: Path,
    gateway_calls: tuple[list[SearchBody], list[OffersBody]],
) -> None:
    turns = [turn for index in range(1, 6) for turn in purchase("900702", f"it_{index}")[:2]]
    retry = ToolRequest("search_products", {"item_id": "it_6", "query": "organic strawberry jam"})
    turns += [[retry], [retry], [retry], [quote_call(6)], "No candidate for the last item."]
    llm = FakeLLM(tool_responses=turns)
    with environment(tmp_path, llm) as client:
        detail = start_items(client, 6)
        assert client.app.state.store.list_quotes(detail.request_id, BUYER) == []
    assert detail.status == "FAILED" and detail.error.code == "AGENT_FAILED"
    assert detail.quote is None and detail.order is None
    assert {offer.item_id for offer in detail.candidates} == {
        f"it_{index}" for index in range(1, 6)
    }
    outputs = responses(llm)
    assert outputs[-4]["output"]["products"] == []
    assert outputs[-3]["output"]["products"] == []
    assert outputs[-2]["output"]["error"] == "SEARCH_LIMIT"
    assert outputs[-1] == {"output": {"error": "PRODUCT_NOT_OFFERED"}}
    assert len(gateway_calls[0]) == 7 and len(gateway_calls[1]) == 5
    assert verify_chain(detail.steps)


def test_third_search_at_quote_margin_keeps_search_limit_and_requests_quote(
    tmp_path: Path,
    gateway_calls: tuple[list[SearchBody], list[OffersBody]],
) -> None:
    search, check = purchase("900702")[:2]
    llm = FakeLLM(
        tool_responses=[search, search, check, search, [quote_call(1)], "The quote is ready."],
        structured_outputs=[EXPLANATION],
    )
    with environment(tmp_path, llm) as client:
        detail = start_items(client, 1)
    assert detail.status == "ORDERED", detail.error
    assert detail.quote is not None
    limited = responses(llm)[-2]["output"]
    assert limited["error"] == "SEARCH_LIMIT"
    assert "request_quote now" in limited["message"]
    assert len(gateway_calls[0]) == 2 and len(gateway_calls[1]) == 1


@pytest.mark.parametrize("count", [1, 6])
@pytest.mark.parametrize("tool", ["search_products", "check_offers"])
@pytest.mark.parametrize("batched", [False, True])
def test_quote_now_leaves_a_call_after_its_error_reply(
    tmp_path: Path,
    gateway_calls: tuple[list[SearchBody], list[OffersBody]],
    count: int,
    tool: str,
    batched: bool,
) -> None:
    calls = [
        call
        for index in range(1, count + 1)
        for turn in purchase("900702", f"it_{index}")[:2]
        for call in turn
    ]
    # Invalid arguments still consume calls, without entering a tool function.
    calls += [ToolRequest("search_products", {"item_id": "it_1"})] * count
    calls.append(
        ToolRequest(
            tool,
            {"item_id": "it_1", "query": "flour"}
            if tool == "search_products"
            else {"item_id": "it_1", "product_ids": ["900702"]},
        )
    )
    calls.append(quote_call(count))
    turns: list[str | list[ToolRequest]] = [calls] if batched else [[call] for call in calls]
    turns.append("The quote is ready.")
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION])
    with environment(tmp_path, llm) as client:
        detail = start_items(client, count)
    assert detail.status == "ORDERED", detail.error
    assert detail.quote is not None and len(detail.quote.lines) == count
    outputs = responses(llm)
    assert outputs[-2]["output"]["error"] == "QUOTE_NOW"
    assert "request_quote" in outputs[-2]["output"]["message"]
    assert "quote_id" in outputs[-1]["output"]
    assert len(gateway_calls[0]) == count and len(gateway_calls[1]) == count
    assert sum(step.type == "TOOL_CALL" and step.model == "fake" for step in detail.steps) == (
        3 * count + 2
    )
    assert verify_chain(detail.steps)


@pytest.mark.parametrize("tool", ["search_products", "check_offers"])
def test_missing_offers_keep_tools_open_at_the_budget_margin(
    tmp_path: Path,
    gateway_calls: tuple[list[SearchBody], list[OffersBody]],
    tool: str,
) -> None:
    search, check = purchase("900702")[:2]
    # With one item the budget is five: call four is normally the quote-now guard.
    margin_call = search if tool == "search_products" else check
    turns = [search, [ToolRequest("unknown_tool")], [ToolRequest("unknown_tool")], margin_call]
    if tool == "search_products":
        turns.append(check)
    turns.append("Finished without a quote.")
    llm = FakeLLM(tool_responses=turns)
    with environment(tmp_path, llm) as client:
        detail = start_items(client, 1)
    assert detail.status == "FAILED" and detail.error.code == "AGENT_FAILED"
    assert "QUOTE_NOW" not in str(responses(llm))
    assert len(gateway_calls[0]) == (2 if tool == "search_products" else 1)
    assert len(gateway_calls[1]) == 1
    assert len(detail.candidates) == 1


@pytest.mark.parametrize("organic_level", ["prefer", "require"])
def test_expansion_only_sends_required_preferences(organic_level: str) -> None:
    preferences = [
        Preference(key="organic", level=organic_level),
        Preference(key="finnish_origin", level="prefer"),
        Preference(key="low_carbon", level="prefer"),
        Preference(key="lactose_free", level="require"),
    ]
    dishes = [EventDish(name="Invented cake", servings=None)]
    llm = FakeLLM(
        structured_outputs=[proposal(("Invented cake", [ingredient(10, description="jam")]))]
    )
    result = expand_event(llm, event(), dishes, preferences, [])
    call = llm.calls[0]
    payload = json.loads(call.contents[0].parts[0].text.split("\nReturn JSON")[0])
    assert payload["preferences"] == [p.key for p in preferences if p.level == "require"]
    assert "finnish_origin" not in payload["preferences"]
    assert "low_carbon" not in payload["preferences"]
    assert "lactose_free" in payload["preferences"]
    assert "strawberry jam, not organic strawberry jam" in call.config.system_instruction
    assert "Finnish or low-carbon" in call.config.system_instruction
    expanded = build_event_items(result.value, event(), dishes, preferences)
    assert expanded.items[0].constraints == ["lactose_free"]
