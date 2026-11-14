"""Committed presets replay invented recordings through the real app and mandates."""

import json
from itertools import count
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from agent.llm import FakeLLM
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.catalog.models import SlimRecord
from gateway.customers import CUSTOMERS_DIR
from gateway.main import create_app
from gateway.schemas import Quote, RequestDetail
from gateway.settings import Settings
from gateway.store import MemoryStore
from gateway.valio.models import AvailabilityResponse, PriceResponse
from tests.test_e2e import poll
from tests.test_offers import NOW, FakeValio

ROOT = Path(__file__).resolve().parents[1]
NUTS = {"tree_nuts", "peanuts"}


def recording(preset: str) -> dict:
    return json.loads((ROOT / "tests" / "recorded" / "pipeline" / f"{preset}.json").read_bytes())


def environment(tmp_path: Path, preset: str, *, reorder: bool = False) -> TestClient:
    recorded = recording(preset)
    responses = recorded["llm"]
    llm = FakeLLM(
        structured_outputs=responses["structured_outputs"],
    )
    ids = count(1)
    app = create_app(
        Settings(
            agent_mode="pipeline",
            store="memory",
            valio_api_key="",
            gemini_api_key="",
            gemini_model="",
            gemini_expansion_model="",
            database_url="",
        ),
        catalog=Catalog(TypeAdapter(list[SlimRecord]).validate_python(recorded["catalog"])),
        valio=FakeValio(
            PriceResponse.model_validate(recorded["valio"]["price"]).products,
            AvailabilityResponse.model_validate(recorded["valio"]["availability"]).products,
        ),
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        customers_dir=CUSTOMERS_DIR,
        customer_overlay_dir=tmp_path / "unused-overlays",
        llm=llm,
    )
    client = TestClient(app)
    app.state.gateway_http_client = client
    return client


def assert_audit(client: TestClient, detail: RequestDetail) -> None:
    response = client.get(f"/audit/{detail.request_id}")
    assert response.status_code == 200
    assert response.json()["chain_valid"] is True
    assert verify_chain(detail.steps)
    assert response.json()["steps"] == [step.model_dump(mode="json") for step in detail.steps]


def start(client: TestClient, preset: str) -> RequestDetail:
    # Read the committed request without injecting case-specific quantities or constraints.
    body = json.loads((ROOT / "presets" / f"{preset}.json").read_bytes())
    assert body["customer_id"] == body["request"]["customer_id"]
    response = client.post("/requests", json=body["request"])
    assert response.status_code == 202, response.text
    detail = poll(client, response.json()["request_id"])
    assert detail.error is None
    assert_audit(client, detail)
    assert not client.app.state.llm.structured_outputs
    return detail


def options(client: TestClient, detail: RequestDetail) -> list[Quote]:
    assert detail.status == "WAITING_APPROVAL" and detail.order is None
    assert detail.quote is not None and detail.quote.policy.outcome == "NEEDS_APPROVAL"
    response = client.get(f"/approvals/{detail.approval_id}")
    assert response.status_code == 200
    result = TypeAdapter(list[Quote]).validate_json(json.dumps(response.json()["options"]))
    assert 2 <= len(result) <= 3
    assert result[0].quote_id == detail.quote.quote_id
    return result


def approve(client: TestClient, detail: RequestDetail, quote: Quote) -> RequestDetail:
    response = client.post(
        f"/approvals/{detail.approval_id}/approve",
        json={
            "approver": "Invented Approver",
            "quote_id": quote.quote_id,
            "content_hash": quote.content_hash,
            "comment": "Approved invented order",
        },
    )
    assert response.status_code == 200, response.text
    ordered = poll(client, detail.request_id)
    assert ordered.status == "ORDERED" and ordered.order is not None
    assert ordered.order.approved_by == "Invented Approver"
    assert ordered.order.quote_id == quote.quote_id
    assert ordered.order.lines == quote.lines
    assert sum(step.type == "ORDER_PLACED" for step in ordered.steps) == 1
    assert_audit(client, ordered)
    return ordered


def rules(quote: Quote) -> set[str]:
    return {rule.rule_id for rule in quote.policy.rules_fired}


def test_restaurant_weekly_orders_alone_under_chefs_mandate(tmp_path: Path) -> None:
    with environment(tmp_path, "restaurant_weekly") as client:
        detail = start(client, "restaurant_weekly")
        assert detail.customer_id == "c_chef" and detail.buyer_id == "bistro-example"
        assert detail.status == "ORDERED" and detail.order is not None
        assert detail.quote is not None and len(detail.quote.lines) == 4
        assert detail.quote.policy.outcome == "AUTO" and detail.quote.policy.rules_fired == []
        assert detail.approval_id is None and detail.order.approved_by is None
        assert not any(step.actor == "human" for step in detail.steps)
        decisions = [step for step in detail.steps if step.type == "POLICY_DECISION"]
        assert decisions and all(step.detail["mandate"] == "bistro-example" for step in decisions)
        assert all("bistro-example" in step.title for step in decisions)
        assert client.get("/customers/c_chef").json()["playful"] is True
        assert detail.suggestions == []
        assert "SUGGESTIONS_PROPOSED" not in {step.type for step in detail.steps}


def test_wedding_split_and_valio_substitute_need_approval(tmp_path: Path) -> None:
    with environment(tmp_path, "wedding_120") as client:
        detail = start(client, "wedding_120")
        variants = options(client, detail)
        quote = variants[0]
        assert rules(quote) & {"SPLIT_DELIVERY", "PARTIAL_QUANTITY"}
        assert "SUBSTITUTION" in rules(quote)
        assert any(line.is_split for line in quote.lines)
        assert any(line.is_substitute and line.substitute_source == "valio" for line in quote.lines)
        assert len({variant.content_hash for variant in variants}) == len(variants)
        approve(client, detail, quote)


def test_birthday_may_contain_nuts_has_safe_approved_option(tmp_path: Path) -> None:
    with environment(tmp_path, "birthday_kids_25") as client:
        detail = start(client, "birthday_kids_25")
        variants = options(client, detail)
        assert NUTS <= set(detail.intent.exclude_allergens)
        assert "ALLERGEN_MAY_CONTAIN" in rules(variants[0])
        assert any(NUTS & set(line.allergens.may_contain) for line in variants[0].lines)
        safe = next(
            quote
            for quote in variants[1:]
            if all(
                line.allergens.data_available
                and all(line.dietary_check[f"no_{nut}"] == "confirmed" for nut in NUTS)
                and not NUTS & set(line.allergens.may_contain + line.allergens.contains)
                for line in quote.lines
            )
        )
        assert "ALLERGEN_MAY_CONTAIN" not in rules(safe)
        approve(client, detail, safe)


def test_company_lunch_requires_finnish_origin_and_cheaper_budget_option(tmp_path: Path) -> None:
    with environment(tmp_path, "company_lunch_60") as client:
        detail = start(client, "company_lunch_60")
        assert detail.customer_id == "c_pena" and detail.buyer_id == "ab-oy"
        variants = options(client, detail)
        assert {"BUDGET_EXCEEDED", "APPROVAL_ALWAYS_REQUIRED"} <= rules(variants[0])
        assert any(
            p.key == "finnish_origin" and p.level == "require" for p in detail.intent.preferences
        )
        assert all(
            line.dietary_check["finnish_origin"] == "confirmed"
            for quote in variants
            for line in quote.lines
        )
        cheaper = next(
            quote
            for quote in variants[1:]
            if quote.option_label in {"Cheaper option within budget", "Cheaper option"}
        )
        assert cheaper.total < variants[0].total
        assert cheaper.budget is not None and cheaper.total <= cheaper.budget.limit
        assert "APPROVAL_ALWAYS_REQUIRED" in rules(cheaper)
        assert detail.suggestions == []
        approve(client, detail, cheaper)
