"""Price locks, approval state transitions and exact quote hash coverage."""

import hashlib
import json
from datetime import date, timedelta

import pytest

from gateway.errors import GatewayError
from gateway.quotes import (
    approve,
    compute_content_hash,
    get_quote,
    reject,
    request_approval,
)
from gateway.schemas import Quantity
from tests.engine_support import AGENT, BUYER, Engine, mandate
from tests.test_offers import NOW, REQUESTED, offer, product


async def test_hash_canonical_stable_and_covers_only_locked_fields() -> None:
    engine = Engine()
    quote = await engine.quote()
    payload = quote.model_dump(
        mode="json",
        include={
            "lines",
            "total",
            "total_with_tax",
            "delivery_date",
            "expires_at",
        },
    )
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert quote.content_hash == expected == compute_content_hash(quote)
    assert (await engine.quote(agent_explanation="Different explanation")).content_hash == expected
    quote.status = "APPROVED"
    quote.agent_explanation = "A factual explanation"
    assert compute_content_hash(quote) == expected
    for field, value in [
        ("total", quote.total + 0.01),
        ("total_with_tax", quote.total_with_tax + 0.01),
        ("delivery_date", REQUESTED + timedelta(days=1)),
        ("expires_at", quote.expires_at + timedelta(seconds=1)),
    ]:
        changed = quote.model_copy(update={field: value}, deep=True)
        assert compute_content_hash(changed) != expected
    quote.lines[0].quantity_text = "Changed line"
    assert compute_content_hash(quote) != expected


async def test_totals_dates_carbon_and_price_lock() -> None:
    first = await offer(record=product(carbon_kg_co2e_per_kg=2.1))
    second = first.model_copy(deep=True)
    second.item_id = "it_second"
    second.delivery_date = date(2026, 11, 23)
    second.order_deadline = NOW + timedelta(minutes=12)
    second.quantity_covered = Quantity(value=3000.0, unit="g")
    second.quantity_requested = Quantity(value=3000.0, unit="g")
    first.line_total, second.line_total = 0.1, 0.2
    first.line_total_with_tax, second.line_total_with_tax = 0.12, 0.24
    from gateway.policy import ItemContext

    engine = Engine()
    quote = await engine.quote(
        lines=[first, second],
        item_context={
            "it_flour": ItemContext(),
            "it_second": ItemContext(),
        },
        budget=1.0,
    )
    assert quote.total == 0.3 and quote.total_with_tax == 0.36
    assert quote.delivery_date == second.delivery_date
    assert quote.order_deadline == second.order_deadline
    assert quote.co2e_total_kg == pytest.approx(12.6)
    assert quote.budget is not None and quote.budget.status == "within"
    first.line_total = 900.0
    quote.lines[0].line_total = 999.0
    locked = get_quote(engine.store, quote.quote_id, BUYER, NOW)
    assert locked.lines[0].line_total == 0.1
    assert locked.total == 0.3


@pytest.mark.parametrize("minutes,expected", [(10, 10), (60, 30), (-5, 30), (0, 30)])
async def test_expiry_caps_only_future_closing_times(minutes: int, expected: int) -> None:
    line = await offer()
    line.closing_time = NOW + timedelta(minutes=minutes)
    other = line.model_copy(deep=True)
    other.item_id = "it_other"
    other.closing_time = NOW + timedelta(minutes=20)
    from gateway.policy import ItemContext

    engine = Engine()
    quote = await engine.quote(lines=[line])
    assert quote.expires_at == NOW + timedelta(minutes=expected)
    multi = await engine.quote(
        lines=[line, other],
        item_context={
            "it_flour": ItemContext(),
            "it_other": ItemContext(),
        },
    )
    assert multi.expires_at == NOW + timedelta(minutes=min(expected, 20))


async def test_empty_quote_fallback_dates_and_no_carbon() -> None:
    quote = await Engine().quote(lines=[])
    assert quote.delivery_date == REQUESTED and quote.order_deadline is None
    assert quote.total == quote.total_with_tax == 0
    assert quote.co2e_total_kg is None
    assert quote.budget is None
    assert quote.agent_explanation == ""
    assert quote.expires_at == NOW + timedelta(minutes=30)
    line = await offer(record=product(carbon_kg_co2e_per_kg=2.0))
    line.quantity_covered = line.quantity_requested = Quantity(value=3.0, unit="L")
    assert (await Engine().quote(lines=[line])).co2e_total_kg is None


@pytest.mark.parametrize(
    "limits,status",
    [
        ({}, "AUTO_APPROVED"),
        ({"auto_limit": 5.0}, "PENDING_APPROVAL"),
        ({"auto_limit": 5.0, "hard_cap": 6.0}, "BLOCKED"),
    ],
)
async def test_status_mapping_and_expired_reads(limits: dict[str, float], status: str) -> None:
    engine = Engine()
    quote = await engine.quote(mandate=mandate(**limits))
    assert quote.status == status
    assert get_quote(engine.store, quote.quote_id, BUYER, NOW).status == status
    assert get_quote(engine.store, quote.quote_id, BUYER, quote.expires_at).status == "EXPIRED"
    assert engine.store.get_quote(quote.quote_id, BUYER) == quote


async def test_approval_binds_hash_records_decision_and_prevents_second_decision() -> None:
    engine = Engine()
    quote = await engine.quote(mandate=mandate(auto_limit=5.0))
    approval = request_approval(
        quote.quote_id,
        BUYER,
        AGENT,
        "Check spend",
        store=engine.store,
        now=NOW,
        id_generator=engine.next_id,
    )
    with pytest.raises(GatewayError) as error:
        approve(
            approval.approval_id,
            "Invented Approver",
            quote.quote_id,
            "sha256:wrong",
            "OK",
            NOW,
            store=engine.store,
            buyer_id=BUYER,
        )
    assert (error.value.status_code, error.value.code) == (409, "HASH_MISMATCH")
    result = approve(
        approval.approval_id,
        "Invented Approver",
        quote.quote_id,
        quote.content_hash,
        "Checked",
        NOW,
        store=engine.store,
        buyer_id=BUYER,
    )
    assert result.status == "APPROVED" and result.approved_hash == quote.content_hash
    assert result.decided_by == "Invented Approver" and result.decided_at == NOW
    assert result.comment == "Checked"
    assert get_quote(engine.store, quote.quote_id, BUYER, NOW).status == "APPROVED"
    for decide in [
        lambda: approve(
            approval.approval_id,
            "Other",
            quote.quote_id,
            quote.content_hash,
            "",
            NOW,
            store=engine.store,
            buyer_id=BUYER,
        ),
        lambda: reject(
            approval.approval_id, "Other", "No", NOW, store=engine.store, buyer_id=BUYER
        ),
    ]:
        with pytest.raises(GatewayError) as error:
            decide()
        assert (error.value.status_code, error.value.code) == (409, "ALREADY_DECIDED")


async def test_expired_approval_and_request_fail_without_state_change() -> None:
    engine = Engine()
    quote = await engine.quote(mandate=mandate(auto_limit=5.0))
    approval = request_approval(
        quote.quote_id,
        BUYER,
        AGENT,
        "Check",
        store=engine.store,
        now=NOW,
        id_generator=engine.next_id,
    )
    for action in [
        lambda: approve(
            approval.approval_id,
            "Invented Approver",
            quote.quote_id,
            quote.content_hash,
            "",
            quote.expires_at,
            store=engine.store,
            buyer_id=BUYER,
        ),
        lambda: request_approval(
            quote.quote_id,
            BUYER,
            AGENT,
            "Check",
            store=engine.store,
            now=quote.expires_at,
            id_generator=engine.next_id,
        ),
    ]:
        with pytest.raises(GatewayError) as error:
            action()
        assert (error.value.status_code, error.value.code) == (410, "QUOTE_EXPIRED")
    stored = engine.store.get_approval(approval.approval_id, BUYER)
    assert stored is not None and stored.status == "PENDING"
    assert len(engine.store.get_steps(quote.request_id, BUYER)) == 3


async def test_options_selected_quote_and_reject() -> None:
    engine = Engine()
    quote = await engine.quote(mandate=mandate(auto_limit=5.0))
    option = await engine.quote(option_label="Invented alternative")
    approval = request_approval(
        quote.quote_id,
        BUYER,
        AGENT,
        "Check",
        [option.quote_id],
        store=engine.store,
        now=NOW,
        id_generator=engine.next_id,
    )
    assert approval.option_quote_ids == [quote.quote_id, option.quote_id]
    approved = approve(
        approval.approval_id,
        "Invented Approver",
        option.quote_id,
        option.content_hash,
        "Choose alternative",
        NOW,
        store=engine.store,
        buyer_id=BUYER,
    )
    assert approved.selected_quote_id == option.quote_id
    other = await engine.quote(mandate=mandate(auto_limit=5.0), request_id="invented-other")
    rejected = request_approval(
        other.quote_id,
        BUYER,
        AGENT,
        "Check",
        store=engine.store,
        now=NOW,
        id_generator=engine.next_id,
    )
    decision = reject(
        rejected.approval_id,
        "Invented Approver",
        "Too much",
        NOW,
        store=engine.store,
        buyer_id=BUYER,
    )
    assert decision.status == "REJECTED" and decision.comment == "Too much"
    assert get_quote(engine.store, other.quote_id, BUYER, NOW).status == "REJECTED"
    assert engine.store.get_steps(other.request_id, BUYER)[-1].type == "REJECTED"


async def test_option_validation_and_buyer_isolation() -> None:
    engine = Engine()
    quote = await engine.quote(mandate=mandate(auto_limit=5.0))
    options = [await engine.quote() for _ in range(3)]
    with pytest.raises(ValueError, match="three"):
        request_approval(
            quote.quote_id,
            BUYER,
            AGENT,
            "Check",
            [q.quote_id for q in options],
            store=engine.store,
            now=NOW,
            id_generator=engine.next_id,
        )
    approval = request_approval(
        quote.quote_id,
        BUYER,
        AGENT,
        "Check",
        store=engine.store,
        now=NOW,
        id_generator=engine.next_id,
    )
    for action in [
        lambda: get_quote(engine.store, quote.quote_id, "another-buyer", NOW),
        lambda: request_approval(
            quote.quote_id,
            "another-buyer",
            AGENT,
            "",
            store=engine.store,
            now=NOW,
            id_generator=engine.next_id,
        ),
        lambda: approve(
            approval.approval_id,
            "Invented Approver",
            options[0].quote_id,
            options[0].content_hash,
            "",
            NOW,
            store=engine.store,
            buyer_id=BUYER,
        ),
        lambda: approve(
            approval.approval_id,
            "Invented Approver",
            quote.quote_id,
            quote.content_hash,
            "",
            NOW,
            store=engine.store,
            buyer_id="another-buyer",
        ),
    ]:
        with pytest.raises(GatewayError) as error:
            action()
        assert (error.value.status_code, error.value.code) == (404, "NOT_FOUND")


async def test_request_approval_requires_pending_and_same_request_options() -> None:
    engine = Engine()
    auto = await engine.quote()
    blocked = await engine.quote(mandate=mandate(auto_limit=5.0, hard_cap=6.0))
    pending = await engine.quote(mandate=mandate(auto_limit=5.0))
    unrelated = await engine.quote(request_id="another-request")
    for quote, options, code in [
        (auto, [], "NEEDS_APPROVAL"),
        (blocked, [], "BLOCKED"),
        (pending, [unrelated.quote_id], "NOT_FOUND"),
    ]:
        with pytest.raises(GatewayError) as error:
            request_approval(
                quote.quote_id,
                BUYER,
                AGENT,
                "Check",
                options,
                store=engine.store,
                now=NOW,
                id_generator=engine.next_id,
            )
        assert error.value.code == code
