"""MCP surface and HTTP forwarding with invented inputs and no network."""

import json
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp_types import CallToolResult

from mcp_server import server as server_module
from mcp_server.server import create_server

SEARCH = {
    "query": "Invented pantry flour",
    "constraints": ["organic"],
    "exclude_allergens": ["milk"],
    "limit": 5,
}


@pytest.fixture(autouse=True)
def mcp_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LIMOS_GATEWAY_URL", "LIMOS_AGENT_ID", "LIMOS_REQUEST_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


async def test_tool_list() -> None:
    tools = await create_server().list_tools()
    assert len(tools) == 6
    assert {tool.name for tool in tools} == {
        "search_products",
        "check_offers",
        "request_quote",
        "request_approval",
        "place_order",
        "get_order_status",
    }
    for tool in tools:
        assert "buyer_id" in tool.input_schema["required"]
        assert "agent_id" not in tool.input_schema["properties"]
        assert tool.output_schema is not None


@pytest.mark.parametrize(
    ("name", "arguments", "method", "path"),
    [
        ("search_products", SEARCH, "POST", "/gateway/search"),
        (
            "check_offers",
            {
                "product_ids": ["invented-flour"],
                "item_id": "invented-item",
                "quantity": 2.0,
                "unit": "kg",
                "requested_date": "2026-11-20",
                "constraints": [],
                "exclude_allergens": [],
            },
            "POST",
            "/gateway/offers",
        ),
        (
            "request_quote",
            {
                "requested_date": "2026-11-20",
                "lines": [
                    {
                        "item_id": "invented-item",
                        "product_id": "invented-flour",
                        "quantity_sales_units": 2.0,
                    }
                ],
                "request_id": "invented-request",
                "budget": 25.0,
                "option_label": "Pantry",
            },
            "POST",
            "/gateway/quotes",
        ),
        (
            "request_approval",
            {
                "quote_id": "invented-quote",
                "explanation": "The quote needs human review.",
                "alternative_quote_ids": ["invented-alternative"],
            },
            "POST",
            "/gateway/quotes/invented-quote/request-approval",
        ),
        (
            "place_order",
            {"quote_id": "invented-quote", "idempotency_key": "invented-key"},
            "POST",
            "/gateway/orders",
        ),
        (
            "get_order_status",
            {"order_id": "invented-order"},
            "GET",
            "/gateway/orders/invented-order",
        ),
    ],
)
async def test_http_mapping(name: str, arguments: dict[str, Any], method: str, path: str) -> None:
    calls: list[httpx.Request] = []
    response = {"invented_result": {"values": [1, None, True], "status": "ok"}}

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await create_server(client=client).call_tool(
            name, {"buyer_id": "invented-buyer", **arguments}
        )
        assert not client.is_closed

    assert isinstance(result, CallToolResult)
    assert not result.is_error
    assert result.structured_content == response
    assert len(calls) == 1
    request = calls[0]
    assert request.method == method
    assert str(request.url).split("?")[0] == f"http://localhost:8000{path}"
    identity = {"buyer_id": "invented-buyer", "agent_id": "limos-mcp"}
    if method == "GET":
        assert dict(request.url.params) == identity
        assert request.content == b""
    else:
        body = dict(arguments)
        if name == "request_approval":
            del body["quote_id"]
        assert json.loads(request.content) == {**identity, **body}


@pytest.mark.parametrize("code", ["NEEDS_APPROVAL", "AGENT_NOT_PERMITTED"])
async def test_gateway_error(code: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": code, "message": "Human review."}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        server = create_server(client=client)
        with pytest.raises(ToolError, match=f"HTTP 403 {code}: Human review") as caught:
            await server.call_tool(
                "place_order",
                {
                    "buyer_id": "invented-buyer",
                    "quote_id": "invented-quote",
                    "idempotency_key": "invented-key",
                },
            )
        assert not isinstance(caught.value, UnexpectedToolError)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_failure(failure: type[httpx.RequestError]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise failure("Invented transport failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ToolError, match="GATEWAY_UNAVAILABLE") as caught:
            await create_server(client=client).call_tool(
                "search_products", {"buyer_id": "invented-buyer", **SEARCH}
            )
        assert not isinstance(caught.value, UnexpectedToolError)


async def test_environment_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIMOS_GATEWAY_URL", "http://invented.invalid/prefix/")
    monkeypatch.setenv("LIMOS_AGENT_ID", "invented-agent")
    monkeypatch.setenv("LIMOS_REQUEST_TIMEOUT_SECONDS", "2.5")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://invented.invalid/prefix/gateway/search"
        assert json.loads(request.content)["agent_id"] == "invented-agent"
        assert request.extensions["timeout"] == dict.fromkeys(
            ["connect", "read", "write", "pool"], 2.5
        )
        return httpx.Response(200, json={"products": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await create_server(client=client).call_tool(
            "search_products", {"buyer_id": "invented-buyer", **SEARCH}
        )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], {"transport": "stdio"}),
        (["--http"], {"transport": "streamable-http", "host": "127.0.0.1", "port": 8765}),
        (
            ["--http", "--host", "localhost", "--port", "8888"],
            {"transport": "streamable-http", "host": "localhost", "port": 8888},
        ),
    ],
)
def test_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected: dict[str, Any],
) -> None:
    calls = []

    class FakeServer:
        def run(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(server_module, "create_server", FakeServer)
    monkeypatch.setattr("sys.argv", ["limos-mcp", *arguments])
    server_module.main()
    assert calls == [expected]
    assert capsys.readouterr().out == ""
