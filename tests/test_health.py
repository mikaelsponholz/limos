import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from gateway.errors import GatewayError
from gateway.main import create_app
from gateway.settings import Settings


@pytest.fixture
def app() -> FastAPI:
    return create_app(
        Settings(
            valio_api_key="",
            store="memory",
            web_origin="http://localhost:3000, http://localhost:3001",
        )
    )


def test_health(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "valio_api": "unconfigured",
        "database": "ok",
    }


def test_not_found_error(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/missing")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "NOT_FOUND", "message": "Not Found"}}


def test_validation_error(app: FastAPI) -> None:
    @app.get("/test-validation")
    async def validation_route(count: int) -> dict[str, int]:
        return {"count": count}

    with TestClient(app) as client:
        response = client.get("/test-validation", params={"count": "invalid"})
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "VALIDATION_ERROR", "message": "Request validation failed."}
    }


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://localhost:3001"])
def test_cors(app: FastAPI, origin: str) -> None:
    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": origin})
        preflight = client.options(
            "/health",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
    assert response.headers["access-control-allow-origin"] == origin
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin


def test_cors_rejects_other_origin(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://localhost:3002"})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("error", "status_code", "code", "message"),
    [
        (
            GatewayError(409, "QUOTE_CHANGED", "Quote changed."),
            409,
            "QUOTE_CHANGED",
            "Quote changed.",
        ),
        (HTTPException(403, "Forbidden"), 403, "FORBIDDEN", "Forbidden"),
        (RuntimeError("internal details"), 500, "INTERNAL_ERROR", "An internal error occurred."),
    ],
)
def test_error_handlers(
    app: FastAPI, error: Exception, status_code: int, code: str, message: str
) -> None:
    @app.get("/test-error")
    async def error_route() -> None:
        raise error

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-error")
    assert response.status_code == status_code
    assert response.json() == {"error": {"code": code, "message": message}}
