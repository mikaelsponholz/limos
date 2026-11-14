# Limos over MCP

Limos exposes its gateway tools over the Model Context Protocol, so an outside agent such as Claude Desktop can search, quote and order through the same rules as the Limos agent. The MCP server is a thin client of the gateway's REST API. Mandates, price locks, approvals and the decision log run in the gateway, not in the agent.

There is no approve tool. An agent can ask for approval, but only a person can give it, through the approver routes.

## Tools

| Tool | What it does |
|---|---|
| `search_products` | Searches the local catalogue. Excluded allergens and violated constraints are filtered out before results return. |
| `check_offers` | Gets Valio price and availability for a short list of products and converts the need into sales units. |
| `request_quote` | Locks prices for the chosen lines and runs the policy. Returns the outcome: `AUTO`, `NEEDS_APPROVAL` or `BLOCK`. |
| `request_approval` | Asks a person to approve a `NEEDS_APPROVAL` quote. It does not approve anything. |
| `place_order` | Places a simulated order for an `AUTO` quote, or for an approved quote whose content hash still matches. The same idempotency key returns the same order. |
| `get_order_status` | Returns an order. |

Every tool takes a `buyer_id`. The server adds its own agent id (`limos-mcp` by default), and the gateway checks it against that buyer's mandate.

The gateway always enforces the buyer's allergen and dietary rules: standalone quotes use the union of that buyer's customer profiles, while quotes with a `request_id` keep the request's per-item rules. The optional `request_quote` fields `exclude_allergens` and `constraints` apply to every line and can only add restrictions. Omitting them or sending empty lists cannot remove existing rules.

## Run it

1. Start the gateway:

   ```bash
   uv run uvicorn gateway.main:app --port 8000
   ```

2. Start the MCP server over stdio (what Claude Desktop uses):

   ```bash
   uv run limos-mcp
   ```

   Or over Streamable HTTP:

   ```bash
   uv run limos-mcp --http --host 127.0.0.1 --port 8765
   ```

Settings, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `LIMOS_GATEWAY_URL` | `http://localhost:8000` | Where the gateway runs |
| `LIMOS_AGENT_ID` | `limos-mcp` | The agent id sent with every call. It must be in the buyer's mandate. |
| `LIMOS_REQUEST_TIMEOUT_SECONDS` | `10` | Timeout for each gateway call |

The MCP server holds no keys. The gateway has the Valio and Gemini keys.

## Claude Desktop

Add this to `claude_desktop_config.json` and restart Claude Desktop. Replace the path with your checkout.

```json
{
  "mcpServers": {
    "limos": {
      "command": "uv",
      "args": ["--directory", "C:/path/to/limos", "run", "limos-mcp"],
      "env": {"LIMOS_GATEWAY_URL": "http://localhost:8000"}
    }
  }
}
```

## Walkthrough

Ask Claude something like: "Using Limos as buyer `demo-catering`, order 5 kg of butter and 40 l of lactose-free milk for delivery on 17 November."

1. `search_products` finds candidates for each item.
2. `check_offers` returns prices, stock, pack sizes, delivery date and order deadline for the short list.
3. `request_quote` locks the chosen lines. The quote carries `expires_at`, a `content_hash` and the policy decision with the rules that fired.
4. If the outcome is `AUTO`, `place_order` places the order. It is simulated, because the Valio mock API has no order endpoint.
5. If the outcome is `NEEDS_APPROVAL`, `place_order` returns `NEEDS_APPROVAL`. The agent calls `request_approval`, a person approves the quote with `POST /approvals/{approval_id}/approve`, and `get_order_status` then shows the order using the order id returned by the approval route.
6. If the outcome is `BLOCK`, nothing can be ordered. The quote lists the rule and the reason.

Quote creation, policy decisions, approval requests, human decisions and order placement are in the decision log: `GET /audit/{request_id}` returns the chain and whether it verifies.

## Without MCP

`clients/plain_client.py` places the same kind of order through plain REST calls, to show that any HTTP client can use the gateway:

```bash
uv run python clients/plain_client.py --base-url http://localhost:8000
```
