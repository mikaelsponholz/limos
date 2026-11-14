"""Injectable application resources and JSON parsing for strict contract models."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import Request
from pydantic import BaseModel, BeforeValidator

from agent.llm import LLM, GeminiLLM
from gateway.catalog.index import Catalog, load_catalog
from gateway.errors import GatewayError
from gateway.offers import OfferValio
from gateway.policy import MandateEntry
from gateway.settings import Settings
from gateway.store import FileStore, MemoryStore, Store, create_store
from gateway.valio.client import ValioClient, ValioError


def json_body(model: type[BaseModel]) -> BeforeValidator:
    # FastAPI decodes JSON before validation; strict date fields need JSON semantics.
    def parse(value: Any) -> BaseModel:
        if isinstance(value, model):
            return value
        return model.model_validate_json(json.dumps(value))

    return BeforeValidator(parse)


class Resources:
    def __init__(self, request: Request) -> None:
        self.state = request.app.state
        self.settings = cast(Settings, self.state.settings)

    @property
    def store(self) -> Store:
        if self.state.store is None:
            if self.settings.store == "memory":
                self.state.store = MemoryStore()
            elif self.settings.store == "file":
                self.state.store = FileStore()
            elif self.settings.store == "postgres" and self.settings.database_url:
                self.state.store = create_store(self.settings)
            else:
                raise GatewayError(503, "STORE_UNAVAILABLE", "Store backend is not available.")
        return cast(Store, self.state.store)

    @property
    def catalog(self) -> Catalog:
        if self.state.catalog is None:
            try:
                self.state.catalog = load_catalog(self.settings.catalog_path)
            except FileNotFoundError:
                raise GatewayError(
                    503, "CATALOG_UNAVAILABLE", "Catalogue is not available."
                ) from None
            self._seed_history(cast(Catalog, self.state.catalog))
        return cast(Catalog, self.state.catalog)

    def _seed_history(self, catalog: Catalog) -> None:
        """Resolve the seeded customer history if no overlay exists yet.

        A hosted container prepares its data in a background process; if that process
        could not write the overlay, the gateway resolves it here with the catalogue it
        already holds, so no second copy is loaded. Logs counts and error types only.
        """
        customers_dir = cast(Path, self.state.customers_dir)
        overlay_dir = cast(Path, self.state.customer_overlay_dir)
        if all((overlay_dir / p.name).is_file() for p in customers_dir.glob("*.json")):
            return
        from scripts.seed_customers import resolve_customers

        try:
            customers, lines = resolve_customers(
                catalog, customers_dir=customers_dir, out=overlay_dir
            )
        except Exception as error:  # noqa: BLE001 - the catalogue stays usable either way
            print(f"Customer history not resolved: {type(error).__name__}", flush=True)
            return
        print(f"Customer history resolved: {customers} customers, {lines} lines", flush=True)

    @property
    def valio(self) -> OfferValio:
        if self.state.valio is None:
            if not self.settings.valio_api_key or not self.settings.valio_base_url:
                raise GatewayError(503, "VALIO_UNAVAILABLE", "Valio is not configured.")
            self.state.valio = ValioClient(self.settings)
            self.state.owns_valio = True
        return cast(OfferValio, self.state.valio)

    def now(self) -> datetime:
        return cast(Callable[[], datetime], self.state.clock)()

    @property
    def gateway_http_client(self) -> httpx.Client | None:
        return cast(httpx.Client | None, self.state.gateway_http_client)

    @property
    def llm(self) -> LLM | None:
        if (
            self.state.llm is None
            and self.settings.gemini_api_key.strip()
            and self.settings.gemini_model.strip()
        ):
            self.state.llm = GeminiLLM(self.settings)
        return cast(LLM | None, self.state.llm)

    def new_id(self, prefix: str) -> str:
        return prefix + cast(Callable[[], str], self.state.id_generator)()

    @property
    def mandates(self) -> dict[str, MandateEntry]:
        return cast(dict[str, MandateEntry], self.state.mandates)

    def permit(self, buyer_id: str, agent_id: str) -> MandateEntry:
        mandate = self.mandates.get(buyer_id)
        if mandate is None or agent_id not in mandate.allowed_agents:
            raise GatewayError(403, "AGENT_NOT_PERMITTED", "Agent is not permitted for this buyer.")
        return mandate

    @property
    def customer_paths(self) -> dict[str, Path]:
        # Resolving the seeded history needs the catalogue; load it once it exists so a
        # customer view never shows the placeholder codes. Without it, show them as is.
        if self.state.catalog is None:
            try:
                _ = self.catalog
            except GatewayError:
                pass
        return {
            "customers_dir": cast(Path, self.state.customers_dir),
            "overlay_dir": cast(Path, self.state.customer_overlay_dir),
        }

    def request_buyer(self, request_id: str) -> str:
        buyer = self.store.request_buyer(request_id)
        if buyer is None:
            raise GatewayError(404, "NOT_FOUND", "Request not found.")
        return buyer


def resources(request: Request) -> Resources:
    return Resources(request)


@contextmanager
def domain_errors() -> Iterator[None]:
    try:
        yield
    except ValioError:
        raise GatewayError(
            502, "VALIO_UNAVAILABLE", "Valio could not complete the request."
        ) from None
    except ValueError:
        raise GatewayError(422, "VALIDATION_ERROR", "Invalid quantities or quote inputs.") from None
