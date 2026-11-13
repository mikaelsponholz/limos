import json
from collections.abc import AsyncIterator, Callable
from datetime import date
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest

from gateway.settings import Settings
from gateway.valio.client import (
    ValioClient,
    ValioError,
    ValioHTTPError,
    ValioResponseError,
    ValioTransportError,
)
from scripts.fetch_valio_data import download

REQUESTED_DATE = date(2026, 11, 16)
TEST_KEY = "invented-test-key"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(handler: Callable[[httpx.Request], httpx.Response], clock: FakeTime) -> ValioClient:
    return ValioClient(
        Settings(valio_base_url="https://valio.invalid", valio_api_key=TEST_KEY),
        transport=httpx.MockTransport(handler),
        clock=clock.clock,
        sleep=clock.sleep,
    )


def product_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if request.url.path == "/api/price":
        return httpx.Response(
            200,
            json={
                "customerId": body["customer"],
                "currency": "EUR",
                "pricingDate": body["requestedDate"],
                "products": [
                    {"productId": code, "quantity": 1, "netPriceFound": False, "error": True}
                    for code in body["products"]
                ],
            },
        )
    return httpx.Response(
        200,
        json={
            "timeNow": "2026-11-13T12:00:00",
            "requestedDate": body["requestedDate"],
            "products": [
                {"productId": code, "salesBlock": False, "scheduleLines": []}
                for code in body["products"]
            ],
        },
    )


@pytest.mark.parametrize("kind", ["price", "availability"])
async def test_batches_merge_in_request_order(kind: str) -> None:
    sizes: list[int] = []
    codes = [f"TEST-{number:04}" for number in range(2500)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sizes.append(len(body["products"]))
        assert request.headers["X-API-Key"] == TEST_KEY
        assert body["requestedDate"] == REQUESTED_DATE.isoformat()
        return product_response(request)

    async with make_client(handler, FakeTime()) as client:
        method = client.price if kind == "price" else client.availability
        result = await method(iter(codes), REQUESTED_DATE)
    assert sizes == [1000, 1000, 500]
    assert [product.product_id for product in result.products] == codes


@pytest.mark.parametrize("kind", ["price", "availability"])
async def test_cache_hit_partial_hit_expiry_and_context(kind: str) -> None:
    requests: list[list[str]] = []
    clock = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content)["products"])
        return product_response(request)

    async with make_client(handler, clock) as client:
        method = client.price if kind == "price" else client.availability
        first = await method(["TEST-A"], REQUESTED_DATE)
        first.products[0].product_id = "MODIFIED"
        cached = await method(["TEST-A"], REQUESTED_DATE)
        assert cached.products[0].product_id == "TEST-A"
        await method(["TEST-A", "TEST-B"], REQUESTED_DATE)
        assert requests == [["TEST-A"], ["TEST-B"]]
        clock.now += 299
        await method(["TEST-A"], REQUESTED_DATE)
        assert len(requests) == 2
        clock.now += 1
        await method(["TEST-A"], REQUESTED_DATE)
        assert len(requests) == 3
        await method(["TEST-A"], date(2026, 11, 17))
        await method(["TEST-A"], REQUESTED_DATE, customer="different-customer")
        other_method = client.availability if kind == "price" else client.price
        await other_method(["TEST-A"], REQUESTED_DATE)
        assert len(requests) == 6


@pytest.mark.parametrize("retry_after", ["7", "http-date", "invalid", None])
async def test_retry_429_then_success_honors_retry_after(retry_after: str | None) -> None:
    clock = FakeTime()
    calls = 0
    header = formatdate(clock.now + 7, usegmt=True) if retry_after == "http-date" else retry_after

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={} if header is None else {"Retry-After": header})
        return product_response(request)

    async with make_client(handler, clock) as client:
        result = await client.price(["TEST-RETRY"], REQUESTED_DATE)
    assert len(result.products) == 1
    assert calls == 2
    assert clock.sleeps == ([7] if retry_after in ("7", "http-date") else [0.5])


async def test_503_exhausted() -> None:
    clock = FakeTime()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text=TEST_KEY)

    async with make_client(handler, clock) as client:
        with pytest.raises(ValioError, match="HTTP 503") as error:
            await client.ping()
    assert calls == 3
    assert clock.sleeps == [0.5, 1]
    assert TEST_KEY not in str(error.value)
    assert TEST_KEY not in repr(error.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_other_4xx_not_retried_and_key_not_in_errors(status: int) -> None:
    clock = FakeTime()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["X-API-Key"] == TEST_KEY
        return httpx.Response(status, text=f"Failed using {TEST_KEY}")

    async with make_client(handler, clock) as client:
        assert TEST_KEY not in repr(client)
        with pytest.raises(ValioHTTPError) as error:
            await client.price(["TEST-BAD"], REQUESTED_DATE)
    assert error.value.status_code == status
    assert TEST_KEY not in str(error.value)
    assert TEST_KEY not in repr(error.value)
    assert calls == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("recover", [False, True])
async def test_timeout_retries(recover: bool) -> None:
    clock = FakeTime()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.extensions["timeout"]["read"] == 5
        if recover and calls == 2:
            return httpx.Response(200, json={"ok": True})
        raise httpx.ReadTimeout(TEST_KEY, request=request)

    async with make_client(handler, clock) as client:
        if recover:
            assert (await client.ping()).root == {"ok": True}
        else:
            with pytest.raises(ValioTransportError) as error:
                await client.ping()
            assert TEST_KEY not in str(error.value)
            assert error.value.__suppress_context__
    assert calls == (2 if recover else 3)
    assert clock.sleeps == ([0.5] if recover else [0.5, 1])


@pytest.mark.parametrize(
    "content", [b"not json", b'{"products": []}', b'{"secret": "' + TEST_KEY.encode() + b'"}']
)
async def test_invalid_response_sanitized_without_retry(content: bytes) -> None:
    clock = FakeTime()
    async with make_client(lambda request: httpx.Response(200, content=content), clock) as client:
        with pytest.raises(ValioResponseError) as error:
            await client.price(["TEST-INVALID"], REQUESTED_DATE)
    assert TEST_KEY not in str(error.value)
    assert clock.sleeps == []


async def test_empty_schedule_unknown_product_cached_normally() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return product_response(request)

    async with make_client(handler, FakeTime()) as client:
        for _ in range(2):
            response = await client.availability(["TEST-UNKNOWN"], REQUESTED_DATE)
            assert not response.products[0].deliverable
    assert calls == 1


async def test_mismatched_products_not_cached() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"customerId": "test", "currency": "EUR", "pricingDate": "date", "products": []},
        )

    async with make_client(handler, FakeTime()) as client:
        for _ in range(2):
            with pytest.raises(ValioResponseError, match="mismatched"):
                await client.price(["TEST-MISSING"], REQUESTED_DATE)
    assert calls == 2


async def test_empty_input_no_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Empty input must not make a request")

    async with make_client(handler, FakeTime()) as client:
        with pytest.raises(ValueError, match="At least one"):
            await client.price([], REQUESTED_DATE)


def test_configuration_key_not_in_repr() -> None:
    settings = Settings(valio_api_key=TEST_KEY)
    assert TEST_KEY not in repr(settings)
    with pytest.raises(ValioError, match="VALIO_BASE_URL") as error:
        ValioClient(settings)
    assert TEST_KEY not in str(error.value)


async def test_download_redirect_streams_without_forwarding_key(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "valio.invalid":
            assert request.headers["X-API-Key"] == TEST_KEY
            return httpx.Response(302, headers={"Location": "https://files.invalid/invented.json"})
        assert "X-API-Key" not in request.headers
        return httpx.Response(200, content=b'{"invented": true}')

    destination = tmp_path / "invented.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await download(
            client,
            "https://valio.invalid/api/products/download",
            destination,
            TEST_KEY,
            httpx.URL("https://valio.invalid"),
        )
    assert len(calls) == 2
    assert destination.read_bytes() == b'{"invented": true}'
    assert not destination.with_suffix(".json.part").exists()


class InterruptedStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial invented content"
        raise httpx.ReadTimeout("Invented interruption")


async def test_interrupted_download_preserves_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "invented.json"
    destination.write_bytes(b"previous invented content")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=InterruptedStream()))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ReadTimeout):
            await download(
                client,
                "https://valio.invalid/download",
                destination,
                TEST_KEY,
                httpx.URL("https://valio.invalid"),
            )
    assert destination.read_bytes() == b"previous invented content"
    assert not destination.with_suffix(".json.part").exists()


async def test_download_rejects_insecure_redirect(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://files.invalid/invented.json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="downgrade"):
            await download(
                client,
                "https://valio.invalid/download",
                tmp_path / "invented.json",
                TEST_KEY,
                httpx.URL("https://valio.invalid"),
            )
    assert calls == 1
