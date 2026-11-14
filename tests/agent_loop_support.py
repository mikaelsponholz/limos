"""Explicit offline HTTP wiring and scripted purchases shared by Phase 3 tests."""

from fastapi.testclient import TestClient

from agent.gateway_client import AGENT_ID
from agent.llm import FakeLLM, ToolRequest

EXPLANATION = {
    "text": "The selected products cover the requested items. The quote is ready for review."
}


def purchase(product_id: str = "900701", item_id: str = "it_1") -> list[str | list[ToolRequest]]:
    return [
        [ToolRequest("search_products", {"item_id": item_id, "query": "flour"})],
        [ToolRequest("check_offers", {"item_id": item_id, "product_ids": [product_id]})],
        [
            ToolRequest(
                "request_quote", {"choices": [{"item_id": item_id, "product_id": product_id}]}
            )
        ],
        "The quote is ready.",
    ]


def bind_client(client: TestClient, llm: FakeLLM | None) -> TestClient:
    client.app.state.gateway_http_client = client
    if llm is not None:
        client.app.state.mandates = {
            buyer: entry.model_copy(
                update={"allowed_agents": tuple(dict.fromkeys((*entry.allowed_agents, AGENT_ID)))}
            )
            for buyer, entry in client.app.state.mandates.items()
        }
    return client
