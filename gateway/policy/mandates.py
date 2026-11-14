"""Validated buyer configuration and its public contract view."""

from pathlib import Path
from typing import Self

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from gateway.errors import GatewayError
from gateway.schemas import Mandate

MANDATES_PATH = Path(__file__).with_name("mandates.json")


class MandateEntry(Mandate):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    buyer_id: str = Field(min_length=1)
    auto_limit: float = Field(ge=0)
    line_limit: float = Field(ge=0)
    hard_cap: float = Field(ge=0)
    max_overbuy_percent: float = Field(ge=0)
    waste_percent: float = Field(ge=0)
    allowed_agents: tuple[str, ...] = Field(min_length=1)
    terminal_reliability_threshold: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_limits_and_agents(self) -> Self:
        if self.auto_limit > self.hard_cap:
            raise ValueError("Auto limit must not exceed the hard cap.")
        if any(not agent.strip() for agent in self.allowed_agents):
            raise ValueError("Allowed agent ids must not be blank.")
        if len(set(self.allowed_agents)) != len(self.allowed_agents):
            raise ValueError("Allowed agent ids must be unique.")
        return self

    @property
    def contract(self) -> Mandate:
        """Return only the fields agreed in the public API contract."""
        return Mandate.model_validate(self.model_dump(include=set(Mandate.model_fields)))


class UnknownBuyerError(GatewayError):
    def __init__(self, buyer_id: str) -> None:
        self.buyer_id = buyer_id
        super().__init__(404, "BUYER_NOT_FOUND", f"No mandate configured for buyer: {buyer_id}.")


def load_mandates(path: Path = MANDATES_PATH) -> dict[str, MandateEntry]:
    """Validate all entries, rejecting duplicate buyers rather than silently overwriting."""
    entries = TypeAdapter(list[MandateEntry]).validate_json(path.read_text(encoding="utf-8"))
    mandates = {entry.buyer_id: entry for entry in entries}
    if len(mandates) != len(entries):
        raise ValueError("Mandate buyer ids must be unique.")
    return mandates


def get_mandate(buyer_id: str) -> MandateEntry:
    try:
        return load_mandates()[buyer_id]
    except KeyError:
        raise UnknownBuyerError(buyer_id) from None
