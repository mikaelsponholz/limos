"""Batched, cached Valio requests with bounded retries and sanitized errors."""

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from email.utils import parsedate_to_datetime
from time import time
from typing import Literal, Self

import httpx
from pydantic import BaseModel, ValidationError

from gateway.settings import Settings
from gateway.valio.cache import CacheKey, TTLCache
from gateway.valio.models import (
    AvailabilityResponse,
    PingResponse,
    PriceResponse,
    ProductsRequest,
)

BATCH_SIZE = 1000


class ValioError(Exception):
    """Base error with no response body, request headers or credentials."""


class ValioHTTPError(ValioError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Valio API returned HTTP {status_code}.")


class ValioTransportError(ValioError):
    """The API could not be reached within the retry budget."""


class ValioResponseError(ValioError):
    """The API returned an invalid response."""


class ValioClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time,
        attempts: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        if not settings.valio_base_url or not settings.valio_api_key:
            raise ValioError("Set VALIO_BASE_URL and VALIO_API_KEY before calling Valio.")
        if attempts < 1 or backoff_seconds < 0 or settings.valio_timeout_seconds <= 0:
            raise ValueError("Attempts and timeout must be positive; backoff must be nonnegative.")
        self._http = httpx.AsyncClient(
            base_url=settings.valio_base_url.rstrip("/") + "/",
            headers={"X-API-Key": settings.valio_api_key},
            timeout=settings.valio_timeout_seconds,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        self._attempts = attempts
        self._backoff = backoff_seconds
        self._cache: TTLCache[PriceResponse | AvailabilityResponse] = TTLCache(clock=clock)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _retry_delay(self, retry_after: str | None, attempt: int) -> float:
        delay = self._backoff * math.pow(2, attempt)
        if retry_after is None:
            return delay
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(retry_after).timestamp() - self._clock()
            except (ValueError, TypeError, OverflowError):
                return delay
        return max(delay, seconds) if math.isfinite(seconds) else delay

    async def _request[M: BaseModel](
        self, path: str, model: type[M], body: ProductsRequest | None = None
    ) -> M:
        for attempt in range(self._attempts):
            try:
                response = await self._http.request(
                    "GET" if body is None else "POST",
                    path,
                    json=None if body is None else body.model_dump(mode="json", by_alias=True),
                )
            except httpx.TransportError:
                if attempt + 1 == self._attempts:
                    raise ValioTransportError("Valio transport failed after retries.") from None
                await self._sleep(self._retry_delay(None, attempt))
                continue
            status = response.status_code
            if status == 429 or 500 <= status <= 599:
                if attempt + 1 == self._attempts:
                    raise ValioHTTPError(status)
                await self._sleep(self._retry_delay(response.headers.get("Retry-After"), attempt))
                continue
            if not response.is_success:
                raise ValioHTTPError(status)
            try:
                return model.model_validate_json(response.content)
            except (ValidationError, ValueError):
                raise ValioResponseError("Valio API returned an invalid response.") from None
        raise AssertionError("Retry loop must return or raise.")

    async def ping(self) -> PingResponse:
        return await self._request("/api/ping", PingResponse)

    async def price(
        self, product_codes: Iterable[str], requested_date: date, *, customer: str = "123"
    ) -> PriceResponse:
        return await self._products("price", product_codes, requested_date, customer, PriceResponse)

    async def availability(
        self,
        product_codes: Iterable[str],
        requested_date: date,
        *,
        customer: str = "123",
        refresh: bool = False,
    ) -> AvailabilityResponse:
        return await self._products(
            "availability",
            product_codes,
            requested_date,
            customer,
            AvailabilityResponse,
            refresh=refresh,
        )

    async def _products[M: (PriceResponse, AvailabilityResponse)](
        self,
        kind: Literal["price", "availability"],
        product_codes: Iterable[str],
        requested_date: date,
        customer: str,
        model: type[M],
        *,
        refresh: bool = False,
    ) -> M:
        codes = list(product_codes)
        if not codes:
            raise ValueError("At least one product code is required.")
        entries: dict[str, M] = {}
        missing: list[str] = []
        for code in dict.fromkeys(codes):
            cached = (
                None if refresh else self._cache.get(CacheKey(kind, code, requested_date, customer))
            )
            if isinstance(cached, model):
                entries[code] = cached
            else:
                missing.append(code)
        for start in range(0, len(missing), BATCH_SIZE):
            batch = missing[start : start + BATCH_SIZE]
            body = ProductsRequest(customer=customer, requestedDate=requested_date, products=batch)
            response = await self._request(f"/api/{kind}", model, body)
            returned_codes = [product.product_id for product in response.products]
            if len(returned_codes) != len(batch) or set(returned_codes) != set(batch):
                raise ValioResponseError("Valio API returned mismatched product codes.")
            for product in response.products:
                entry = response.model_copy(update={"products": [product]}).model_copy(deep=True)
                entries[product.product_id] = entry
                self._cache.set(CacheKey(kind, product.product_id, requested_date, customer), entry)
        # Copy so callers cannot mutate the cached wire models.
        return (
            entries[codes[0]]
            .model_copy(update={"products": [entries[code].products[0] for code in codes]})
            .model_copy(deep=True)
        )
