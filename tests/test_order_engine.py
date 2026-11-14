"""Simulated order authorization, retry safety and one order per event."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from gateway.audit import get_audit
from gateway.errors import GatewayError
from gateway.orders import place_order
from gateway.quotes import approve, compute_content_hash, get_quote, reject, request_approval
from tests.engine_support import AGENT, BUYER, Engine, mandate
from tests.test_offers import NOW


def order(engine: Engine, quote_id: str, key: str = "invented-key", **changes: object):
    params = {
        "store": engine.store,
        "mandate": mandate(),
        "id_generator": engine.next_id,
        "buyer_id": BUYER,
        "agent_id": AGENT,
        "now": NOW,
        **changes,
    }
    return place_order(quote_id=quote_id, idempotency_key=key, **params)


async def test_auto_order_price_lock_authority_and_retry() -> None:
    engine = Engine()
    quote = await engine.quote(budget=100.0)
    placed = order(engine, quote.quote_id)
    assert placed.simulated is True and placed.status == "CONFIRMED"
    assert placed.placed_by.type == "agent" and placed.placed_by.name == AGENT
    assert placed.approved_by is None
    assert "1500.00 EUR auto limit" in placed.authority
    assert "250.00 EUR" in placed.authority and "100.00 EUR budget" in placed.authority
    assert placed.lines == quote.lines and placed.total == quote.total
    assert placed.delivery_date == quote.delivery_date
    assert get_quote(engine.store, quote.quote_id, BUYER, quote.expires_at).status == "ORDERED"
    audit = get_audit(engine.store, quote.request_id, BUYER)
    assert audit.chain_valid and [s.type for s in audit.steps] == [
        "QUOTE_CREATED",
        "POLICY_DECISION",
        "ORDER_PLACED",
    ]
    assert order(engine, quote.quote_id, now=NOW + timedelta(days=1)) == placed
    assert get_audit(engine.store, quote.request_id, BUYER) == audit


async def test_pending_requires_approval_then_orders_with_matching_hash() -> None:
    engine = Engine()
    quote = await engine.quote(mandate=mandate(auto_limit=5.0))
    with pytest.raises(GatewayError) as error:
        order(engine, quote.quote_id)
    assert (error.value.status_code, error.value.code) == (403, "NEEDS_APPROVAL")
    approval = request_approval(
        quote.quote_id,
        BUYER,
        AGENT,
        "Check spend",
        store=engine.store,
        now=NOW,
        id_generator=engine.next_id,
    )
    approve(
        approval.approval_id,
        "Invented Approver",
        quote.quote_id,
        quote.content_hash,
        "OK",
        NOW,
        store=engine.store,
        buyer_id=BUYER,
    )
    placed = order(engine, quote.quote_id)
    assert placed.approved_by == "Invented Approver"
    assert placed.authority == (
        f"Invented Approver approved quote {quote.quote_id} and its content hash matched."
    )
    assert [s.actor for s in get_audit(engine.store, quote.request_id, BUYER).steps] == [
        "gateway",
        "policy",
        "agent",
        "human",
        "gateway",
    ]


async def test_selected_option_orders_and_original_still_requires_approval() -> None:
    engine = Engine()
    original = await engine.quote(mandate=mandate(auto_limit=5.0))
    option = await engine.quote(mandate=mandate(auto_limit=5.0), option_label="Alternative")
    approval = request_approval(
        original.quote_id,
        BUYER,
        AGENT,
        "Check",
        [option.quote_id],
        store=engine.store,
        now=NOW,
        id_generator=engine.next_id,
    )
    approve(
        approval.approval_id,
        "Invented Approver",
        option.quote_id,
        option.content_hash,
        "",
        NOW,
        store=engine.store,
        buyer_id=BUYER,
    )
    with pytest.raises(GatewayError) as error:
        order(engine, original.quote_id)
    assert error.value.code == "NEEDS_APPROVAL"
    assert order(engine, option.quote_id).approved_by == "Invented Approver"


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("blocked", 422, "BLOCKED"),
        ("expired", 410, "QUOTE_EXPIRED"),
        ("buyer", 404, "NOT_FOUND"),
        ("unknown", 404, "NOT_FOUND"),
        ("agent", 403, "AGENT_NOT_PERMITTED"),
        ("rejected", 403, "NEEDS_APPROVAL"),
    ],
)
async def test_order_refusals(case: str, status: int, code: str) -> None:
    engine = Engine()
    limits = mandate(auto_limit=5.0, hard_cap=6.0) if case == "blocked" else mandate()
    if case == "rejected":
        limits = mandate(auto_limit=5.0)
    quote = await engine.quote(mandate=limits)
    if case == "rejected":
        approval = request_approval(
            quote.quote_id,
            BUYER,
            AGENT,
            "Check",
            store=engine.store,
            now=NOW,
            id_generator=engine.next_id,
        )
        reject(
            approval.approval_id, "Invented Approver", "No", NOW, store=engine.store, buyer_id=BUYER
        )
    params: dict[str, object] = {}
    if case == "buyer":
        params["buyer_id"] = "another-buyer"
    if case == "agent":
        params["agent_id"] = "not-permitted"
    if case == "expired":
        params["now"] = quote.expires_at
    before = engine.store.get_steps(quote.request_id, BUYER)
    with pytest.raises(GatewayError) as error:
        order(engine, "missing" if case == "unknown" else quote.quote_id, **params)
    assert (error.value.status_code, error.value.code) == (status, code)
    assert engine.store.get_steps(quote.request_id, BUYER) == before


async def test_idempotency_conflict_and_second_order_for_request() -> None:
    engine = Engine()
    first, second = await engine.quote(), await engine.quote()
    order(engine, first.quote_id)
    for quote, key, expected in [
        (second, "invented-key", "IDEMPOTENCY_CONFLICT"),
        (second, "another-key", "ALREADY_ORDERED"),
        (first, "another-key", "ALREADY_ORDERED"),
    ]:
        with pytest.raises(GatewayError) as error:
            order(engine, quote.quote_id, key)
        assert (error.value.status_code, error.value.code) == (409, expected)


async def test_same_key_is_scoped_to_buyer() -> None:
    engine = Engine()
    first = await engine.quote()
    other = await engine.quote(
        buyer_id="other-invented-buyer",
        request_id="other-request",
        mandate=mandate(buyer_id="other-invented-buyer"),
    )
    original = order(engine, first.quote_id)
    second = order(
        engine,
        other.quote_id,
        buyer_id="other-invented-buyer",
        mandate=mandate(buyer_id="other-invented-buyer"),
    )
    assert original.order_id != second.order_id


@pytest.mark.parametrize("recompute", [False, True])
async def test_changed_quote_cannot_use_old_approval(recompute: bool) -> None:
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
    approve(
        approval.approval_id,
        "Invented Approver",
        quote.quote_id,
        quote.content_hash,
        "",
        NOW,
        store=engine.store,
        buyer_id=BUYER,
    )
    changed = get_quote(engine.store, quote.quote_id, BUYER, NOW)
    changed.total += 1
    if recompute:
        changed.content_hash = compute_content_hash(changed)
    engine.store.save_quote(changed)
    with pytest.raises(GatewayError) as error:
        order(engine, quote.quote_id)
    assert (error.value.status_code, error.value.code) == (409, "HASH_MISMATCH")


async def test_concurrent_retries_produce_one_order_and_one_log_step() -> None:
    engine = Engine()
    quote = await engine.quote()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: order(engine, quote.quote_id), range(16)))
    assert len({result.order_id for result in results}) == 1
    assert [s.type for s in engine.store.get_steps(quote.request_id, BUYER)].count(
        "ORDER_PLACED"
    ) == 1


async def test_no_request_id_does_not_create_audit_or_share_event_limit() -> None:
    engine = Engine()
    first, second = await engine.quote(request_id=None), await engine.quote(request_id=None)
    assert order(engine, first.quote_id).request_id == ""
    assert order(engine, second.quote_id, "another-key").request_id == ""
    assert engine.store.get_steps("", BUYER) == []
