"""Migration and approval guarantees checked through a raw database connection."""

from collections.abc import Iterator
from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg")
from gateway.store.postgres import PostgresStore  # noqa: E402


@pytest.fixture
def raw(pg_schema: tuple[str, str]) -> Iterator[Any]:
    url, schema = pg_schema
    with psycopg.connect(url, autocommit=True, options=f"-c search_path={schema}") as conn:
        yield conn


def test_explicit_migrations_apply_both_files_idempotently(
    pg_schema: tuple[str, str], raw: Any
) -> None:
    url, schema = pg_schema
    store = PostgresStore(url, schema=schema, migrate=False)
    try:
        assert (
            raw.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
            ).fetchall()
            == []
        )
        for _ in range(2):
            assert store.migrate() == ["001_init.sql", "002_approval_guards.sql"]
            assert raw.execute("SELECT to_regclass('limos_approvals')").fetchone()[0]
            assert raw.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'limos_approvals'::regclass "
                "AND NOT tgisinternal ORDER BY tgname"
            ).fetchall() == [
                ("limos_approvals_decided_once",),
                ("limos_approvals_no_decided_delete",),
                ("limos_approvals_no_truncate",),
            ]
            raw.execute(
                "INSERT INTO limos_approvals (approval_id, buyer_id, status, payload) "
                "VALUES ('invented-decided', 'invented-buyer', 'APPROVED', '{}'::jsonb) "
                "ON CONFLICT DO NOTHING"
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="already APPROVED"):
                raw.execute("UPDATE limos_approvals SET status = 'PENDING'")
    finally:
        store.close()


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED", "EXPIRED"])
def test_decided_approvals_cannot_change_or_be_removed(
    pg_schema: tuple[str, str], raw: Any, status: str
) -> None:
    url, schema = pg_schema
    store = PostgresStore(url, schema=schema)
    try:
        raw.execute(
            "INSERT INTO limos_approvals (approval_id, buyer_id, status, payload) "
            "VALUES ('invented-decided', 'invented-buyer', %s, '{}'::jsonb)",
            (status,),
        )
        for statement in (
            "UPDATE limos_approvals SET status = 'PENDING'",
            "UPDATE limos_approvals SET payload = '{\"changed\":true}'::jsonb",
            "DELETE FROM limos_approvals",
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match=f"already {status}"):
                raw.execute(statement)
        raw.execute("UPDATE limos_approvals SET status = status, payload = payload")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            raw.execute("TRUNCATE limos_approvals")
        assert raw.execute("SELECT status FROM limos_approvals").fetchall() == [(status,)]
    finally:
        store.close()


def test_pending_approval_delete_is_allowed_but_truncate_is_rejected(
    pg_schema: tuple[str, str], raw: Any
) -> None:
    url, schema = pg_schema
    store = PostgresStore(url, schema=schema)
    try:
        raw.execute(
            "INSERT INTO limos_approvals (approval_id, buyer_id, status, payload) "
            "VALUES ('invented-pending', 'invented-buyer', 'PENDING', '{}'::jsonb)"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            raw.execute("TRUNCATE limos_approvals")
        assert raw.execute("DELETE FROM limos_approvals").rowcount == 1
        assert raw.execute("SELECT count(*) FROM limos_approvals").fetchone()[0] == 0
    finally:
        store.close()
