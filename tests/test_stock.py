"""Stock photo levels become refill items in code, then run the normal policy path."""

import base64
import json
from collections.abc import Iterator
from itertools import count
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.stock import stock_items
from agent.vision import parse_levels
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.errors import GatewayError
from gateway.main import create_app
from gateway.runner import AGENT_ID
from gateway.schemas import Customer, RequestCreate
from gateway.settings import Settings
from gateway.store import MemoryStore
from tests.engine_support import BUYER, mandate
from tests.test_e2e import poll
from tests.test_offers import FakeValio, price, product, stock

STOCK = [
    {
        "name": "Flour",
        "aliases": ["jauho"],
        "description": "flour",
        "full_quantity": 10,
        "unit": "kg",
        "reorder_below_percent": 50,
        "constraints": [],
    },
    {
        "name": "Oats",
        "description": "oats",
        "full_quantity": 20,
        "unit": "kg",
        "reorder_below_percent": 30,
    },
    {
        "name": "Spoons",
        "description": "invented spoons",
        "full_quantity": 1000,
        "unit": "pcs",
        "reorder_below_percent": 30,
    },
]


def customer(stock_list: list[dict[str, object]] | None = None) -> Customer:
    return Customer.model_validate_json(
        json.dumps(
            {
                "customer_id": "invented-stock",
                "name": "Invented Cook",
                "company": "Invented Kitchen",
                "role": "Chef",
                "buyer_id": BUYER,
                "age_group": "35_to_54",
                "playful": False,
                "preferences": [],
                "exclude_allergens": [],
                "defaults": {"priority": "balanced", "typical_guests": None, "budget": None},
                "order_history": [],
                "simulated": True,
                "stock": STOCK if stock_list is None else stock_list,
            }
        )
    )


def test_levels_become_refill_quantities_and_notes() -> None:
    items, notes = stock_items({"flour": 25, "Oats": 80, "Spoons": 12.5, "Saffron": 0}, customer())
    assert [(i.description, i.quantity, i.unit) for i in items] == [
        ("flour", 7.5, "kg"),
        ("invented spoons", 875.0, "pcs"),
    ]
    assert any("Oats is 80 % full" in note for note in notes)
    assert any("Saffron is not in your stock list" in note for note in notes)
    assert any("ordering 7.5 kg" in note for note in notes)


def test_pieces_round_up_to_whole_units() -> None:
    items, _ = stock_items({"Spoons": 0.05}, customer())
    assert items[0].quantity == 1000.0
    items, _ = stock_items({"Spoons": 29.95}, customer())
    assert items[0].quantity == 701.0


@pytest.mark.parametrize(
    ("levels", "profile", "code"),
    [
        ({"Flour": 90}, STOCK, "NOTHING_TO_REORDER"),
        ({"Saffron": 0}, STOCK, "NOTHING_TO_REORDER"),
        ({"Flour": 0}, [], "STOCK_PROFILE_REQUIRED"),
    ],
)
def test_nothing_to_order_is_a_clear_error(
    levels: dict[str, float], profile: list[dict[str, object]], code: str
) -> None:
    with pytest.raises(GatewayError) as error:
        stock_items(levels, customer(profile))
    assert error.value.code == code


def test_levels_are_percentages() -> None:
    with pytest.raises(ValidationError, match="0 to 100"):
        RequestCreate.model_validate({"customer_id": "c", "stock_levels": {"Milk": 120.0}})
    request = RequestCreate.model_validate({"customer_id": "c", "stock_levels": {"Milk": 25.0}})
    assert request.stock_levels == {"Milk": 25.0}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"Milk": 25, "Spoons": 15}', {"Milk": 25.0, "Spoons": 15.0}),
        ('```json\n{"Milk": 25}\n```', {"Milk": 25.0}),
        ('I think {"x": "y"} then {"Milk": 140, "Cups": -3}', {"Milk": 100.0, "Cups": 0.0}),
    ],
)
def test_vision_reply_is_parsed_and_clamped(text: str, expected: dict[str, float]) -> None:
    assert parse_levels(text) == expected


@pytest.mark.parametrize("text", ["", "no idea", '{"Milk": "low"}', '{"Milk": true}', "{}"])
def test_vision_reply_without_levels_is_rejected(text: str) -> None:
    with pytest.raises(GatewayError) as error:
        parse_levels(text)
    assert error.value.code == "VISION_OUTPUT_INVALID"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    records = [
        product("900701", names={"en": "Invented pantry flour"}),
        product("900702", names={"en": "Invented pantry oats"}),
    ]
    (tmp_path / "invented-stock.json").write_text(customer().model_dump_json(), encoding="utf-8")
    ids = count(1)
    app = create_app(
        Settings(
            store="memory",
            valio_api_key="",
            catalog_path=str(tmp_path / "missing.json"),
            nvidia_api_key="invented-test-key",
        ),
        catalog=Catalog(records),
        valio=FakeValio(
            [price(p.product_id) for p in records], [stock(p.product_id) for p in records]
        ),
        store=MemoryStore(),
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={BUYER: mandate(buyer_id=BUYER, auto_limit=100.0, allowed_agents=(AGENT_ID,))},
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )
    with TestClient(app) as test_client:
        yield test_client


def test_stock_request_runs_the_normal_policy_path(client: TestClient) -> None:
    response = client.post(
        "/requests",
        json={
            "customer_id": "invented-stock",
            "requested_date": "2026-11-20",
            "stock_levels": {"Flour": 25, "Oats": 80, "Saffron": 0},
        },
    )
    assert response.status_code == 202, response.text
    detail = poll(client, response.json()["request_id"])
    assert [(i.description, i.quantity) for i in detail.intent.items] == [("flour", 7.5)]
    assert any("Saffron" in note for note in detail.intent.assumptions)
    assert detail.quote is not None and detail.quote.lines[0].product_id == "900701"
    assert detail.status in {"ORDERED", "WAITING_APPROVAL"}
    assert verify_chain(detail.steps)


def test_stock_request_with_nothing_low_is_refused(client: TestClient) -> None:
    response = client.post(
        "/requests",
        json={"customer_id": "invented-stock", "stock_levels": {"Flour": 90, "Oats": 95}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NOTHING_TO_REORDER"


def nvidia(reply: str, status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer invented-test-key"
        body = json.loads(request.content)
        content = body["messages"][0]["content"]
        assert "Flour, Oats, Spoons" in content[0]["text"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(status, json={"choices": [{"message": {"content": reply}}]})

    return httpx.MockTransport(handle)


IMAGE = base64.b64encode(b"invented image bytes").decode()


def test_stock_photo_is_read_and_matched_to_the_stock_list(client: TestClient) -> None:
    client.app.state.vision_transport = nvidia('{"jauho": 25, "oats": 80, "Ketchup": 10}')
    response = client.post(
        "/stock/read",
        json={"customer_id": "invented-stock", "image_base64": IMAGE, "mime_type": "image/png"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["levels"] == {"Flour": 25.0, "Oats": 80.0}
    assert response.json()["unmatched"] == ["Ketchup"]


@pytest.mark.parametrize(
    ("transport", "image", "status", "code"),
    [
        (nvidia("", 500), IMAGE, 502, "VISION_FAILED"),
        (nvidia("nothing to see"), IMAGE, 502, "VISION_OUTPUT_INVALID"),
        (None, "not base64!", 422, "IMAGE_INVALID"),
    ],
)
def test_stock_photo_errors_are_clear(
    client: TestClient, transport: httpx.MockTransport | None, image: str, status: int, code: str
) -> None:
    client.app.state.vision_transport = transport
    response = client.post(
        "/stock/read",
        json={"customer_id": "invented-stock", "image_base64": image, "mime_type": "image/png"},
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_stock_photo_needs_an_nvidia_key(tmp_path: Path) -> None:
    (tmp_path / "invented-stock.json").write_text(customer().model_dump_json(), encoding="utf-8")
    app = create_app(
        Settings(store="memory", nvidia_api_key=""),
        store=MemoryStore(),
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/stock/read",
            json={
                "customer_id": "invented-stock",
                "image_base64": IMAGE,
                "mime_type": "image/png",
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "VISION_UNAVAILABLE"
