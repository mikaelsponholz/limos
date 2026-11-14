"""Seeded demo profiles, recorded defaults and deterministic order history."""

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from math import ceil
from pathlib import Path

from pydantic import JsonValue

from gateway.errors import GatewayError
from gateway.schemas import (
    BasedOn,
    Customer,
    CustomerSummary,
    FilledGap,
    LastOrder,
    Order,
    PastOrder,
    PastOrderLine,
    RequestCreate,
)
from gateway.store import Store

ROOT = Path(__file__).resolve().parents[1]
CUSTOMERS_DIR = ROOT / "customers"
OVERLAY_DIR = ROOT / ".runtime" / "customers"


def load_customers(
    customers_dir: Path = CUSTOMERS_DIR, overlay_dir: Path | None = OVERLAY_DIR
) -> dict[str, Customer]:
    """Only committed profile ids are recognised; overlays replace their seeded data."""
    customers: dict[str, Customer] = {}
    for path in sorted(customers_dir.glob("*.json")):
        customer = Customer.model_validate_json(path.read_bytes())
        if customer.customer_id != path.stem:
            raise ValueError("Customer id must match its file name.")
        overlay = overlay_dir / path.name if overlay_dir is not None else None
        if overlay is not None and overlay.is_file():
            resolved = Customer.model_validate_json(overlay.read_bytes())
            if (resolved.customer_id, resolved.buyer_id) != (
                customer.customer_id,
                customer.buyer_id,
            ):
                raise ValueError("Customer overlay must retain customer and buyer ids.")
            customer = resolved
        customers[customer.customer_id] = customer
    return customers


def _with_history(customer: Customer, store: Store) -> Customer:
    # Later appends win ties on a date, the finest precision in the public contract.
    history = {
        order.order_ref: order
        for order in reversed(store.get_past_orders(customer.customer_id, customer.buyer_id))
    }
    for order in customer.order_history:
        history.setdefault(order.order_ref, order)
    return customer.model_copy(
        update={"order_history": sorted(history.values(), key=lambda o: o.date, reverse=True)},
        deep=True,
    )


def get_customer(
    customer_id: str,
    store: Store,
    *,
    customers_dir: Path = CUSTOMERS_DIR,
    overlay_dir: Path = OVERLAY_DIR,
) -> Customer:
    customer = load_customers(customers_dir, overlay_dir).get(customer_id)
    if customer is None:
        raise GatewayError(404, "CUSTOMER_NOT_FOUND", "Customer was not found.")
    return _with_history(customer, store)


def list_customers(
    store: Store,
    *,
    customers_dir: Path = CUSTOMERS_DIR,
    overlay_dir: Path = OVERLAY_DIR,
) -> list[CustomerSummary]:
    summaries = []
    for profile in load_customers(customers_dir, overlay_dir).values():
        customer = _with_history(profile, store)
        last = latest_order(customer)
        summaries.append(
            CustomerSummary(
                customer_id=customer.customer_id,
                name=customer.name,
                company=customer.company,
                role=customer.role,
                age_group=customer.age_group,
                playful=customer.playful,
                last_order=LastOrder(order_ref=last.order_ref, date=last.date, title=last.title)
                if last
                else None,
                simulated=True,
            )
        )
    return summaries


def apply_profile(
    request: RequestCreate,
    customer: Customer | None = None,
    *,
    respect_explicit_lists: bool = False,
) -> tuple[RequestCreate, list[FilledGap]]:
    """Return an isolated, resolved request and the provenance of every supplied gap."""
    resolved = request.model_copy(deep=True)
    gaps: list[FilledGap] = []

    def record(field: str, value: JsonValue, label: str) -> None:
        gaps.append(
            FilledGap(
                field=field,
                value=value,
                source="profile" if customer else "default",
                note=f"{label}, from {customer.name}'s profile"
                if customer
                else f"{label}, system default",
            )
        )

    if customer:
        if request.buyer_id is not None and request.buyer_id != customer.buyer_id:
            raise GatewayError(422, "BUYER_MISMATCH", "Buyer must match the customer's buyer.")
        if request.buyer_id is None:
            resolved.buyer_id = customer.buyer_id
            record("buyer_id", resolved.buyer_id, f"Buyer {resolved.buyer_id}")
        if "budget" not in request.model_fields_set and customer.defaults.budget is not None:
            resolved.budget = customer.defaults.budget
            record("budget", resolved.budget, f"Budget {resolved.budget:g} EUR")
        existing_keys = {preference.key for preference in request.preferences}
        for preference in (
            []
            if respect_explicit_lists and "preferences" in request.model_fields_set
            else customer.preferences
        ):
            if preference.key not in existing_keys:
                resolved.preferences.append(preference.model_copy(deep=True))
                existing_keys.add(preference.key)
                record(
                    f"preferences.{preference.key}",
                    preference.model_dump(mode="json"),
                    f"Preference {preference.key}: {preference.level}",
                )
        if (
            not request.exclude_allergens
            and customer.exclude_allergens
            and not (respect_explicit_lists and "exclude_allergens" in request.model_fields_set)
        ):
            resolved.exclude_allergens = customer.exclude_allergens.copy()
            record(
                "exclude_allergens",
                list(resolved.exclude_allergens),
                f"Excluded allergens: {', '.join(resolved.exclude_allergens)}",
            )
    if "priority" not in request.model_fields_set:
        resolved.priority = customer.defaults.priority if customer else "balanced"
        record("priority", resolved.priority, f"Priority {resolved.priority}")
    return resolved, gaps


def latest_order(customer: Customer, event_type: str | None = None) -> PastOrder | None:
    return max(
        (o for o in customer.order_history if event_type is None or o.event_type == event_type),
        key=lambda order: order.date,
        default=None,
    )


@dataclass(frozen=True)
class ReorderLine:
    product_id: str
    name: str
    sales_unit: str
    quantity_sales_units: int


def resolve_reorder(
    customer: Customer, order_ref: str, guests_now: int | None
) -> tuple[BasedOn, list[ReorderLine]]:
    past = next((o for o in customer.order_history if o.order_ref == order_ref), None)
    if past is None:
        raise GatewayError(404, "ORDER_NOT_FOUND", "Past order was not found for this customer.")
    if (guests_now is not None and guests_now <= 0) or (
        past.guests is not None and past.guests <= 0
    ):
        raise GatewayError(422, "INVALID_GUESTS", "Guest counts must be positive when provided.")
    scale = (
        Fraction(guests_now, past.guests)
        if guests_now is not None and past.guests is not None
        else Fraction(1)
    )
    based_on = BasedOn(
        order_ref=past.order_ref,
        title=past.title,
        date=past.date,
        guests_before=past.guests,
        guests_now=guests_now,
        scale=float(scale),
    )
    lines = [
        ReorderLine(
            product_id=line.product_id,
            name=line.name,
            sales_unit=line.sales_unit,
            quantity_sales_units=max(1, ceil(Fraction(str(line.quantity_sales_units)) * scale)),
        )
        for line in past.lines
    ]
    return based_on, lines


def append_placed_order(
    store: Store,
    customer_id: str,
    order: Order,
    title: str,
    event_type: str,
    guests: int | None,
    now: datetime,
    *,
    customers_dir: Path = CUSTOMERS_DIR,
    overlay_dir: Path = OVERLAY_DIR,
) -> PastOrder:
    with store.transaction():
        customer = get_customer(
            customer_id, store, customers_dir=customers_dir, overlay_dir=overlay_dir
        )
        order_ref = f"ph_{order.order_id}"
        existing = next((o for o in customer.order_history if o.order_ref == order_ref), None)
        if existing is not None:
            return existing
        past = PastOrder(
            order_ref=order_ref,
            date=now.date(),
            title=title,
            event_type=event_type,
            guests=guests,
            lines=[
                PastOrderLine(
                    product_id=line.product_id,
                    name=line.name,
                    quantity_sales_units=line.quantity_sales_units,
                    sales_unit=line.sales_unit,
                    quantity_text=line.quantity_text,
                    unit_price=line.unit_price,
                    line_total=line.line_total,
                )
                for line in order.lines
            ],
            total=order.total,
            source="limos",
            simulated=True,
        )
        store.append_past_order(customer_id, customer.buyer_id, past)
        return past
