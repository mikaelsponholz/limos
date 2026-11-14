"""The plain HTTP client forwards optional quote restrictions without gateway imports."""

import json
from typing import Any

import httpx
import pytest

from clients.plain_client import main


@pytest.mark.parametrize(
    ("arguments", "allergens", "constraints"),
    [
        ([], [], []),
        (
            ["--exclude-allergens", "milk", "egg", "--constraints", "vegan"],
            ["milk", "egg"],
            ["vegan"],
        ),
    ],
)
def test_plain_client_passes_quote_additions(
    arguments: list[str],
    allergens: list[str],
    constraints: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/gateway/search":
            assert payload["exclude_allergens"] == allergens
            assert set(constraints) <= set(payload["constraints"])
            return httpx.Response(200, json={"products": [{"product_id": "900901"}]})
        if request.url.path == "/gateway/offers":
            return httpx.Response(
                200,
                json={"offers": [{"product_id": "900901", "quantity_sales_units": 1, "flags": []}]},
            )
        assert request.url.path == "/gateway/quotes"
        payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "policy": {"outcome": "BLOCK", "rules_fired": []},
                "lines": [],
                "total": 0.0,
                "delivery_date": "2026-11-17",
                "order_deadline": None,
            },
        )

    with httpx.Client(
        base_url="http://invented.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        assert main(arguments, client=http) == 1
    assert len(payloads) == 1
    assert payloads[0]["exclude_allergens"] == allergens
    assert payloads[0]["constraints"] == constraints
    assert capsys.readouterr().err == ""
