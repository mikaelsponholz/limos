"""Expose the six gateway tools through stdio or Streamable HTTP."""

import argparse
import logging
from datetime import date
from typing import Annotated
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import Field, JsonValue

from gateway.schemas import (
    AgentIdentity,
    AllergenCode,
    Constraint,
    OffersBody,
    OrderBody,
    QuoteBody,
    QuoteLine,
    RequestApprovalBody,
    SearchBody,
    Unit,
)
from mcp_server.gateway_client import GatewayClient


def create_server(*, client: httpx.AsyncClient | None = None) -> MCPServer[None]:
    """Build a server; tests can supply a client with a mock or ASGI transport."""
    gateway = GatewayClient(client)
    instructions = None
    buyer_field = Field()
    if gateway.buyer_id is not None:
        instructions = (
            f"This server acts for buyer {gateway.buyer_id} only. "
            "Omit buyer_id in tool calls to use this configured buyer. "
            "Do not ask the user for a buyer id. The gateway still enforces the buyer's mandate."
        )
        buyer_field = Field(default=gateway.buyer_id, description=instructions)
    server: MCPServer[None] = MCPServer("limos-mcp", instructions=instructions)

    @server.tool(structured_output=True)
    async def search_products(
        buyer_id: Annotated[str, buyer_field],
        query: str,
        constraints: list[Constraint],
        exclude_allergens: list[AllergenCode],
        limit: int,
    ) -> dict[str, JsonValue]:
        """Search the gateway catalogue with dietary and allergen filters."""
        return await gateway.request(
            "POST",
            "/gateway/search",
            SearchBody(
                buyer_id=buyer_id,
                agent_id=gateway.agent_id,
                query=query,
                constraints=constraints,
                exclude_allergens=exclude_allergens,
                limit=limit,
            ),
        )

    @server.tool(structured_output=True)
    async def check_offers(
        buyer_id: Annotated[str, buyer_field],
        product_ids: list[str],
        item_id: str,
        quantity: float,
        unit: Unit,
        requested_date: date,
        constraints: list[Constraint],
        exclude_allergens: list[AllergenCode],
    ) -> dict[str, JsonValue]:
        """Check prices and availability for a shortlist of products."""
        return await gateway.request(
            "POST",
            "/gateway/offers",
            OffersBody(
                buyer_id=buyer_id,
                agent_id=gateway.agent_id,
                product_ids=product_ids,
                item_id=item_id,
                quantity=quantity,
                unit=unit,
                requested_date=requested_date,
                constraints=constraints,
                exclude_allergens=exclude_allergens,
            ),
        )

    @server.tool(structured_output=True)
    async def request_quote(
        buyer_id: Annotated[str, buyer_field],
        requested_date: date,
        lines: list[QuoteLine],
        request_id: str | None = None,
        budget: float | None = None,
        option_label: str | None = None,
        exclude_allergens: list[AllergenCode] | None = None,
        constraints: list[Constraint] | None = None,
    ) -> dict[str, JsonValue]:
        """Create a price-locked quote with the gateway's policy decision."""
        body = QuoteBody(
            buyer_id=buyer_id,
            agent_id=gateway.agent_id,
            requested_date=requested_date,
            lines=lines,
            request_id=request_id,
            budget=budget,
            option_label=option_label,
        )
        if exclude_allergens is not None:
            body.exclude_allergens = exclude_allergens
        if constraints is not None:
            body.constraints = constraints
        return await gateway.request(
            "POST",
            "/gateway/quotes",
            body,
        )

    @server.tool(structured_output=True)
    async def request_approval(
        buyer_id: Annotated[str, buyer_field],
        quote_id: str,
        explanation: str,
        alternative_quote_ids: list[str] | None = None,
    ) -> dict[str, JsonValue]:
        """Ask a human to review a quote; this tool does not approve it."""
        return await gateway.request(
            "POST",
            f"/gateway/quotes/{quote(quote_id, safe='')}/request-approval",
            RequestApprovalBody(
                buyer_id=buyer_id,
                agent_id=gateway.agent_id,
                explanation=explanation,
                alternative_quote_ids=alternative_quote_ids,
            ),
        )

    @server.tool(structured_output=True)
    async def place_order(
        buyer_id: Annotated[str, buyer_field], quote_id: str, idempotency_key: str
    ) -> dict[str, JsonValue]:
        """Place an order; quotes needing approval fail without a matching human approval."""
        return await gateway.request(
            "POST",
            "/gateway/orders",
            OrderBody(
                buyer_id=buyer_id,
                agent_id=gateway.agent_id,
                quote_id=quote_id,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(structured_output=True)
    async def get_order_status(
        buyer_id: Annotated[str, buyer_field], order_id: str
    ) -> dict[str, JsonValue]:
        """Get an order's current status from the gateway."""
        return await gateway.request(
            "GET",
            f"/gateway/orders/{quote(order_id, safe='')}",
            AgentIdentity(buyer_id=buyer_id, agent_id=gateway.agent_id),
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Limos MCP gateway tools")
    parser.add_argument("--http", action="store_true", help="Serve Streamable HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)  # The default stream is stderr.
    server = create_server()
    if args.http:
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
