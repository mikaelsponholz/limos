"""Shared invented records and deterministic identities for step 3 tests."""

from collections.abc import Iterator
from itertools import count
from typing import Any

from gateway.policy import ItemContext, MandateEntry
from gateway.quotes import create_quote
from gateway.schemas import Intent, Offer, Quote, RequestDetail, RequestInput
from gateway.store import MemoryStore, Store
from tests.test_offers import NOW, REQUESTED, offer

BUYER = "invented-buyer"
AGENT = "invented-agent"


def mandate(**changes: Any) -> MandateEntry:
    return MandateEntry.model_validate(
        {
            "buyer_id": BUYER,
            "auto_limit": 1500.0,
            "line_limit": 250.0,
            "hard_cap": 5000.0,
            "max_overbuy_percent": 25.0,
            "waste_percent": 5.0,
            "allowed_agents": (AGENT,),
            "terminal_reliability_threshold": 0.9,
            **changes,
        }
    )


class Engine:
    def __init__(self, store: Store | None = None) -> None:
        self.store: Store = store if store is not None else MemoryStore()
        self.ids: Iterator[str] = (f"invented_{number}" for number in count(1))

    def next_id(self) -> str:
        return next(self.ids)

    async def quote(self, *, lines: list[Offer] | None = None, **changes: Any) -> Quote:
        params: dict[str, Any] = {
            "store": self.store,
            "request_id": "invented-request",
            "buyer_id": BUYER,
            "agent_id": AGENT,
            "requested_date": REQUESTED,
            "item_context": {"it_flour": ItemContext()},
            "mandate": mandate(),
            "now": NOW,
            "id_generator": self.next_id,
            **changes,
        }
        return create_quote(lines if lines is not None else [await offer()], **params)


def request_detail() -> RequestDetail:
    return RequestDetail(
        request_id="invented-request",
        buyer_id=BUYER,
        customer_id="invented-customer",
        preset_id=None,
        status="RUNNING",
        created_at=NOW,
        input=RequestInput(text="Invented flour order", image_url=None),
        intent=Intent(
            event=None,
            dishes=[],
            items=[],
            deliver_by=REQUESTED,
            preferences=[],
            exclude_allergens=[],
            budget=None,
            priority="balanced",
            assumptions=[],
            based_on=None,
            filled_gaps=[],
        ),
        question=None,
        steps=[],
        candidates=[],
        quote=None,
        suggestions=[],
        approval_id=None,
        order=None,
        error=None,
    )
