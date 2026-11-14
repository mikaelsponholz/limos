"""Standalone and request quotes enforce additive rules with invented evidence only."""

import json
from collections.abc import Iterator
from itertools import count
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp_types import TextContent

from gateway.api.dependencies import Resources
from gateway.catalog.index import Catalog
from gateway.schemas import Customer, IntentItem, Preference, QuoteBody
from gateway.tools import quote_contexts
from tests.engine_support import BUYER, mandate, request_detail
from tests.test_mcp_session import call, mcp_session
from tests.test_offers import NOW, REQUESTED, FakeValio, price, product, stock

OTHER_BUYER = "invented-other-buyer"
AGENT = "limos-mcp"


def profile(customer_id: str = "invented-cook", **changes: Any) -> Customer:
    return Customer.model_validate(
        {
            "customer_id": customer_id,
            "buyer_id": BUYER,
            "name": "Invented Cook",
            "company": "Invented Kitchen",
            "role": "Chef",
            "age_group": "35_to_54",
            "playful": False,
            "preferences": [
                {"key": "lactose_free", "level": "require"},
                {"key": "organic", "level": "require"},
                {"key": "vegan", "level": "prefer"},
            ],
            "exclude_allergens": ["milk"],
            "defaults": {"priority": "balanced", "typical_guests": 10, "budget": None},
            "order_history": [],
            "simulated": True,
            **changes,
        }
    )


def write_profile(directory: Path, customer: Customer) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{customer.customer_id}.json").write_text(
        customer.model_dump_json(), encoding="utf-8"
    )


@pytest.fixture
def rules_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from gateway.main import create_app
    from gateway.settings import Settings
    from gateway.store import MemoryStore

    monkeypatch.setenv("LIMOS_AGENT_ID", AGENT)
    monkeypatch.setenv("LIMOS_GATEWAY_URL", "http://invented.invalid")
    write_profile(tmp_path, profile())
    records = [
        product("900801", allergens={"contains": ["milk", "lactose"], "data_available": True}),
        product(
            "900802",
            allergens={"may_contain": ["milk"], "data_available": True},
            claims=["lactose_free"],
        ),
        product("900803", claims=["lactose_free"]),
        product("900804", allergens={"data_available": True}),
        product(
            "900805",
            allergens={"contains": ["milk", "egg", "wheat"], "data_available": True},
            claims=["lactose_free"],
        ),
    ]
    ids = count(1)
    return create_app(
        Settings(store="memory", valio_api_key="", gemini_api_key="", database_url=""),
        catalog=Catalog(records),
        valio=FakeValio(
            [price(p.product_id) for p in records], [stock(p.product_id) for p in records]
        ),
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={
            buyer: mandate(buyer_id=buyer, allowed_agents=(AGENT,))
            for buyer in (BUYER, OTHER_BUYER)
        },
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
    )


@pytest.fixture
def rules_client(rules_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(rules_app) as client:
        yield client


def body(product_id: str = "900801", **changes: Any) -> dict[str, Any]:
    return {
        "buyer_id": BUYER,
        "agent_id": AGENT,
        "requested_date": REQUESTED.isoformat(),
        "lines": [{"item_id": "it_food", "product_id": product_id, "quantity_sales_units": 1}],
        **changes,
    }


def quote(client: TestClient, product_id: str = "900801", **changes: Any) -> dict[str, Any]:
    response = client.post("/gateway/quotes", json=body(product_id, **changes))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("product_id", "rules", "outcome"),
    [
        ("900801", {"ALLERGEN_CONTAINS", "DIETARY_VIOLATED"}, "BLOCK"),
        ("900802", {"ALLERGEN_MAY_CONTAIN"}, "NEEDS_APPROVAL"),
        ("900803", {"ALLERGEN_DATA_MISSING"}, "NEEDS_APPROVAL"),
        ("900804", {"DIETARY_UNKNOWN"}, "NEEDS_APPROVAL"),
    ],
)
def test_standalone_profile_rules_and_buyer_isolation(
    rules_client: TestClient, product_id: str, rules: set[str], outcome: str
) -> None:
    result = quote(rules_client, product_id)
    assert result["policy"]["outcome"] == outcome
    fired = result["policy"]["rules_fired"]
    assert {rule["rule_id"] for rule in fired} == rules
    assert all(rule["description"] and rule["product_id"] == product_id for rule in fired)
    other = quote(rules_client, product_id, buyer_id=OTHER_BUYER)
    assert other["policy"]["outcome"] == "AUTO"
    assert other["policy"]["rules_fired"] == []


def test_customer_union_applies_to_every_line(rules_app: FastAPI, rules_client: TestClient) -> None:
    write_profile(
        rules_app.state.customers_dir,
        profile(
            "invented-second-cook",
            exclude_allergens=["egg"],
            preferences=[{"key": "gluten_free", "level": "require"}],
        ),
    )
    result = quote(
        rules_client,
        lines=[
            {"item_id": item, "product_id": "900805", "quantity_sales_units": 1}
            for item in ("it_first", "it_second")
        ],
    )
    assert result["policy"]["outcome"] == "BLOCK"
    for line in result["lines"]:
        assert line["dietary_check"]["no_milk"] == "violated"
        assert line["dietary_check"]["no_egg"] == "violated"
        assert line["dietary_check"]["lactose_free"] == "confirmed"
        assert line["dietary_check"]["gluten_free"] == "unknown"
    assert {rule["item_id"] for rule in result["policy"]["rules_fired"]} == {
        "it_first",
        "it_second",
    }


def test_overlay_rules_are_used(rules_app: FastAPI, rules_client: TestClient) -> None:
    write_profile(
        rules_app.state.customer_overlay_dir,
        profile(exclude_allergens=["egg"], preferences=[]),
    )
    result = quote(rules_client, "900805")
    assert result["policy"]["outcome"] == "BLOCK"
    assert result["lines"][0]["dietary_check"] == {"no_egg": "violated"}


@pytest.mark.parametrize("additions", [[], ["egg"], ["milk", "milk"]])
def test_body_cannot_remove_profile_rule(rules_client: TestClient, additions: list[str]) -> None:
    result = quote(rules_client, exclude_allergens=additions, constraints=[])
    assert result["policy"]["outcome"] == "BLOCK"
    assert {rule["rule_id"] for rule in result["policy"]["rules_fired"]} >= {
        "ALLERGEN_CONTAINS",
        "DIETARY_VIOLATED",
    }


@pytest.mark.parametrize(
    ("additions", "rule"),
    [
        ({"exclude_allergens": ["milk"]}, "ALLERGEN_CONTAINS"),
        ({"constraints": ["lactose_free"]}, "DIETARY_VIOLATED"),
        ({"constraints": ["no_milk"]}, "ALLERGEN_CONTAINS"),
    ],
)
def test_body_additions_without_profile(
    rules_client: TestClient, additions: dict[str, Any], rule: str
) -> None:
    result = quote(rules_client, buyer_id=OTHER_BUYER, **additions)
    assert result["policy"]["outcome"] == "BLOCK"
    assert {entry["rule_id"] for entry in result["policy"]["rules_fired"]} == {rule}


def test_request_context_is_preserved_and_body_only_adds(
    rules_app: FastAPI, rules_client: TestClient
) -> None:
    detail = request_detail()
    detail.intent.exclude_allergens = ["egg"]
    detail.intent.preferences = [Preference(key="organic", level="require")]
    detail.intent.items = [
        IntentItem(
            item_id="it_food",
            description="Invented food",
            description_fi="Invented food",
            quantity=1.0,
            unit="kg",
            constraints=["no_wheat"],
            used_in=[],
        )
    ]
    rules_app.state.store.save_request(detail)
    original = quote(rules_client, "900805", request_id=detail.request_id)
    checks = original["lines"][0]["dietary_check"]
    assert set(checks) == {"organic", "no_egg", "no_wheat"}
    added = quote(
        rules_client,
        "900805",
        request_id=detail.request_id,
        exclude_allergens=["milk"],
        constraints=["vegan"],
    )
    assert set(added["lines"][0]["dietary_check"]) == {*checks, "no_milk", "vegan"}
    assert {rule["rule_id"] for rule in added["policy"]["rules_fired"]} >= {
        "ALLERGEN_CONTAINS",
        "PREFERENCE_REQUIRED_UNMET",
    }
    assert rules_app.state.store.get_request(detail.request_id, BUYER).intent == detail.intent


def test_context_union_is_stable_and_deduplicated(rules_app: FastAPI) -> None:
    from starlette.requests import Request

    write_profile(
        rules_app.state.customers_dir,
        profile("invented-second-cook", exclude_allergens=["milk", "egg"]),
    )
    env = Resources(Request({"type": "http", "app": rules_app}))
    parsed = QuoteBody.model_validate_json(
        json.dumps(body(exclude_allergens=["egg", "wheat", "milk"], constraints=["vegan", "vegan"]))
    )
    context = quote_contexts(parsed, None, env)["it_food"]
    assert context.exclude_allergens == ["milk", "egg", "wheat"]
    assert context.required_preferences == ["lactose_free"]
    assert context.constraints == ["vegan"]


@pytest.mark.parametrize("field", ["exclude_allergens", "constraints"])
def test_quote_additions_use_contract_values(rules_client: TestClient, field: str) -> None:
    response = rules_client.post("/gateway/quotes", json=body(**{field: ["invented-invalid"]}))
    assert response.status_code == 422


@pytest.mark.parametrize(
    "additions", [{}, {"exclude_allergens": ["milk"], "constraints": ["vegan"]}]
)
async def test_mcp_blocks_allergen_quote_and_refuses_order(
    rules_app: FastAPI, additions: dict[str, Any]
) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=rules_app)) as http,
        mcp_session(http) as session,
    ):
        arguments = body(**additions)
        del arguments["agent_id"]
        result = await call(session, "request_quote", **arguments)
        assert result["policy"]["outcome"] == "BLOCK"
        assert "ALLERGEN_CONTAINS" in {rule["rule_id"] for rule in result["policy"]["rules_fired"]}
        if additions:
            assert "vegan" in result["lines"][0]["dietary_check"]
        refused = await session.call_tool(
            "place_order",
            {
                "buyer_id": BUYER,
                "quote_id": result["quote_id"],
                "idempotency_key": "invented-blocked-order",
            },
        )
        assert refused.is_error
        assert any(
            isinstance(content, TextContent) and "HTTP 422 BLOCKED:" in content.text
            for content in refused.content
        )
        assert rules_app.state.store.order_for_request(result["request_id"], BUYER) is None
