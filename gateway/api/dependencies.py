"""Injectable application resources and JSON parsing for strict contract models."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from pydantic import BaseModel, BeforeValidator

from gateway.catalog.index import Catalog, load_catalog
from gateway.errors import GatewayError
from gateway.offers import OfferValio
from gateway.policy import MandateEntry
from gateway.settings import Settings
from gateway.store import FileStore, MemoryStore, Store
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
        return cast(Catalog, self.state.catalog)

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
