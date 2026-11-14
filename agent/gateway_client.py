"""Synchronous, typed HTTP access to the gateway's purchasing tools."""

import re

import httpx
from pydantic import BaseModel, ValidationError

from gateway.schemas import (
    ErrorEnvelope,
    OffersBody,
    OffersResponse,
    Quote,
    QuoteBody,
    SearchBody,
    SearchResponse,
)
from gateway.settings import Settings

AGENT_ID = "limos-gemini"


class GatewayClientError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: Gateway tool request failed.")


class GatewayClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client

    def _post[T: BaseModel](self, path: str, body: BaseModel, schema: type[T]) -> T:
        payload = body.model_dump(mode="json")
        payload["agent_id"] = AGENT_ID
        try:
            if self.client is None:
                with httpx.Client(
                    base_url=self.settings.limos_gateway_url.rstrip("/"),
                    timeout=self.settings.limos_request_timeout_seconds,
                ) as client:
                    response = client.post(path, json=payload)
            else:
                response = self.client.post(
                    path, json=payload, timeout=self.settings.limos_request_timeout_seconds
                )
        except httpx.RequestError:
            raise GatewayClientError("GATEWAY_UNAVAILABLE") from None
        if not response.is_success:
            try:
                code = ErrorEnvelope.model_validate_json(response.content).error.code
            except ValidationError:
                code = "GATEWAY_ERROR"
            # Never propagate remote bodies or free-form messages to the model.
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None:
                code = "GATEWAY_ERROR"
            raise GatewayClientError(code)
        try:
            return schema.model_validate_json(response.content)
        except ValidationError:
            raise GatewayClientError("GATEWAY_INVALID_RESPONSE") from None

    def search_products(self, body: SearchBody) -> SearchResponse:
        return self._post("/gateway/search", body, SearchResponse)

    def check_offers(self, body: OffersBody) -> OffersResponse:
        return self._post("/gateway/offers", body, OffersResponse)

    def request_quote(self, body: QuoteBody) -> Quote:
        return self._post("/gateway/quotes", body, Quote)
