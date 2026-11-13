"""Shared Store tests: the same suite runs against memory, file and Postgres.

Postgres runs only when DATABASE_URL is set (env or the repo .env). It uses a fresh
random schema per run and drops it afterwards, so demo data is never touched.
"""
import copy
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from gateway.store import ChainConflict, FileStore, MemoryStore, NotFound, OrderConflict

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "api"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def database_url() -> str:
    """DATABASE_URL from env, else from the gitignored repo .env. Never printed."""
    url = os.environ.get("DATABASE_URL", "")
    env = ROOT / ".env"
    if not url and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"')
    return url


@pytest.fixture(scope="module")
def pg_store():
    url = database_url()
    if not url:
        pytest.skip("DATABASE_URL not set")
    psycopg = pytest.importorskip("psycopg")
    from gateway.store.postgres import PostgresStore

    schema = f"limos_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = PostgresStore(url, schema=schema, max_size=1)  # one connection, Aiven allows 15
    try:
        store.migrate()
        yield store
    finally:
        store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def uniq(obj: dict, **ids: str) -> dict:
    """Postgres shares one schema per module, so each test uses unique ids."""
    out = copy.deepcopy(obj)
    out.update(ids)
    return out


@pytest.fixture(params=["memory", "file", "postgres"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "file":
        yield FileStore(str(tmp_path / "store.json"))
    else:
        yield request.getfixturevalue("pg_store")


@pytest.fixture
def sfx() -> str:
    return uuid.uuid4().hex[:8]


def saved_quote(store, sfx: str, n: str = "a") -> dict:
    q = load("quote.json")
    q = uniq(q, quote_id=f"q_{n}_{sfx}", request_id=f"r_{sfx}",
             content_hash=f"sha256:{n}{sfx}")
    store.save_quote(q)
    return q


# Round trips ----------------------------------------------------------------

def test_request_round_trip(store, sfx):
    r = uniq(load("request.json"), request_id=f"r_{sfx}", buyer_id=f"b_{sfx}")
    store.save_request(r)
    assert store.get_request(r["request_id"]) == r
    store.update_request_status(r["request_id"], "DONE")
    assert store.get_request(r["request_id"])["status"] == "DONE"
    assert [x["request_id"] for x in store.list_requests(f"b_{sfx}")] == [r["request_id"]]
    assert store.get_request("nope") is None


def test_quote_round_trip(store, sfx):
    q = saved_quote(store, sfx)
    assert store.get_quote(q["quote_id"]) == q
    store.update_quote_status(q["quote_id"], "APPROVED")
    assert store.get_quote(q["quote_id"])["status"] == "APPROVED"
    with pytest.raises(NotFound):
        store.update_quote_status("nope", "APPROVED")


def test_order_round_trip(store, sfx):
    q = saved_quote(store, sfx)
    o = uniq(load("order.json"), order_id=f"o_{sfx}", quote_id=q["quote_id"],
             request_id=f"r_{sfx}", idempotency_key=f"k_{sfx}")
    assert store.create_order(o) == o
    assert store.get_order(o["order_id"]) == o


# Approvals ------------------------------------------------------------------

def make_approval(store, sfx: str) -> dict:
    q = saved_quote(store, sfx)
    a = load("approval-detail.json")
    a["approval_id"] = f"a_{sfx}"
    a["quote"] = q
    a["options"] = [q]
    return store.create_approval(a)


def test_approval_round_trip(store, sfx):
    a = make_approval(store, sfx)
    got = store.get_approval(a["approval_id"])
    assert got["status"] == "PENDING"
    assert got["quote"]["quote_id"] == f"q_a_{sfx}"
    assert a["approval_id"] in [x["approval_id"] for x in store.list_approvals("PENDING", limit=500)]


def test_approval_needs_saved_quote(store, sfx):
    a = load("approval-detail.json")
    a["approval_id"] = f"a_{sfx}"
    a["quote"] = uniq(a["quote"], quote_id=f"missing_{sfx}")
    with pytest.raises(NotFound):
        store.create_approval(a)


def test_approval_decided_only_once(store, sfx):
    a = make_approval(store, sfx)
    q = store.get_quote(f"q_a_{sfx}")
    ok = store.decide_approval(a["approval_id"], "APPROVED", "Anna Example",
                               q["quote_id"], q["content_hash"], "fine")
    assert ok is True
    # A second click, or a reject racing the approve, changes nothing.
    assert store.decide_approval(a["approval_id"], "REJECTED", "Someone else") is False
    got = store.get_approval(a["approval_id"])
    assert got["status"] == "APPROVED"
    assert got["decided_by"] == "Anna Example"
    assert got["approved_hash"] == q["content_hash"]
    assert got["decided_at"]


def test_approve_requires_hash(store, sfx):
    a = make_approval(store, sfx)
    with pytest.raises(ValueError):
        store.decide_approval(a["approval_id"], "APPROVED", "Anna Example")


# Orders ---------------------------------------------------------------------

def test_order_is_idempotent(store, sfx):
    q = saved_quote(store, sfx)
    o = uniq(load("order.json"), order_id=f"o_{sfx}", quote_id=q["quote_id"],
             request_id=f"r_{sfx}", idempotency_key=f"k_{sfx}")
    first = store.create_order(o)
    retry = store.create_order(uniq(o, order_id=f"o2_{sfx}"))  # same key, new id
    assert retry == first
    assert store.get_order(f"o2_{sfx}") is None


def test_one_order_per_request(store, sfx):
    q = saved_quote(store, sfx)
    o = uniq(load("order.json"), order_id=f"o_{sfx}", quote_id=q["quote_id"],
             request_id=f"r_{sfx}", idempotency_key=f"k_{sfx}")
    store.create_order(o)
    with pytest.raises(OrderConflict):
        store.create_order(uniq(o, order_id=f"o2_{sfx}", idempotency_key=f"k2_{sfx}"))


# Decision log ---------------------------------------------------------------

def test_fixture_chain_appends_and_verifies(store, sfx):
    rid = f"r_{sfx}"
    steps = load("steps.json")
    for s in steps:
        s = uniq(s, hash=f"{s['hash']}{sfx}",
                 prev_hash=None if s["prev_hash"] is None else f"{s['prev_hash']}{sfx}")
        store.append_step(rid, s)
    got = store.get_steps(rid)
    assert [s["seq"] for s in got] == list(range(1, len(steps) + 1))
    assert store.verify_chain(rid) is True


def chain(store, sfx: str, n: int = 2) -> list[dict]:
    rid = f"r_{sfx}"
    out, prev = [], None
    for i in range(1, n + 1):
        s = {"seq": i, "at": "2026-11-14T09:12:02+02:00", "actor": "gateway", "type": "TOOL_CALL",
             "title": f"step {i}", "detail": {}, "model": None,
             "prev_hash": prev, "hash": f"sha256:{sfx}{i}"}
        store.append_step(rid, s)
        out.append(s)
        prev = s["hash"]
    return out


def test_broken_chain_rejected(store, sfx):
    rid = f"r_{sfx}"
    steps = chain(store, sfx)
    bad_prev = {**steps[-1], "seq": 3, "prev_hash": "sha256:forged", "hash": f"sha256:{sfx}x"}
    with pytest.raises(ChainConflict):
        store.append_step(rid, bad_prev)
    gap = {**steps[-1], "seq": 5, "prev_hash": steps[-1]["hash"], "hash": f"sha256:{sfx}y"}
    with pytest.raises(ChainConflict):
        store.append_step(rid, gap)
    with pytest.raises(ChainConflict):  # a second "first step" for the same request
        store.append_step(rid, {**steps[0], "hash": f"sha256:{sfx}z"})
    assert len(store.get_steps(rid)) == 2
    assert store.verify_chain(rid) is True


def test_verify_chain_with_recompute(store, sfx):
    rid = f"r_{sfx}"
    chain(store, sfx)
    assert store.verify_chain(rid, recompute=lambda s: s["hash"]) is True
    assert store.verify_chain(rid, recompute=lambda s: "sha256:other") is False


def test_returned_steps_are_copies(store, sfx):
    rid = f"r_{sfx}"
    chain(store, sfx)
    got = store.get_steps(rid)
    got[0]["title"] = "edited"
    assert store.get_steps(rid)[0]["title"] == "step 1"


def test_audit_log_is_append_only_in_db(pg_store, sfx):
    import psycopg

    rid = f"r_{sfx}"
    chain(pg_store, sfx)
    for stmt in (
        "UPDATE audit_steps SET actor = 'attacker' WHERE request_id = %s",
        "DELETE FROM audit_steps WHERE request_id = %s",
    ):
        with pg_store.pool.connection() as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt, (rid,))
    with pg_store.pool.connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE audit_steps")
    assert [s["actor"] for s in pg_store.get_steps(rid)] == ["gateway", "gateway"]


def test_db_rejects_unlinked_insert_even_bypassing_code(pg_store, sfx):
    """The trigger guards the chain even if someone skips append_step and writes SQL."""
    import psycopg
    from psycopg.types.json import Jsonb

    rid = f"r_{sfx}"
    chain(pg_store, sfx)
    with pg_store.pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO audit_steps (request_id, seq, at, actor, type, prev_hash, hash, payload)
                   VALUES (%s, 3, now(), 'x', 'x', 'sha256:forged', %s, %s)""",
                (rid, f"sha256:{sfx}evil", Jsonb({})),
            )
