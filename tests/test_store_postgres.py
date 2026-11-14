"""The database itself enforces the trust rules, even for a client that skips the store."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from gateway.audit import append_step, get_audit
from gateway.orders import place_order
from gateway.quotes import approve, request_approval
from tests.engine_support import AGENT, BUYER, Engine, mandate, request_detail
from tests.test_offers import NOW

psycopg = pytest.importorskip("psycopg")
from gateway.store.postgres import PostgresStore  # noqa: E402


@pytest.fixture
def store(pg_schema: tuple[str, str]) -> Iterator[PostgresStore]:
    url, schema = pg_schema
    s = PostgresStore(url, schema=schema)
    yield s
    s.close()


@pytest.fixture
def raw(pg_schema: tuple[str, str]) -> Iterator[Any]:
    """A plain connection that bypasses PostgresStore, like a careless or hostile client."""
    url, schema = pg_schema
    with psycopg.connect(url, autocommit=True, options=f"-c search_path={schema}") as conn:
        yield conn


async def test_decision_log_cannot_be_edited_deleted_or_truncated(
    store: PostgresStore, raw: Any
) -> None:
    quote = await Engine(store).quote()
    for statement in (
        "UPDATE limos_steps SET payload = '{}'::jsonb",
        "DELETE FROM limos_steps",
        "TRUNCATE limos_steps",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            raw.execute(statement)
    assert get_audit(store, quote.request_id, BUYER).chain_valid


async def test_database_rejects_a_step_that_does_not_link(store: PostgresStore, raw: Any) -> None:
    quote = await Engine(store).quote()
    with pytest.raises(psycopg.errors.CheckViolation, match="chain conflict"):
        raw.execute(
            "INSERT INTO limos_steps (request_id, seq, prev_hash, hash, payload) "
            "VALUES (%s, 99, 'forged', 'forged-hash', '{}'::jsonb)",
            (quote.request_id,),
        )


async def test_orders_are_immutable_and_idempotent(store: PostgresStore, raw: Any) -> None:
    engine = Engine(store)
    quote = await engine.quote()
    args = (quote.quote_id, BUYER, AGENT, "invented-key", NOW)
    first = place_order(*args, store=store, mandate=mandate(), id_generator=engine.next_id)
    again = place_order(*args, store=store, mandate=mandate(), id_generator=engine.next_id)
    assert again == first
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw.execute("UPDATE limos_orders SET payload = '{}'::jsonb")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw.execute("DELETE FROM limos_orders")


async def test_decided_approval_stays_decided(store: PostgresStore, raw: Any) -> None:
    engine = Engine(store)
    quote = await engine.quote(mandate=mandate(auto_limit=5.0))
    approval = request_approval(
        quote.quote_id,
        BUYER,
        AGENT,
        "Check spend",
        store=store,
        now=NOW,
        id_generator=engine.next_id,
    )
    approve(
        approval.approval_id,
        "Invented Approver",
        quote.quote_id,
        quote.content_hash,
        "Checked",
        NOW,
        store=store,
        buyer_id=BUYER,
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="already APPROVED"):
        raw.execute(
            "UPDATE limos_approvals "
            "SET payload = jsonb_set(payload, '{decided_by}', '\"Someone Else\"')"
        )


def test_two_gateway_processes_share_one_valid_chain(pg_schema: tuple[str, str]) -> None:
    """Two store instances stand in for two gateway processes writing the same log."""
    url, schema = pg_schema
    a, b = PostgresStore(url, schema=schema), PostgresStore(url, schema=schema)
    try:
        request = request_detail()
        a.save_request(request)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(
                pool.map(
                    lambda i: append_step(
                        (a, b)[i % 2],
                        request.request_id,
                        "gateway",
                        "TOOL_CALL",
                        "Checked invented products",
                        {"i": i},
                        None,
                        NOW,
                    ),
                    range(12),
                )
            )
        audit = get_audit(b, request.request_id, BUYER)
        assert audit.chain_valid and [s.seq for s in audit.steps] == list(range(1, 13))
    finally:
        a.close()
        b.close()
