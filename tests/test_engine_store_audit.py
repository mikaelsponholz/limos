"""Both stores preserve typed records, ownership, transactions and audit chains."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gateway.audit import append_step, get_audit, verify_chain
from gateway.errors import GatewayError
from gateway.orders import place_order
from gateway.quotes import approve, request_approval
from gateway.schemas import PastOrder, PastOrderLine
from gateway.settings import Settings
from gateway.store import FileStore, MemoryStore, Store, create_store
from tests.engine_support import AGENT, BUYER, Engine, mandate, request_detail
from tests.test_offers import NOW, REQUESTED

KINDS = ["memory", "file", "postgres"]


@pytest.fixture
def fx(request: pytest.FixtureRequest) -> pytest.FixtureRequest:
    """Pytest's request under another name; some tests use `request` for a RequestDetail."""
    return request


def make_store(kind: str, path: Path, request: pytest.FixtureRequest) -> Store:
    """Memory and file as before; postgres in a throwaway schema when DATABASE_URL is set."""
    if kind != "postgres":
        return create_store(Settings(store=kind), path=path)
    from gateway.store.postgres import PostgresStore

    url, schema = request.getfixturevalue("pg_schema")
    store = PostgresStore(url, schema=schema)
    request.addfinalizer(store.close)
    return store


def reopen(kind: str, store: Store, path: Path, request: pytest.FixtureRequest) -> Store:
    """A second store over the same data, as after a restart."""
    if kind == "file":
        return FileStore(path)
    if kind == "postgres":
        from gateway.store.postgres import PostgresStore

        url, schema = request.getfixturevalue("pg_schema")
        again = PostgresStore(url, schema=schema)
        request.addfinalizer(again.close)
        return again
    return store


@pytest.mark.parametrize("kind", KINDS)
async def test_store_round_trip_and_buyer_isolation(
    kind: str, tmp_path: Path, fx: pytest.FixtureRequest
) -> None:
    path = tmp_path / "invented-store.json"
    store = make_store(kind, path, fx)
    engine = Engine(store)
    request = request_detail()
    store.save_request(request)
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
    decided = approve(
        approval.approval_id,
        "Invented Approver",
        quote.quote_id,
        quote.content_hash,
        "Checked",
        NOW,
        store=store,
        buyer_id=BUYER,
    )
    placed = place_order(
        quote.quote_id,
        BUYER,
        AGENT,
        "invented-key",
        NOW,
        store=store,
        mandate=mandate(),
        id_generator=engine.next_id,
    )
    past = PastOrder(
        order_ref=placed.order_id,
        date=REQUESTED,
        title="Invented pantry purchase",
        event_type="staples",
        guests=None,
        lines=[
            PastOrderLine(
                product_id="900101",
                name="Invented pantry flour",
                quantity_sales_units=3.0,
                sales_unit="PRK",
                quantity_text="3 x 1 kg",
                unit_price=3.35,
                line_total=10.05,
            )
        ],
        total=placed.total,
        source="limos",
        simulated=True,
    )
    store.append_past_order("invented-customer", BUYER, past)
    before = get_audit(store, request.request_id, BUYER)
    assert before.chain_valid
    store = reopen(kind, store, path, fx)
    assert store.get_request(request.request_id, BUYER) == request
    assert store.list_requests(BUYER) == [request]
    stored_quote = store.get_quote(quote.quote_id, BUYER)
    assert stored_quote is not None and stored_quote.status == "ORDERED"
    assert store.list_quotes(request.request_id, BUYER) == [stored_quote]
    assert store.get_approval(approval.approval_id, BUYER) == decided
    assert store.list_approvals(BUYER) == [decided]
    assert store.get_order(placed.order_id, BUYER) == placed
    assert store.order_for_request(request.request_id, BUYER) == placed
    assert store.get_idempotent_order("invented-key", BUYER) == placed
    assert store.get_past_orders("invented-customer", BUYER) == [past]
    assert get_audit(store, request.request_id, BUYER) == before
    assert store.get_request(request.request_id, "other") is None
    assert store.list_requests("other") == []
    assert store.get_quote(quote.quote_id, "other") is None
    assert store.list_quotes(request.request_id, "other") == []
    assert store.get_approval(approval.approval_id, "other") is None
    assert store.list_approvals("other") == []
    assert store.get_order(placed.order_id, "other") is None
    assert store.order_for_request(request.request_id, "other") is None
    assert store.get_idempotent_order("invented-key", "other") is None
    assert store.get_steps(request.request_id, "other") == []
    assert store.get_past_orders("invented-customer", "other") == []
    with pytest.raises(GatewayError) as error:
        get_audit(store, request.request_id, "other")
    assert (error.value.status_code, error.value.code) == (404, "NOT_FOUND")
    assert not hasattr(store, "update_step") and not hasattr(store, "delete_step")


@pytest.mark.parametrize("kind", KINDS)
async def test_transaction_rolls_back_all_mutations(
    kind: str, tmp_path: Path, fx: pytest.FixtureRequest
) -> None:
    path = tmp_path / "invented-store.json"
    store = make_store(kind, path, fx)
    engine = Engine(store)
    quote = await engine.quote()
    before = get_audit(store, quote.request_id, BUYER)
    with pytest.raises(RuntimeError, match="Invented failure"):
        with store.transaction():
            changed = quote.model_copy(update={"status": "REJECTED"}, deep=True)
            store.save_quote(changed)
            append_step(store, quote.request_id, "human", "REJECTED", "Rejected", {}, None, NOW)
            raise RuntimeError("Invented failure")
    store = reopen(kind, store, path, fx)
    assert store.get_quote(quote.quote_id, BUYER) == quote
    assert get_audit(store, quote.request_id, BUYER) == before


async def test_file_write_failure_rolls_back_memory_and_preserves_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invented-store.json"
    store = FileStore(path)
    engine = Engine(store)
    quote = await engine.quote()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("Invented disk failure")

    monkeypatch.setattr("gateway.store.file.os.replace", fail_replace)
    with pytest.raises(OSError, match="Invented disk failure"):
        place_order(
            quote.quote_id,
            BUYER,
            AGENT,
            "invented-key",
            NOW,
            store=store,
            mandate=mandate(),
            id_generator=engine.next_id,
        )
    assert store.get_quote(quote.quote_id, BUYER) == quote
    assert FileStore(path).get_quote(quote.quote_id, BUYER) == quote
    assert store.get_idempotent_order("invented-key", BUYER) is None
    assert len(list(tmp_path.iterdir())) == 1


def test_factory_selection_and_clear_unavailable_errors(tmp_path: Path) -> None:
    assert isinstance(create_store(Settings(store="memory")), MemoryStore)
    assert isinstance(create_store(Settings(store="file"), path=tmp_path / "store.json"), FileStore)
    with pytest.raises(ValueError, match="needs DATABASE_URL"):
        create_store(Settings(store="postgres", database_url=""))
    with pytest.raises(ValueError, match="Unknown store"):
        create_store(Settings(store="invalid"))


async def test_audit_exact_hash_links_and_tamper_detection(tmp_path: Path) -> None:
    path = tmp_path / "invented-store.json"
    store = FileStore(path)
    quote = await Engine(store).quote()
    steps = store.get_steps(quote.request_id, BUYER)
    assert verify_chain(steps) and verify_chain([])
    assert steps[0].seq == 1 and steps[0].prev_hash is None
    assert steps[1].prev_hash == steps[0].hash
    assert not verify_chain(list(reversed(steps)))
    changed = [s.model_copy(deep=True) for s in steps]
    changed[0].detail["total"] = 999.0
    assert not verify_chain(changed)
    changed = [s.model_copy(deep=True) for s in steps]
    changed[1].prev_hash = None
    assert not verify_chain(changed)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["steps"][quote.request_id][0]["title"] = "Edited stored step"
    path.write_text(json.dumps(payload), encoding="utf-8")
    reloaded = FileStore(path)
    assert not verify_chain(reloaded.get_steps(quote.request_id, BUYER))
    assert get_audit(reloaded, quote.request_id, BUYER).chain_valid is False


@pytest.mark.parametrize("kind", ["memory", "postgres"])
def test_append_step_on_stored_request_and_concurrent_sequence(
    kind: str, tmp_path: Path, fx: pytest.FixtureRequest
) -> None:
    store = make_store(kind, tmp_path / "invented-store.json", fx)
    request = request_detail()
    store.save_request(request)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda _: append_step(
                    store,
                    request.request_id,
                    "gateway",
                    "TOOL_CALL",
                    "Checked invented products",
                    {"count": 1},
                    None,
                    NOW,
                ),
                range(20),
            )
        )
    audit = get_audit(store, request.request_id, BUYER)
    assert audit.chain_valid and [s.seq for s in audit.steps] == list(range(1, 21))
    changed = store.get_steps(request.request_id, BUYER)
    changed[0].title = "Changed caller snapshot"
    assert get_audit(store, request.request_id, BUYER) == audit
    with pytest.raises(ValueError, match="extend"):
        store.append_step(request.request_id, BUYER, audit.steps[0])
