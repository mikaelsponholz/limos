"""Turn fill levels read from a stock photo into refill items, in code and never by a model.

The customer's stock profile says how much a full shelf holds and below which fill level a
product is reordered. A product at 25 % of a 40 L shelf needs 30 L to be full again.
"""

from math import ceil

from gateway.errors import GatewayError
from gateway.schemas import Customer, RequestItem, StockItem


def _key(name: str) -> str:
    return " ".join(name.casefold().split())


def match_stock(customer: Customer, name: str) -> StockItem | None:
    """Find the stocked product for a name the vision model returned, ignoring case."""
    wanted = _key(name)
    for item in customer.stock:
        if wanted in {_key(item.name), *(_key(alias) for alias in item.aliases)}:
            return item
    return None


def refill_quantity(item: StockItem, level: float) -> float:
    missing = item.full_quantity * (100 - level) / 100
    # Pieces are ordered whole; other units keep two decimals.
    return float(ceil(missing)) if item.unit == "pcs" else round(missing, 2)


def stock_items(
    levels: dict[str, float], customer: Customer | None
) -> tuple[list[RequestItem], list[str]]:
    """Return the refill items and one plain note for every product not ordered."""
    if customer is None or not customer.stock:
        raise GatewayError(
            422, "STOCK_PROFILE_REQUIRED", "Stock levels need a customer with a stock profile."
        )
    items: list[RequestItem] = []
    notes: list[str] = []
    for name, level in levels.items():
        stocked = match_stock(customer, name)
        if stocked is None:
            notes.append(f"{name} is not in your stock list, so it was not ordered.")
            continue
        if level >= stocked.reorder_below_percent:
            notes.append(
                f"{stocked.name} is {level:g} % full, at or above the "
                f"{stocked.reorder_below_percent:g} % reorder level, so it was not ordered."
            )
            continue
        quantity = refill_quantity(stocked, level)
        if quantity <= 0:
            continue
        items.append(
            RequestItem(
                description=stocked.description,
                quantity=quantity,
                unit=stocked.unit,
                constraints=list(stocked.constraints),
            )
        )
        notes.append(
            f"{stocked.name} is {level:g} % full; ordering {quantity:g} {stocked.unit} "
            f"to fill it to {stocked.full_quantity:g} {stocked.unit}."
        )
    if not items:
        raise GatewayError(
            422, "NOTHING_TO_REORDER", "Every recognised product is above its reorder level."
        )
    return items, notes
