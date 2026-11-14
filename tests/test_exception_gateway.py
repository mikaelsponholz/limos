"""Contract v0.6 and both runner paths, with mocked services and invented products."""

from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.gateway_client import AGENT_ID as GEMINI
from agent.llm import FakeLLM, ToolRequest
from gateway.audit import verify_chain
from gateway.catalog.index import Catalog
from gateway.main import create_app
from gateway.quotes import compute_content_hash
from gateway.runner import AGENT_ID
from gateway.settings import Settings
from gateway.store import MemoryStore
from tests.agent_loop_support import EXPLANATION, bind_client, purchase
from tests.engine_support import BUYER, mandate
from tests.test_e2e import poll
from tests.test_offers import NOW, FakeValio, price, product, schedule, stock


def environment(tmp_path: Path, *, mode: str = "split", llm: FakeLLM | None = None) -> TestClient:
    ids = count(1)
    records = [
        product(
            "900701",
            names={"en": "Invented flour"},
            substitutions=[{"product": "900701", "substituteProduct": "900702"}]
            if mode == "valio"
            else [],
        ),
        product(
            "900702",
            names={"en": "Invented oats" if mode in {"valio", "agent"} else "Invented whole flour"},
        ),
        product("900703", names={"en": "Invented salt"}),
    ]
    availability = [stock(record.product_id) for record in records]
    if mode == "split":
        availability[0] = stock("900701", scheduleLines=[schedule(1)])
    elif mode in {"valio", "agent", "blocked"}:
        availability[0] = stock("900701", scheduleLines=[])
    app = create_app(
        Settings(
            agent_mode="tools", store="memory", gemini_api_key="", gemini_model="", valio_api_key=""
        ),
        catalog=Catalog(records),
        valio=FakeValio([price(record.product_id) for record in records], availability),
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={BUYER: mandate(allowed_agents=(AGENT_ID, GEMINI))},
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
        llm=llm,
    )
    return bind_client(TestClient(app), llm)


def start(client: TestClient, *, salt: bool = False, preferences: list | None = None):
    items = [{"description": "flour", "quantity": 3.0, "unit": "kg", "constraints": []}]
    if salt:
        items.append({**items[0], "description": "salt", "quantity": 1.0})
    response = client.post(
        "/requests",
        json={
            "buyer_id": BUYER,
            "requested_date": "2026-11-20",
            "items": items,
            "preferences": preferences or [],
        },
    )
    assert response.status_code == 202, response.text
    return poll(client, response.json()["request_id"])


def quote_body(lines: list[dict]) -> dict:
    return {"buyer_id": BUYER, "agent_id": AGENT_ID, "requested_date": "2026-11-20", "lines": lines}


@pytest.mark.parametrize(
    "metadata",
    [
        {"substitute_for": "900701"},
        {"substitute_source": "agent"},
        {"substitute_source": "valio", "substitute_for": "900703"},
    ],
)
def test_invalid_substitute_metadata_is_422(tmp_path: Path, metadata: dict) -> None:
    with environment(tmp_path) as client:
        response = client.post(
            "/gateway/quotes",
            json=quote_body(
                [
                    {
                        "item_id": "it_flour",
                        "product_id": "900702",
                        "quantity_sales_units": 1.0,
                        **metadata,
                    }
                ]
            ),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert not client.app.state.valio.calls


def test_three_lines_are_rejected_before_pricing(tmp_path: Path) -> None:
    with environment(tmp_path) as client:
        response = client.post(
            "/gateway/quotes",
            json=quote_body(
                [
                    {"item_id": "it_flour", "product_id": code, "quantity_sales_units": 1.0}
                    for code in ("900701", "900702", "900703")
                ]
            ),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert not client.app.state.valio.calls


def test_standalone_split_has_shared_need_and_second_flag(tmp_path: Path) -> None:
    with environment(tmp_path) as client:
        response = client.post(
            "/gateway/quotes",
            json=quote_body(
                [
                    {"item_id": "it_flour", "product_id": code, "quantity_sales_units": amount}
                    for code, amount in (("900701", 1.0), ("900702", 2.0))
                ]
            ),
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert [line["is_split"] for line in result["lines"]] == [False, True]
        assert [line["quantity_requested"]["value"] for line in result["lines"]] == [3, 3]
        assert {rule["rule_id"] for rule in result["policy"]["rules_fired"]} == {"SPLIT_DELIVERY"}


@pytest.mark.parametrize("gemini", [False, True])
def test_split_request_has_two_real_approval_options(tmp_path: Path, gemini: bool) -> None:
    turns = purchase("900701")
    turns[1] = [
        ToolRequest("check_offers", {"item_id": "it_1", "product_ids": ["900701", "900702"]})
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION]) if gemini else None
    with environment(tmp_path, llm=llm) as client:
        detail = start(client, salt=not gemini)
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert detail.quote.policy.outcome == "NEEDS_APPROVAL"
        response = client.get(f"/approvals/{detail.approval_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        options = result["options"]
        assert len(options) == 2
        assert result["quote"]["quote_id"] == detail.quote.quote_id == options[0]["quote_id"]
        assert [line["quantity_sales_units"] for line in options[0]["lines"][:2]] == [1, 2]
        assert [line["is_split"] for line in options[0]["lines"][:2]] == [False, True]
        assert options[0]["option_label"] == "Split the flour across two products"
        assert options[1]["option_label"] == "Use Invented whole flour for all of the flour"
        assert options[1]["lines"][0]["quantity_sales_units"] == 3
        assert not options[1]["lines"][0]["is_split"]
        assert "PARTIAL_QUANTITY" not in {
            rule["rule_id"] for rule in options[0]["policy"]["rules_fired"]
        }
        if not gemini:
            assert options[0]["lines"][-1] == options[1]["lines"][-1]
        assert compute_content_hash(detail.quote) == detail.quote.content_hash
        assert verify_chain(detail.steps)


@pytest.mark.parametrize("gemini", [False, True])
def test_valio_substitute_is_fetched_and_policy_marks_it(tmp_path: Path, gemini: bool) -> None:
    llm = (
        FakeLLM(tool_responses=purchase("900701"), structured_outputs=[EXPLANATION])
        if gemini
        else None
    )
    with environment(tmp_path, mode="valio", llm=llm) as client:
        detail = start(client)
        assert detail.status == "WAITING_APPROVAL", detail.error
        line = detail.quote.lines[0]
        assert line.product_id == "900702" and line.is_substitute
        assert line.substitute_for == "900701" and line.substitute_source == "valio"
        assert "SUBSTITUTION" in {rule.rule_id for rule in detail.quote.policy.rules_fired}
        assert any(codes == ["900702"] for _, codes, _ in client.app.state.valio.calls)
        assert any(step.type == "EXCEPTION" for step in detail.steps)


def test_agent_query_uses_fake_llm_then_gateway_search_and_rules(tmp_path: Path) -> None:
    llm = FakeLLM(
        tool_responses=purchase("900701"), structured_outputs=[{"query": "oats"}, EXPLANATION]
    )
    with environment(tmp_path, mode="agent", llm=llm) as client:
        detail = start(client)
        assert detail.status == "WAITING_APPROVAL", detail.error
        line = detail.quote.lines[0]
        assert (line.product_id, line.substitute_source, line.substitute_for) == (
            "900702",
            "agent",
            "900701",
        )
        assert line.is_substitute and line.quantity_sales_units == 3
        assert sum(step.type == "SUBSTITUTE_PROPOSED" for step in detail.steps) == 1
        assert (
            len(
                [call for call in llm.calls if call.config.response_mime_type == "application/json"]
            )
            == 2
        )


@pytest.mark.parametrize("gemini", [False, True])
def test_required_preference_fact_is_named_in_explanation(tmp_path: Path, gemini: bool) -> None:
    llm = (
        FakeLLM(tool_responses=purchase("900701"), structured_outputs=[EXPLANATION])
        if gemini
        else None
    )
    with environment(tmp_path, mode="normal", llm=llm) as client:
        detail = start(client, preferences=[{"key": "finnish_origin", "level": "require"}])
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert "required finnish origin preference" in detail.quote.agent_explanation
        assert "PREFERENCE_REQUIRED_UNMET" in {
            rule.rule_id for rule in detail.quote.policy.rules_fired
        }


def test_without_llm_and_without_substitutes_shortage_stays_blocked(tmp_path: Path) -> None:
    with environment(tmp_path, mode="agent") as client:
        detail = start(client)
        assert detail.status == "BLOCKED", detail.error
        assert not detail.quote.lines[0].is_substitute


@pytest.mark.parametrize("timely", [False, True])
@pytest.mark.parametrize("gemini", [False, True])
def test_delivery_exception_has_real_policy_and_line_date(
    tmp_path: Path, timely: bool, gemini: bool
) -> None:
    turns = purchase("900701")
    turns[1] = [
        ToolRequest("check_offers", {"item_id": "it_1", "product_ids": ["900701", "900702"]})
    ]
    llm = FakeLLM(tool_responses=turns, structured_outputs=[EXPLANATION]) if gemini else None
    with environment(tmp_path, mode="normal", llm=llm) as client:
        valio = client.app.state.valio
        valio.stock[0] = stock("900701", scheduleLines=[schedule(100, availableDate="2026-11-23")])
        if not timely:
            valio.stock[1] = stock(
                "900702", scheduleLines=[schedule(100, availableDate="2026-11-24")]
            )
        detail = start(client)
        assert detail.status == ("ORDERED" if timely else "WAITING_APPROVAL"), detail.error
        assert detail.quote.lines[0].product_id == ("900702" if timely else "900701")
        rules = {rule.rule_id for rule in detail.quote.policy.rules_fired}
        assert ("LATE_DELIVERY" in rules) is not timely
        if not timely:
            assert detail.quote.lines[0].delivery_date.isoformat() == "2026-11-23"
            assert detail.quote.delivery_date.isoformat() == "2026-11-23"


def test_contract_version_and_quote_line_fields_are_exported() -> None:
    from scripts.export_openapi import build_openapi

    document = build_openapi()
    assert document["info"]["version"] == "0.6"
    line = document["components"]["schemas"]["QuoteLine"]
    assert line["properties"]["substitute_for"]["default"] is None
    assert line["properties"]["substitute_source"]["default"] is None
