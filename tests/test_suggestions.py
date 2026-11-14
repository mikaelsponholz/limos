"""Playful suggestions use invented products, fake Gemini and mocked Valio only."""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent.gateway_client import AGENT_ID as GEMINI
from agent.llm import FakeLLM
from agent.suggestions import propose_suggestions
from gateway.api.dependencies import Resources
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
from tests.test_expired_requote import approve
from tests.test_offers import NOW, FakeValio, price, product, stock


def idea(query="berries", replaces="it_1", kind="seasonal", **changes):
    return {
        "kind": kind,
        "title": f"Try invented {query}",
        "reason": "An optional idea for this menu.",
        "replaces_item_id": replaces,
        "query": query,
        **changes,
    }


@contextmanager
def environment(
    tmp_path, *, ideas=(), playful=True, approval=True, llm_enabled=True, reorder=False
):
    records = [
        product(
            code,
            names={"en": name},
            allergens={"data_available": True, "free_from": ["milk"]},
            **changes,
        )
        for code, name, changes in [
            ("900701", "Invented flour", {}),
            ("900702", "Invented berries", {}),
            ("900703", "Invented herbs", {}),
            ("900704", "Invented oats", {}),
            ("900705", "Invented unavailable fruit", {}),
            ("900706", "Invented missing price fruit", {}),
            ("900707", "Invented blocked fruit", {"sales_block": True}),
        ]
    ]
    records.append(
        product(
            "900708",
            names={"en": "Invented milk cream"},
            allergens={"data_available": True, "contains": ["milk"]},
        )
    )
    llm = (
        FakeLLM(
            tool_responses=[] if reorder else purchase(),
            structured_outputs=([] if reorder else [EXPLANATION]) + [{"suggestions": list(ideas)}],
        )
        if llm_enabled
        else None
    )
    profile = {
        "customer_id": "invented-chef",
        "name": "Invented Chef",
        "company": "Invented Bistro",
        "role": "Chef",
        "buyer_id": BUYER,
        "age_group": "under_35",
        "playful": playful,
        "preferences": [{"key": "organic", "level": "prefer"}],
        "exclude_allergens": [],
        "defaults": {"priority": "balanced", "typical_guests": None, "budget": None},
        "order_history": [
            {
                "order_ref": "invented-past",
                "date": "2026-10-01",
                "title": "Invented flour order",
                "event_type": "item_list",
                "guests": None,
                "lines": [
                    {
                        "product_id": "900701",
                        "name": "Invented flour",
                        "quantity_sales_units": 2.0,
                        "sales_unit": "PRK",
                        "quantity_text": "2 x 1 kg",
                        "unit_price": 3.35,
                        "line_total": 6.7,
                    }
                ],
                "total": 6.7,
                "source": "seeded",
                "simulated": True,
            }
        ],
        "simulated": True,
    }
    (tmp_path / "invented-chef.json").write_text(json.dumps(profile), encoding="utf-8")
    ids = count(1)
    app = create_app(
        Settings(
            agent_mode="tools", store="memory", gemini_api_key="", gemini_model="", valio_api_key=""
        ),
        catalog=Catalog(records),
        valio=FakeValio(
            [price(r.product_id, error=r.product_id == "900706") for r in records],
            [
                stock(r.product_id, **({"scheduleLines": []} if r.product_id == "900705" else {}))
                for r in records
            ],
        ),
        store=MemoryStore(),
        clock=lambda: NOW,
        id_generator=lambda: f"invented_{next(ids)}",
        mandates={
            BUYER: mandate(allowed_agents=(AGENT_ID, GEMINI), always_require_approval=approval)
        },
        customers_dir=tmp_path,
        customer_overlay_dir=tmp_path / "overlays",
        llm=llm,
    )
    with bind_client(TestClient(app), llm) as client:
        yield client, llm


def start(client, *, reorder=False, customer=True, **changes):
    body = {
        "customer_id": "invented-chef" if customer else None,
        "buyer_id": BUYER,
        "requested_date": "2026-11-20",
        **(
            {"reorder_of": "invented-past"}
            if reorder
            else {
                "items": [
                    {"description": "flour", "quantity": 2.0, "unit": "kg", "constraints": []}
                ]
            }
        ),
        **changes,
    }
    response = client.post("/requests", json=body)
    assert response.status_code == 202, response.text
    return poll(client, response.json()["request_id"])


def suggestion_calls(llm):
    return [
        call
        for call in llm.calls
        if call.config.response_json_schema
        and "suggestions" in call.config.response_json_schema.get("properties", {})
    ]


def accept(client, detail, index=0):
    return client.post(
        f"/requests/{detail.request_id}/suggestions/{detail.suggestions[index].suggestion_id}/accept"
    )


@pytest.mark.parametrize("reorder", [False, True])
def test_both_paths_propose_at_most_three_priced_ideas_outside_quote(tmp_path: Path, reorder):
    ideas = [idea(), idea("herbs", None, "holiday"), idea("oats", None, "trend"), idea("flour")]
    with environment(tmp_path, ideas=ideas, reorder=reorder) as (client, llm):
        detail = start(client, reorder=reorder)
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert len(detail.suggestions) == 3
        assert [s.suggestion_id for s in detail.suggestions] == [
            f"sg_{detail.request_id}_{n}" for n in range(1, 4)
        ]
        assert {s.offer.product_id for s in detail.suggestions} == {"900702", "900703", "900704"}
        assert all(s.status == "OFFERED" and s.offer.line_total > 0 for s in detail.suggestions)
        assert [line.product_id for line in detail.quote.lines] == ["900701"]
        assert detail.quote.total == 6.7 and detail.order is None
        assert detail.quote.content_hash == compute_content_hash(detail.quote)
        calls = suggestion_calls(llm)
        assert len(calls) == 1
        prompt = json.loads(calls[0].contents[0].parts[0].text.split("\nReturn JSON")[0])
        assert prompt == {
            "today": "2026-11-14",
            "delivery_date": "2026-11-20",
            "event_type": None,
            "items": [{"item_id": "it_1", "description": "Invented flour" if reorder else "flour"}],
            "preferences": [{"key": "organic", "level": "prefer"}],
        }
        proposed = [step for step in detail.steps if step.type == "SUGGESTIONS_PROPOSED"]
        assert len(proposed) == 1 and proposed[0].model == "fake"
        assert proposed[0].detail["suggestion_ids"] == [s.suggestion_id for s in detail.suggestions]
        assert all(len(codes) <= 5 for _, codes, _ in client.app.state.valio.calls)
        assert verify_chain(detail.steps)


@pytest.mark.parametrize("case", ["nonplayful", "auto", "no_customer", "blocked", "no_llm"])
def test_ineligible_requests_make_no_suggestion_call(tmp_path: Path, case):
    with environment(
        tmp_path,
        ideas=[idea()],
        playful=case != "nonplayful",
        approval=case != "auto",
        llm_enabled=case != "no_llm",
    ) as (client, llm):
        detail = start(
            client, customer=case != "no_customer", **({"budget": 1.0} if case == "blocked" else {})
        )
        assert detail.suggestions == []
        assert detail.status == (
            "ORDERED" if case == "auto" else "BLOCKED" if case == "blocked" else "WAITING_APPROVAL"
        ), detail.error
        if llm:
            assert suggestion_calls(llm) == []
            assert len(llm.structured_outputs) == 1
        if case == "auto":
            assert detail.order is not None and detail.order.total == 6.7


@pytest.mark.parametrize(
    "bad",
    [
        idea(replaces="unknown_item"),
        idea("milk cream"),
        idea("unavailable fruit"),
        idea("missing price fruit"),
        idea("blocked fruit"),
        idea("flour"),
        idea("no matching product"),
    ],
)
def test_invalid_unsafe_unusable_and_duplicate_ideas_are_dropped(tmp_path: Path, bad):
    with environment(tmp_path, ideas=[bad, idea(), idea()]) as (client, llm):
        detail = start(client, exclude_allergens=["milk"])
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert len(detail.suggestions) == 1
        assert detail.suggestions[0].offer.product_id == "900702"
        assert len(suggestion_calls(llm)) == 1 and verify_chain(detail.steps)


def test_required_dietary_constraint_filters_suggestion(tmp_path: Path):
    with environment(tmp_path, ideas=[idea("milk cream"), idea()]) as (client, _):
        detail = start(client, preferences=[{"key": "vegan", "level": "require"}])
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert [s.offer.product_id for s in detail.suggestions] == ["900702"]


def test_copy_is_trimmed_and_date_is_helsinki_local(tmp_path: Path):
    with environment(tmp_path, ideas=[idea(title="  " + "T" * 300, reason="  " + "R" * 900)]) as (
        client,
        llm,
    ):
        client.app.state.clock = lambda: datetime.fromisoformat("2026-11-13T23:30:00+00:00")
        detail = start(client)
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert len(detail.suggestions[0].title) == 160
        assert len(detail.suggestions[0].reason) == 500
        assert '"today": "2026-11-14"' in suggestion_calls(llm)[0].contents[0].parts[0].text


@pytest.mark.parametrize("reorder", [False, True])
def test_accept_swap_requotes_expires_old_approval_and_preserves_audit(tmp_path: Path, reorder):
    with environment(tmp_path, ideas=[idea(), idea("herbs", None)], reorder=reorder) as (
        client,
        llm,
    ):
        old = start(client, reorder=reorder)
        response = accept(client, old)
        assert response.status_code == 202, response.text
        assert response.json() == {"request_id": old.request_id, "status": "RUNNING"}
        new = poll(client, old.request_id)
        assert new.status == "WAITING_APPROVAL" and new.order is None, new.error
        assert [line.product_id for line in new.quote.lines] == ["900702"]
        assert new.quote.lines[0].quantity_sales_units == 2.0
        assert new.quote.content_hash != old.quote.content_hash
        assert new.quote.content_hash == compute_content_hash(new.quote)
        assert new.approval_id != old.approval_id
        assert client.get(f"/approvals/{new.approval_id}").json()["status"] == "PENDING"
        assert client.app.state.store.get_approval(old.approval_id, BUYER).status == "EXPIRED"
        assert (
            approve(client, old.approval_id, old.quote.model_dump(mode="json")).status_code == 410
        )
        assert [s.status for s in new.suggestions] == ["ACCEPTED", "OFFERED"]
        assert len(suggestion_calls(llm)) == 1
        assert sum(step.type == "SUGGESTION_ACCEPTED" for step in new.steps) == 1
        assert verify_chain(new.steps)
        repeated = accept(client, old)
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] == "SUGGESTION_NOT_OFFERED"


def test_auto_replacement_still_needs_new_approval(tmp_path: Path):
    with environment(tmp_path, ideas=[idea()]) as (client, _):
        old = start(client)
        client.app.state.mandates[BUYER] = mandate(allowed_agents=(AGENT_ID, GEMINI))
        assert accept(client, old).status_code == 202
        new = poll(client, old.request_id)
        assert new.status == "WAITING_APPROVAL" and new.order is None
        assert new.quote.policy.outcome == "AUTO" and new.quote.status == "PENDING_APPROVAL"
        denied = client.post(
            "/gateway/orders",
            json={
                "buyer_id": BUYER,
                "agent_id": new.quote.agent_id,
                "quote_id": new.quote.quote_id,
                "idempotency_key": "invented-bypass",
            },
        )
        assert denied.status_code == 403
        assert (
            approve(client, new.approval_id, new.quote.model_dump(mode="json")).status_code == 200
        )


def test_accept_addition_adds_matching_intent_item_and_runs_its_rules(tmp_path: Path):
    with environment(tmp_path, ideas=[idea("herbs", None)]) as (client, _):
        old = start(
            client, exclude_allergens=["milk"], preferences=[{"key": "vegan", "level": "require"}]
        )
        assert accept(client, old).status_code == 202
        new = poll(client, old.request_id)
        assert new.status == "WAITING_APPROVAL" and new.order is None, new.error
        assert {line.product_id for line in new.quote.lines} == {"900701", "900703"}
        added = next(
            item for item in new.intent.items if item.item_id == old.suggestions[0].offer.item_id
        )
        assert set(added.constraints) == {"vegan", "no_milk"}
        assert added.quantity == 1.0 and added.unit == "kg"
        rules = [rule for rule in new.quote.policy.rules_fired if rule.item_id == added.item_id]
        assert any(rule.rule_id == "DIETARY_UNKNOWN" for rule in rules)
        assert new.quote.total == 10.05 and verify_chain(new.steps)


def test_blocked_replacement_stays_blocked_and_never_orders(tmp_path: Path):
    with environment(tmp_path, ideas=[idea()]) as (client, _):
        old = start(client)
        valio = client.app.state.valio
        valio.stock = [stock(p.product_id, scheduleLines=[]) for p in valio.stock]
        assert accept(client, old).status_code == 202
        new = poll(client, old.request_id)
        assert new.status == "BLOCKED" and new.order is None
        assert new.quote.policy.outcome == "BLOCK" and new.approval_id is None
        assert new.suggestions[0].status == "ACCEPTED"
        assert len(client.app.state.store.list_approvals(BUYER)) == 1
        accepted = next(step for step in new.steps if step.type == "SUGGESTION_ACCEPTED")
        assert accepted.detail["outcome"] == "BLOCK" and accepted.detail["reasons"]
        assert verify_chain(new.steps)


@pytest.mark.parametrize("reject", [False, True])
def test_order_or_rejection_declines_unaccepted_suggestions(tmp_path: Path, reject):
    with environment(tmp_path, ideas=[idea(), idea("herbs", None)]) as (client, _):
        old = start(client)
        assert accept(client, old).status_code == 202
        new = poll(client, old.request_id)
        response = (
            client.post(
                f"/approvals/{new.approval_id}/reject",
                json={"approver": "Invented Approver", "reason": "Invented reason"},
            )
            if reject
            else approve(client, new.approval_id, new.quote.model_dump(mode="json"))
        )
        assert response.status_code == 200, response.text
        done = poll(client, new.request_id)
        assert done.status == ("REJECTED" if reject else "ORDERED")
        assert [s.status for s in done.suggestions] == ["ACCEPTED", "DECLINED"]
        response = accept(client, done, 1)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SUGGESTION_NOT_OFFERED"
        assert verify_chain(done.steps)


def test_not_found_and_suggestions_are_scoped_to_the_request(tmp_path: Path):
    with environment(tmp_path, ideas=[idea()]) as (client, _):
        detail = start(client)
        for path in (
            "/requests/unknown/suggestions/unknown/accept",
            f"/requests/{detail.request_id}/suggestions/unknown/accept",
        ):
            response = client.post(path)
            assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"
        env = Resources(Request({"type": "http", "app": client.app}))
        assert env.store.get_request(detail.request_id, "invented-other-buyer") is None


async def test_generation_does_not_overwrite_a_decision_during_offer_checks(
    tmp_path: Path, monkeypatch
):
    with environment(tmp_path, ideas=[idea()], llm_enabled=False) as (client, _):
        detail = start(client)
        client.app.state.llm = FakeLLM(structured_outputs=[{"suggestions": [idea()]}])
        env = Resources(Request({"type": "http", "app": client.app}))
        original = client.app.state.valio.price

        async def decide_during_price(*args, **kwargs):
            current = env.store.get_request(detail.request_id, BUYER)
            current.status = "REJECTED"
            env.store.save_request(current)
            await asyncio.sleep(0)
            return await original(*args, **kwargs)

        monkeypatch.setattr(client.app.state.valio, "price", decide_during_price)
        await propose_suggestions(detail.request_id, env)
        current = env.store.get_request(detail.request_id, BUYER)
        assert current.status == "REJECTED" and current.suggestions == []


def test_bad_offer_data_for_one_idea_keeps_the_waiting_quote(tmp_path: Path, monkeypatch):
    # Seen live: Valio returned an unsupported logistic model for a suggested product,
    # and the whole request turned FAILED after its list was already waiting for approval.
    from agent import suggestions as module
    from gateway.offers import OfferDataError

    original = module.tools.check_offers

    async def broken_for_berries(body, env):
        if "900702" in body.product_ids:
            raise OfferDataError("Missing or unsupported logistic model for 900702.")
        return await original(body, env)

    monkeypatch.setattr(module.tools, "check_offers", broken_for_berries)
    with environment(tmp_path, ideas=[idea(), idea("herbs")]) as (client, _):
        detail = start(client)
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert detail.error is None
        assert [s.offer.product_id for s in detail.suggestions] == ["900703"]
        assert verify_chain(detail.steps)


def test_failed_suggestion_step_keeps_the_waiting_quote(tmp_path: Path, monkeypatch):
    from gateway import runner

    async def crash(request_id, env):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(runner, "propose_suggestions", crash)
    with environment(tmp_path, ideas=[idea()]) as (client, _):
        detail = start(client)
        assert detail.status == "WAITING_APPROVAL", detail.error
        assert detail.approval_id is not None and detail.suggestions == []
        assert detail.steps[-1].type == "EXCEPTION"
        assert verify_chain(detail.steps)
