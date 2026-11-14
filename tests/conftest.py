"""Shared fixtures. The Postgres fixture runs only when DATABASE_URL is set."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    env = ROOT / ".env"
    if not url and env.exists():
        # Read only this one key; the value is never printed.
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    return url


@pytest.fixture
def pg_schema() -> Iterator[tuple[str, str]]:
    """A fresh schema on the configured database, dropped after the test."""
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL is not set")
    psycopg = pytest.importorskip("psycopg")
    schema = f"limos_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield url, schema
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
