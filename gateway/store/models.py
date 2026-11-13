"""Internal persisted records; public response schemas remain unchanged."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from gateway.schemas import Order, PastOrder, Quote, RequestDetail, Step


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    approval_id: str
    buyer_id: str
    agent_id: str
    quote_id: str
    request_id: str
    option_quote_ids: list[str]
    explanation: str
    created_at: AwareDatetime
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"] = "PENDING"
    selected_quote_id: str | None = None
    approved_hash: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class StoreState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: dict[str, RequestDetail] = Field(default_factory=dict)
    request_buyers: dict[str, str] = Field(default_factory=dict)
    quotes: dict[str, Quote] = Field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = Field(default_factory=dict)
    orders: dict[str, Order] = Field(default_factory=dict)
    idempotency: dict[str, dict[str, str]] = Field(default_factory=dict)
    steps: dict[str, list[Step]] = Field(default_factory=dict)
    past_orders: dict[str, dict[str, list[PastOrder]]] = Field(default_factory=dict)
