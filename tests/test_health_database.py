"""/health must test the database, not only that DATABASE_URL is set."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.main import create_app
from gateway.settings import Settings

pytest.importorskip("psycopg")
from gateway.store.postgres import PostgresStore  # noqa: E402

# Nothing listens on port 1, so the login fails like a rotated password or a down host.
UNREACHABLE = "postgresql://limos:wrong@127.0.0.1:1/limos?connect_timeout=1"


def _health(store: PostgresStore, url: str) -> dict[str, str]:
    app = create_app(Settings(valio_api_key="", store="postgres", database_url=url), store=store)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    return dict(response.json())


@pytest.fixture
def unreachable_store() -> Iterator[PostgresStore]:
    store = PostgresStore(UNREACHABLE, migrate=False)
    yield store
    store.close()


def test_health_reports_unreachable_database(unreachable_store: PostgresStore) -> None:
    assert unreachable_store.ping(timeout=1) is False
    assert _health(unreachable_store, UNREACHABLE)["database"] == "unavailable"


def test_health_reports_working_database(pg_schema: tuple[str, str]) -> None:
    url, schema = pg_schema
    store = PostgresStore(url, schema=schema)
    try:
        assert _health(store, url)["database"] == "ok"
    finally:
        store.close()
