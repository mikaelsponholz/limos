"""Shared fixtures. The Postgres fixture runs only when DATABASE_URL is set.

Never let the connection string reach test output: it holds the database password.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]


SKIP_VARIABLE = "LIMOS_SKIP_PG_TESTS"


def _skip_requested() -> bool:
    return os.getenv(SKIP_VARIABLE, "").strip().lower() in {"1", "true", "yes"}


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    env = ROOT / ".env"
    if not url and env.exists():
        # Read only this one key; the value is never printed.
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    return url


class PgTarget(NamedTuple):
    """The database URL and test schema. Its repr hides the URL, which holds a password."""

    url: str
    schema: str

    def __repr__(self) -> str:
        return f"PgTarget(url=<hidden>, schema={self.schema!r})"


def _run(url: str, statement: str, action: str) -> None:
    """Run one statement; on any failure report a generic message, never the URL."""
    import psycopg

    failed = False
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(statement)
    except Exception:
        # The exception and its traceback can contain the connection string.
        failed = True
    if failed:
        pytest.fail(f"Postgres test database: could not {action} (details hidden).", pytrace=False)


@pytest.fixture
def pg_schema() -> Iterator[PgTarget]:
    """A fresh schema on the configured database, dropped after the test.

    Skipped without reading .env when LIMOS_SKIP_PG_TESTS=1 (set in every Codex build).
    """
    if _skip_requested():
        pytest.skip(f"{SKIP_VARIABLE} is set")
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL is not set")
    pytest.importorskip("psycopg")
    target = PgTarget(url, f"limos_test_{uuid.uuid4().hex[:12]}")
    _run(url, f'CREATE SCHEMA "{target.schema}"', "connect or create the test schema")
    try:
        yield target
    finally:
        _run(url, f'DROP SCHEMA "{target.schema}" CASCADE', "drop the test schema")
