"""HTTP forwarding and MCP error translation, with no purchasing logic."""

import os

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import JsonValue, TypeAdapter, ValidationError

from gateway.schemas import AgentIdentity, ErrorEnvelope

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class GatewayClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.url = os.getenv("LIMOS_GATEWAY_URL", "http://localhost:8000").rstrip("/")
        self.agent_id = os.getenv("LIMOS_AGENT_ID", "limos-mcp")
        self.buyer_id = os.getenv("LIMOS_BUYER_ID") or None
        self.timeout = float(os.getenv("LIMOS_REQUEST_TIMEOUT_SECONDS", "10"))
        # Injected clients belong to their caller; production clients close after each request.
        self._client = client

    async def request(self, method: str, path: str, body: AgentIdentity) -> dict[str, JsonValue]:
        if self.buyer_id is not None and body.buyer_id != self.buyer_id:
            raise ToolError(f"This server acts for buyer {self.buyer_id} only")
        payload = body.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        try:
            if self._client is not None:
                response = await self._send(self._client, method, path, payload)
            else:
                async with httpx.AsyncClient() as client:
                    response = await self._send(client, method, path, payload)
        except httpx.RequestError as exc:
            raise ToolError("GATEWAY_UNAVAILABLE: Could not reach the gateway.") from exc

        if not response.is_success:
            try:
                error = ErrorEnvelope.model_validate_json(response.content).error
            except ValidationError as exc:
                raise ToolError(
                    f"HTTP {response.status_code} GATEWAY_ERROR: Invalid gateway error response."
                ) from exc
            raise ToolError(f"HTTP {response.status_code} {error.code}: {error.message}")
        try:
            return _JSON_OBJECT.validate_json(response.content)
        except ValidationError as exc:
            raise ToolError(
                "GATEWAY_INVALID_RESPONSE: Expected a JSON object from the gateway."
            ) from exc

    async def _send(
        self, client: httpx.AsyncClient, method: str, path: str, payload: dict[str, JsonValue]
    ) -> httpx.Response:
        url = f"{self.url}{path}"
        if method == "GET":
            return await client.get(
                url,
                params={"buyer_id": str(payload["buyer_id"]), "agent_id": self.agent_id},
                timeout=self.timeout,
            )
        return await client.post(url, json=payload, timeout=self.timeout)
