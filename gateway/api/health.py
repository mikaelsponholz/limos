from typing import Annotated, Literal

from fastapi import APIRouter, Depends

from gateway.api.dependencies import Resources, resources
from gateway.errors import GatewayError
from gateway.schemas import Health

router = APIRouter()


def _database(env: Resources) -> Literal["ok", "unconfigured", "unavailable"]:
    settings = env.settings
    if settings.store in {"memory", "file"}:
        return "ok"
    if settings.store != "postgres" or not settings.database_url:
        return "unconfigured"
    try:
        store = env.store
    except (GatewayError, ValueError, OSError):
        return "unavailable"
    # A set DATABASE_URL is not proof: a rotated password or an unreachable host must show here.
    ping = getattr(store, "ping", None)
    if ping is None:
        return "ok"
    return "ok" if ping() else "unavailable"


# A plain def runs in the thread pool, so the database check never blocks the event loop.
@router.get("/health")
def health(env: Annotated[Resources, Depends(resources)]) -> Health:
    return Health(
        status="ok",
        valio_api="ok" if env.settings.valio_api_key else "unconfigured",
        database=_database(env),
    )
