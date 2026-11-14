"""Deterministic, idempotent simulated ordering under the buyer's mandate."""

from collections.abc import Callable
from datetime import datetime

from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.policy import MandateEntry
from gateway.quotes import compute_content_hash, get_quote, require_unexpired
from gateway.schemas import Order, PlacedBy
from gateway.store import Store


def place_order(
    quote_id: str,
    buyer_id: str,
    agent_id: str,
    idempotency_key: str,
    now: datetime,
    *,
    store: Store,
    mandate: MandateEntry,
    id_generator: Callable[[], str],
) -> Order:
    with store.transaction():
        quote = get_quote(store, quote_id, buyer_id, now)
        if mandate.buyer_id != buyer_id or agent_id not in mandate.allowed_agents:
            raise GatewayError(403, "AGENT_NOT_PERMITTED", "Agent is not permitted for this buyer.")
        existing = store.get_idempotent_order(idempotency_key, buyer_id)
        if existing:
            if existing.quote_id != quote_id:
                raise GatewayError(409, "IDEMPOTENCY_CONFLICT", "Key was used for another quote.")
            return existing
        if quote.status == "ORDERED" or store.order_for_request(quote.request_id, buyer_id):
            raise GatewayError(409, "ALREADY_ORDERED", "An order has already been placed.")
        require_unexpired(quote, now)
        if quote.status == "BLOCKED" or quote.policy.outcome == "BLOCK":
            raise GatewayError(422, "BLOCKED", "Quote is blocked by policy.")
        if compute_content_hash(quote) != quote.content_hash:
            raise GatewayError(409, "HASH_MISMATCH", "Locked quote content has changed.")
        if quote.status not in {"AUTO_APPROVED", "PENDING_APPROVAL", "APPROVED"}:
            raise GatewayError(403, "NEEDS_APPROVAL", "Quote is not approved for ordering.")
        approval = None
        if quote.policy.outcome != "AUTO" or quote.status != "AUTO_APPROVED":
            approved = [
                a
                for a in store.list_approvals(buyer_id)
                if a.status == "APPROVED" and a.selected_quote_id == quote_id
            ]
            approval = next((a for a in approved if a.approved_hash == quote.content_hash), None)
            if approval is None:
                if approved:
                    raise GatewayError(409, "HASH_MISMATCH", "Approved hash no longer matches.")
                raise GatewayError(403, "NEEDS_APPROVAL", "A matching human approval is required.")
        if approval:
            authority = (
                f"{approval.decided_by} approved quote {quote_id} and its content hash matched."
            )
        else:
            limits = quote.policy.mandate
            authority = (
                f"Agent {agent_id} placed quote {quote_id} with total {quote.total:.2f} EUR "
                f"within the {limits.auto_limit:.2f} EUR auto limit, "
                f"each line within {limits.line_limit:.2f} EUR and "
                f"the total within the {limits.hard_cap:.2f} EUR hard cap"
            )
            if quote.budget:
                authority += f" and the {quote.budget.limit:.2f} EUR budget"
            authority += "."
        order = Order(
            order_id=id_generator(),
            quote_id=quote_id,
            request_id=quote.request_id,
            status="CONFIRMED",
            placed_at=now,
            placed_by=PlacedBy(type="agent", name=agent_id),
            approved_by=approval.decided_by if approval else None,
            simulated=True,
            authority=authority,
            idempotency_key=idempotency_key,
            delivery_date=quote.delivery_date,
            order_deadline=quote.order_deadline,
            lines=quote.lines,
            total=quote.total,
            total_with_tax=quote.total_with_tax,
        )
        store.save_order(order, buyer_id)
        quote.status = "ORDERED"
        store.save_quote(quote)
        if quote.request_id:
            append_step(
                store,
                quote.request_id,
                "gateway",
                "ORDER_PLACED",
                "Simulated order placed",
                {"order_id": order.order_id, "quote_id": quote_id, "simulated": True},
                None,
                now,
            )
        return order
