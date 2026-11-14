"""Real MCP sessions exercise gateway guardrails over in-process transports only."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import count
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp_types import TextContent

from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.settings import Settings
from gateway.store import MemoryStore
from mcp_server.server import create_server
from tests.engine_support import BUYER, mandate
from tests.test_offers import NOW, REQUESTED, FakeValio, price, product, stock

TOOLS = {
    "search_products",
    "check_offers",
    "request_quote",
    "request_approval",
    "place_order",
    "get_order_status",
}
SEARCH = {"query": "flour", "constraints": [], "exclude_allergens": [], "limit": 5}
OFFER = {
    "product_ids": ["900101"],
    "item_id": "it_flour",
    "quantity": 2.0,
    "unit": "kg",
    "requested_date": REQUESTED.isoformat(),
    "constraints": [],
    "exclude_allergens": [],
}
QUOTE = {
    "requested_date": REQUESTED.isoformat(),
    "lines": [{"item_id": "it_flour", "product_id": "900101", "quantity_sales_units": 2.0}],
}


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("LIMOS_GATEWAY_URL", "http://invented.invalid")
    monkeypatch.setenv("LIMOS_AGENT_ID", "limos-mcp")
    monkeypatch.setenv("LIMOS_REQUEST_TIMEOUT_SECONDS", "2")
    ids = count(1)
    return create_app(
        Settings(store="memory", valio_api_key="", catalog_path=str(tmp_path / "missing.json")),
        catalog=Catalog([product()]),
        valio=FakeValio([price()], [stock()]),
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={BUYER: mandate(auto_limit=100.0, allowed_agents=("limos-mcp",))},
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )


@asynccontextmanager
async def mcp_session(client: httpx.AsyncClient) -> AsyncIterator[ClientSession]:
    server = create_server(client=client)
    # Use the same low-level runner as the SDK's stdio transport, with paired streams.
    lowlevel = server._lowlevel_server
    with anyio.fail_after(10):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    lowlevel.run, *server_streams, lowlevel.create_initialization_options()
                )
                try:
                    async with ClientSession(*client_streams, read_timeout_seconds=2) as session:
                        initialized = await session.initialize()
                        assert initialized.server_info.name == "limos-mcp"
                        yield session
                finally:
                    tasks.cancel_scope.cancel()


async def call(session: ClientSession, name: str, **arguments: Any) -> dict[str, Any]:
    result = await session.call_tool(name, {"buyer_id": BUYER, **arguments})
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def assert_tool_error(session: ClientSession, name: str, code: str, **arguments: Any) -> None:
    result = await session.call_tool(name, {"buyer_id": BUYER, **arguments})
    assert result.is_error, result
    assert any(
        isinstance(content, TextContent) and f"HTTP 403 {code}:" in content.text
        for content in result.content
    ), result.content


async def test_session_lists_only_gateway_tools(gateway: FastAPI) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway)) as http,
        mcp_session(http) as session,
    ):
        listing = await session.list_tools()
        assert len(listing.tools) == 6
        assert {tool.name for tool in listing.tools} == TOOLS
        assert listing.next_cursor is None


async def test_session_auto_order_and_idempotent_retry(gateway: FastAPI) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway)) as http,
        mcp_session(http) as session,
    ):
        found = await call(session, "search_products", **SEARCH)
        assert [entry["product_id"] for entry in found["products"]] == ["900101"]
        assert gateway.state.valio.calls == []
        checked = await call(
            session,
            "check_offers",
            **{**OFFER, "product_ids": [found["products"][0]["product_id"]]},
        )
        assert len(checked["offers"]) == 1
        offer = checked["offers"][0]
        quote = await call(
            session,
            "request_quote",
            requested_date=REQUESTED.isoformat(),
            budget=50.0,
            lines=[
                {
                    "item_id": offer["item_id"],
                    "product_id": offer["product_id"],
                    "quantity_sales_units": offer["quantity_sales_units"],
                }
            ],
        )
        assert quote["status"] == "AUTO_APPROVED"
        assert quote["agent_id"] == "limos-mcp"
        assert quote["policy"]["outcome"] == "AUTO"
        assert quote["policy"]["rules_fired"] == []
        assert quote["total"] == 6.7
        limits = quote["policy"]["mandate"]
        assert quote["total"] < min(limits["auto_limit"], limits["hard_cap"], 50.0)
        assert all(line["line_total"] < limits["line_limit"] for line in quote["lines"])
        arguments = {"quote_id": quote["quote_id"], "idempotency_key": "invented-auto-order"}
        order = await call(session, "place_order", **arguments)
        assert order["status"] == "CONFIRMED"
        assert order["simulated"] is True
        assert order["placed_by"] == {"type": "agent", "name": "limos-mcp"}
        assert order["approved_by"] is None
        assert order["quote_id"] == quote["quote_id"]
        assert order["lines"] == quote["lines"]
        assert order["total"] == quote["total"]
        assert await call(session, "get_order_status", order_id=order["order_id"]) == order
        assert await call(session, "place_order", **arguments) == order


async def test_session_needs_human_rest_approval(gateway: FastAPI) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway)) as http,
        mcp_session(http) as session,
    ):
        quote = await call(
            session,
            "request_quote",
            requested_date=REQUESTED.isoformat(),
            lines=[{"item_id": "it_flour", "product_id": "900101", "quantity_sales_units": 40.0}],
        )
        assert quote["status"] == "PENDING_APPROVAL"
        assert quote["policy"]["outcome"] == "NEEDS_APPROVAL"
        assert quote["policy"]["mandate"]["auto_limit"] < quote["total"] == 134.0
        assert [rule["rule_id"] for rule in quote["policy"]["rules_fired"]] == ["SPEND_SOFT_LIMIT"]
        arguments = {
            "quote_id": quote["quote_id"],
            "idempotency_key": f"{quote['request_id']}-{quote['quote_id']}",
        }
        await assert_tool_error(session, "place_order", "NEEDS_APPROVAL", **arguments)
        approval = await call(
            session,
            "request_approval",
            quote_id=quote["quote_id"],
            explanation="Invented pantry order exceeds the automatic spend limit.",
        )
        assert approval["status"] == "PENDING"
        # Asking for approval does not grant it, even with the eventual order's key.
        await assert_tool_error(session, "place_order", "NEEDS_APPROVAL", **arguments)
        assert gateway.state.store.order_for_request(quote["request_id"], BUYER) is None
        assert {tool.name for tool in (await session.list_tools()).tools} == TOOLS
        response = await http.post(
            f"http://invented.invalid/approvals/{approval['approval_id']}/approve",
            json={
                "approver": "Invented Human",
                "quote_id": quote["quote_id"],
                "content_hash": quote["content_hash"],
                "comment": "Approved invented pantry stock.",
            },
        )
        assert response.status_code == 200, response.text
        approved = response.json()
        assert approved["status"] == "APPROVED"
        order = approved["order"]
        assert order["status"] == "CONFIRMED"
        assert order["simulated"] is True
        assert order["approved_by"] == "Invented Human"
        assert order["placed_by"] == {"type": "agent", "name": "limos-mcp"}
        assert order["quote_id"] == quote["quote_id"]
        assert order["idempotency_key"] == arguments["idempotency_key"]
        assert await call(session, "get_order_status", order_id=order["order_id"]) == order
        assert await call(session, "place_order", **arguments) == order


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("search_products", SEARCH),
        ("check_offers", OFFER),
        ("request_quote", QUOTE),
        ("request_approval", {"quote_id": "invented-quote", "explanation": "Human review."}),
        ("place_order", {"quote_id": "invented-quote", "idempotency_key": "invented-key"}),
        ("get_order_status", {"order_id": "invented-order"}),
    ],
)
async def test_session_rejects_disallowed_agent_on_every_tool(
    gateway: FastAPI, monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict[str, Any]
) -> None:
    monkeypatch.setenv("LIMOS_AGENT_ID", "invented-disallowed-agent")
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway)) as http,
        mcp_session(http) as session,
    ):
        await assert_tool_error(session, name, "AGENT_NOT_PERMITTED", **arguments)
        assert gateway.state.valio.calls == []
