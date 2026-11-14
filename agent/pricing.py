"""Price insights for the buyer: cheaper delivery days, season, waste, packs, sales, trends.

Every insight is computed in code from Valio prices, the quote and the buyer's own history.
They only inform the buyer; none of them changes the quote or skips an approval.
"""

import re
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Protocol

from gateway.offers import _available_date, _schedule
from gateway.schemas import Customer, Offer, PriceInsight, Quote, RequestDetail
from gateway.valio.models import AvailabilityResponse, PriceResponse

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
# Finnish fresh-produce seasons, by month. Keywords match Finnish and English names.
SEASONS: list[tuple[tuple[str, ...], str, set[int]]] = [
    (("mansikka", "strawberr"), "strawberries", {6, 7, 8}),
    (("mustikka", "blueberr", "bilberr"), "blueberries", {7, 8, 9}),
    (("vadelma", "raspberr"), "raspberries", {7, 8, 9}),
    (("puolukka", "lingonberr"), "lingonberries", {8, 9, 10}),
    (("parsa", "asparagus"), "asparagus", {5, 6}),
    (("raparperi", "rhubarb"), "rhubarb", {5, 6, 7}),
    (("kantarelli", "chanterelle"), "chanterelles", {7, 8, 9}),
    (("uusi peruna", "uudet perunat", "new potato"), "new potatoes", {6, 7, 8}),
    (("herne", "pea "), "fresh peas", {7, 8}),
    (("kurpitsa", "pumpkin"), "pumpkin", {9, 10, 11}),
    (("luumu", "plum"), "plums", {8, 9}),
]
PRESERVED = (
    "pakaste",
    "pakastettu",
    "frozen",
    "hillo",
    "jam",
    "kuivattu",
    "dried",
    "säilyke",
    "canned",
    "sose",
    "puree",
    "mehu",
    "juice",
    "kastike",
    "sauce",
    "aromi",
    "jogurtti",
    "yogurt",
    "rahka",
    "jäätelö",
    "kakku",
    "cake",
    "kiisseli",
    "keitto",
)
MIN_SAVING = 0.5
MIN_CHANGE = 0.03
MIN_WASTE = 3.0
VALID_AHEAD = 60
SALE_MARGIN = 0.05


class PriceValio(Protocol):
    async def price(self, product_codes: Sequence[str], requested_date: date) -> PriceResponse: ...

    async def availability(
        self, product_codes: Sequence[str], requested_date: date
    ) -> AvailabilityResponse: ...


def _money(value: float) -> str:
    return f"{value:.2f} €"


def _priced(quote: Quote) -> list[Offer]:
    return [line for line in quote.lines if line.unit_price > 0]


def _delivery_days(today: date, delivery: date) -> list[date]:
    """Up to three weekdays before the delivery date, nearest first, from tomorrow on."""
    days: list[date] = []
    day = delivery - timedelta(days=1)
    while day > today and len(days) < 3:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days


async def date_insights(quote: Quote, valio: PriceValio, today: date) -> list[PriceInsight]:
    """Price the same basket on earlier delivery days and name real price changes."""
    lines = _priced(quote)
    if not lines:
        return []
    codes = list(dict.fromkeys(line.product_id for line in lines))
    current = sum(line.line_total for line in lines)
    best: tuple[float, date] | None = None
    changes: dict[str, PriceInsight] = {}
    for day in _delivery_days(today, quote.delivery_date):
        prices = {p.product_id: p for p in (await valio.price(codes, day)).products}
        stock = {s.product_id: s for s in (await valio.availability(codes, day)).products}
        total = 0.0
        complete = True
        for line in lines:
            price = prices.get(line.product_id)
            availability = stock.get(line.product_id)
            schedule = _schedule(availability) if availability else None
            available = schedule is not None and (
                (_available_date(schedule) or date.max) <= day
                and schedule.available_amount >= Decimal(str(line.quantity_sales_units))
            )
            if price is None or price.error or price.catalog_price is None or not available:
                complete = False
                continue
            unit = float(price.catalog_price.value)
            total += unit * line.quantity_sales_units
            change = (line.unit_price - unit) / unit if unit else 0.0
            if abs(change) >= MIN_CHANGE and line.product_id not in changes:
                direction = "rises" if line.unit_price > unit else "falls"
                changes[line.product_id] = PriceInsight(
                    kind="price_change",
                    item_id=line.item_id,
                    product_id=line.product_id,
                    text=(
                        f"The price of {line.name} {direction} by {abs(change):.0%} by your "
                        f"delivery date: {_money(unit)} on {day.isoformat()}, "
                        f"{_money(line.unit_price)} on {quote.delivery_date.isoformat()}."
                    ),
                    saving=round((line.unit_price - unit) * line.quantity_sales_units, 2)
                    if line.unit_price > unit
                    else None,
                )
        if complete and current - total >= MIN_SAVING and (best is None or total < best[0]):
            best = (total, day)
    insights: list[PriceInsight] = []
    # Most Valio prices are open-ended; a near end date means a new price is coming.
    on_delivery = (await valio.price(codes, quote.delivery_date)).products
    by_id = {line.product_id: line for line in lines}
    for entry in on_delivery:
        offer = by_id.get(entry.product_id)
        if offer is None or entry.catalog_price is None or entry.error:
            continue
        try:
            ends = date.fromisoformat(entry.catalog_price.valid_to[:10])
        except ValueError:
            continue
        if quote.delivery_date <= ends <= quote.delivery_date + timedelta(days=VALID_AHEAD):
            insights.append(
                PriceInsight(
                    kind="price_change",
                    item_id=offer.item_id,
                    product_id=offer.product_id,
                    text=(
                        f"Valio's price for {offer.name} ({_money(offer.unit_price)}) is set "
                        f"until {ends.isoformat()}; a new price may apply after that. If it "
                        "keeps, ordering more now locks in today's price."
                    ),
                    saving=None,
                )
            )
    if best is not None:
        total, day = best
        insights.append(
            PriceInsight(
                kind="delivery_date",
                item_id=None,
                product_id=None,
                text=(
                    f"Delivery on {day.strftime('%A')} {day.isoformat()} would cost "
                    f"{_money(total)} instead of {_money(current)}, everything in stock. "
                    "Change the delivery date if an earlier delivery suits you."
                ),
                saving=round(current - total, 2),
            )
        )
    return insights[:3] + list(changes.values())[:3]


def _season(text: str) -> tuple[str, set[int]] | None:
    lowered = f"{text.casefold()} "
    if any(word in lowered for word in PRESERVED):
        return None
    for keywords, name, months in SEASONS:
        if any(keyword in lowered for keyword in keywords):
            return name, months
    return None


def season_insights(detail: RequestDetail, delivery: date) -> list[PriceInsight]:
    """Name fresh produce out of season (pricier, weaker) or in season (best value)."""
    insights: list[PriceInsight] = []
    seen: set[str] = set()
    names = {line.item_id: line.name for line in (detail.quote.lines if detail.quote else [])}
    month = MONTHS[delivery.month - 1]
    for item in detail.intent.items:
        text = " ".join([item.description, item.description_fi, names.get(item.item_id, "")])
        found = _season(text)
        if found is None or found[0] in seen:
            continue
        name, months = found
        seen.add(name)
        if delivery.month in months:
            message = (
                f"Fresh {name} are in season in {month}: the best taste and usually the "
                "lowest price of the year."
            )
        else:
            first = MONTHS[min(months) - 1]
            message = (
                f"Fresh {name} are out of season in {month} (Finnish season from {first}), so "
                "they cost more and taste less. "
                + (
                    f"Frozen {name} are usually cheaper and work well in baking."
                    if name.endswith("berries")
                    else "Something that is in season now may suit better."
                )
            )
        insights.append(
            PriceInsight(
                kind="season",
                item_id=item.item_id,
                product_id=None,
                text=message,
                saving=None,
            )
        )
    if detail.intent.event and detail.intent.event.date.month == 12:
        if detail.intent.event.date.day <= 26:
            insights.append(
                PriceInsight(
                    kind="season",
                    item_id=None,
                    product_id=None,
                    text=(
                        "Christmas season: stock and delivery slots tighten before Christmas, "
                        "so ordering early keeps your choice of products and prices."
                    ),
                    saving=None,
                )
            )
    return insights


def _tokens(name: str) -> set[str]:
    return {token for token in re.findall(r"[^\W\d_]{3,}", name.casefold())}


def _discount_insights(lines: Sequence[Offer]) -> list[PriceInsight]:
    """The usual gap to list price is the buyer's agreement; only a deeper cut is a sale."""
    off = {
        line.product_id: 1 - line.unit_price / line.list_price
        for line in lines
        if line.list_price > line.unit_price * 1.01
    }
    if not off:
        return []
    usual = median(off.values())
    below = sum(
        (line.list_price - line.unit_price) * line.quantity_sales_units
        for line in lines
        if line.product_id in off
    )
    insights = [
        PriceInsight(
            kind="discount",
            item_id=None,
            product_id=None,
            text=(
                f"Your customer prices are {_money(below)} below Valio's list prices on this "
                f"order (usually {usual:.0%} off)."
            ),
            saving=None,
        )
    ]
    for line in lines:
        cut = off.get(line.product_id)
        if cut is not None and cut >= usual + SALE_MARGIN:
            insights.append(
                PriceInsight(
                    kind="discount",
                    item_id=line.item_id,
                    product_id=line.product_id,
                    text=(
                        f"{line.name} is on sale: {cut:.0%} off the list price "
                        f"{_money(line.list_price)}, deeper than your usual {usual:.0%}."
                    ),
                    saving=round(
                        (line.list_price - line.unit_price) * line.quantity_sales_units, 2
                    ),
                )
            )
    return insights


def line_insights(quote: Quote, candidates: Sequence[Offer]) -> list[PriceInsight]:
    """Leftover from pack sizes, a bigger pack that costs less, and products on sale."""
    insights: list[PriceInsight] = []
    waste: list[PriceInsight] = []
    for line in _priced(quote):
        need, got = line.quantity_requested, line.quantity_covered
        if need.unit == got.unit and got.value > 0 and need.value > 0:
            leftover = got.value - need.value
            value = line.line_total * leftover / got.value
            if leftover / got.value >= 0.25 and value >= MIN_WASTE:
                waste.append(
                    PriceInsight(
                        kind="waste",
                        item_id=line.item_id,
                        product_id=line.product_id,
                        text=(
                            f"You need {need.value:g} {need.unit} of {line.name} but the packs "
                            f"give {got.value:g} {got.unit}: {leftover:g} {got.unit} (about "
                            f"{_money(value)}) is left over. Use it in another dish or pick a "
                            "smaller pack."
                        ),
                        saving=round(value, 2),
                    )
                )
        brand = _tokens(line.name)
        better = [
            other
            for other in candidates
            if other.item_id == line.item_id
            and other.product_id != line.product_id
            and other.vendor == line.vendor
            and other.unit_price > 0
            and other.comparison_price.unit == line.comparison_price.unit
            and 0 < other.comparison_price.value < line.comparison_price.value
            and other.quantity_covered.unit == need.unit
            and other.quantity_covered.value >= need.value
            and line.line_total - other.line_total >= MIN_SAVING
            # The same product line in another pack: only the size differs in the name.
            and _tokens(other.name) == brand
        ]
        if better:
            pack = min(better, key=lambda other: other.line_total)
            unit = pack.comparison_price.unit.lower()
            insights.append(
                PriceInsight(
                    kind="pack_size",
                    item_id=line.item_id,
                    product_id=pack.product_id,
                    text=(
                        f"{pack.name} covers the same need for {_money(pack.line_total)} "
                        f"instead of {_money(line.line_total)} "
                        f"({pack.comparison_price.value:.2f} €/{unit} vs "
                        f"{line.comparison_price.value:.2f} €/{unit})."
                    ),
                    saving=round(line.line_total - pack.line_total, 2),
                )
            )
    # The costliest leftovers only; small spice and baking packs keep and are noise.
    waste.sort(key=lambda insight: insight.saving or 0, reverse=True)
    return waste[:3] + insights + _discount_insights(_priced(quote))


def history_insights(detail: RequestDetail, customer: Customer | None) -> list[PriceInsight]:
    """Cost per guest against the last similar event, and price trends of repeat products."""
    quote = detail.quote
    if quote is None or quote.total <= 0:
        return []
    insights: list[PriceInsight] = []
    event = detail.intent.event
    # Only orders placed through Limos have real prices; seeded history is invented.
    history = (
        sorted(
            (order for order in customer.order_history if order.source == "limos"),
            key=lambda order: order.date,
        )
        if customer
        else []
    )
    if event is not None and event.guests > 0:
        per_guest = quote.total / event.guests
        text = f"{_money(per_guest)} per guest for {event.guests} guests."
        earlier = [
            order
            for order in history
            if order.guests and order.total > 0 and order.event_type == event.type
        ]
        if earlier:
            last = earlier[-1]
            before = last.total / (last.guests or 1)
            text += (
                f" Your {last.title} on {last.date.isoformat()} cost {_money(before)} per guest."
            )
        insights.append(
            PriceInsight(kind="per_guest", item_id=None, product_id=None, text=text, saving=None)
        )
    for line in _priced(quote):
        past = [
            (order.date, past_line.unit_price)
            for order in history
            for past_line in order.lines
            if past_line.product_id == line.product_id and past_line.unit_price > 0
        ]
        if not past:
            continue
        when, before = max(past)
        change = (line.unit_price - before) / before
        if abs(change) >= MIN_CHANGE:
            word = "dearer" if change > 0 else "cheaper"
            insights.append(
                PriceInsight(
                    kind="price_trend",
                    item_id=line.item_id,
                    product_id=line.product_id,
                    text=(
                        f"{line.name} is {abs(change):.0%} {word} than in your order on "
                        f"{when.isoformat()} ({_money(before)} then, "
                        f"{_money(line.unit_price)} now)."
                    ),
                    saving=None,
                )
            )
    return insights


# The chat shows the first few: money the buyer can act on first, background facts last.
RANK = {
    "delivery_date": 0,
    "season": 1,
    "waste": 2,
    "pack_size": 2,
    "price_change": 3,
    "discount": 3,
    "price_trend": 4,
    "per_guest": 5,
}


def _rank(insight: PriceInsight) -> tuple[int, float]:
    rank = RANK[insight.kind]
    if insight.kind == "discount" and insight.item_id is None:
        rank = 6  # The agreement summary is background, not a tip.
    return rank, -(insight.saving or 0)


async def price_insights(
    detail: RequestDetail,
    customer: Customer | None,
    valio: PriceValio,
    today: date,
) -> list[PriceInsight]:
    quote = detail.quote
    if quote is None:
        return []
    found = [
        *(await date_insights(quote, valio, today)),
        *season_insights(detail, quote.delivery_date),
        *line_insights(quote, detail.candidates),
        *history_insights(detail, customer),
    ]
    return sorted(found, key=_rank)
