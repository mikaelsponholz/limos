from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent.llm import LLM
from gateway.api import approvals, audit, customers, gateway_tools, presets, requests, stock
from gateway.api.health import router as health_router
from gateway.catalog.index import Catalog
from gateway.customers import CUSTOMERS_DIR, OVERLAY_DIR
from gateway.errors import register_error_handlers
from gateway.offers import OfferValio
from gateway.policy.mandates import MandateEntry, load_mandates
from gateway.schemas import CONTRACT_VERSION
from gateway.settings import Settings
from gateway.store import Store
from gateway.valio.client import ValioClient


def create_app(
    settings: Settings | None = None,
    *,
    catalog: Catalog | None = None,
    valio: OfferValio | None = None,
    store: Store | None = None,
    clock: Callable[[], datetime] | None = None,
    id_generator: Callable[[], str] | None = None,
    mandates: dict[str, MandateEntry] | None = None,
    customers_dir: Path = CUSTOMERS_DIR,
    customer_overlay_dir: Path = OVERLAY_DIR,
    llm: LLM | None = None,
    gateway_http_client: httpx.Client | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if app.state.owns_valio and isinstance(app.state.valio, ValioClient):
                await app.state.valio.aclose()

    application = FastAPI(title="Limos", version=CONTRACT_VERSION, lifespan=lifespan)
    application.state.settings = config
    application.state.store = store
    application.state.catalog = catalog
    application.state.valio = valio
    application.state.llm = llm
    application.state.gateway_http_client = gateway_http_client
    # Tests replace the NVIDIA endpoint here; production uses the network.
    application.state.vision_transport = None
    application.state.owns_valio = False
    application.state.clock = clock if clock is not None else lambda: datetime.now(UTC)
    application.state.id_generator = (
        id_generator if id_generator is not None else lambda: uuid4().hex
    )
    application.state.mandates = mandates if mandates is not None else load_mandates()
    application.state.customers_dir = customers_dir
    application.state.customer_overlay_dir = customer_overlay_dir
    register_error_handlers(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.web_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    for router in (
        gateway_tools.router,
        customers.router,
        presets.router,
        requests.router,
        approvals.router,
        audit.router,
        stock.router,
    ):
        application.include_router(router)
    return application


app = create_app()
