"""Deterministic shortlist pricing and pack conversion, without ranking or policy."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from math import isfinite
from typing import Literal, Protocol, cast

from gateway.catalog.index import Catalog
from gateway.catalog.models import PackSize, SlimRecord
from gateway.catalog.packsize import resolve_pack_size
from gateway.errors import GatewayError
from gateway.policy import ItemContext
from gateway.schemas import (
    AllergenCheck,
    Allergens,
    ComparisonPrice,
    Constraint,
    Offer,
    OfferFlag,
    OffersBody,
    Quantity,
    QuoteLine,
)
from gateway.valio.client import ValioClient
from gateway.valio.models import (
    AvailabilityResponse,
    PriceResponse,
    ProductAvailability,
    ProductPrice,
    ScheduleLine,
    normalize_amount,
)

type Check = Literal["confirmed", "violated", "unknown"]
_NEED_UNITS = {
    "g": ("kg", Decimal("0.001")),
    "kg": ("kg", Decimal(1)),
    "ml": ("L", Decimal("0.001")),
    "L": ("L", Decimal(1)),
    "pcs": ("pcs", Decimal(1)),
}
_MEAT_ALLERGENS = {"fish", "crustaceans", "molluscs"}


class OfferValio(Protocol):
    async def price(self, product_codes: Iterable[str], requested_date: date) -> PriceResponse: ...

    async def availability(
        self, product_codes: Iterable[str], requested_date: date
    ) -> AvailabilityResponse: ...


class OfferSnapshot:
    """Request-local wire evidence shared by shortlists and locked quote options.

    A new date or new substitute ids fetch new evidence. Rebuilding offers from
    this snapshot always runs the normal conversion and policy inputs again.
    """

    def __init__(self, source: OfferValio) -> None:
        self.source = source
        self.prices: dict[date, PriceResponse] = {}
        self.stock: dict[date, AvailabilityResponse] = {}
        self.price_ids: dict[date, set[str]] = {}
        self.stock_ids: dict[date, set[str]] = {}

    async def price(self, product_codes: Iterable[str], requested_date: date) -> PriceResponse:
        ids = list(dict.fromkeys(product_codes))
        missing = [code for code in ids if code not in self.price_ids.get(requested_date, set())]
        if missing:
            response = await self.source.price(missing, requested_date)
            previous = self.prices.get(requested_date)
            self.prices[requested_date] = response.model_copy(
                update={"products": [*(previous.products if previous else []), *response.products]}
            )
            self.price_ids.setdefault(requested_date, set()).update(missing)
        return self.prices[requested_date]

    async def availability(
        self, product_codes: Iterable[str], requested_date: date
    ) -> AvailabilityResponse:
        ids = list(dict.fromkeys(product_codes))
        missing = [code for code in ids if code not in self.stock_ids.get(requested_date, set())]
        if missing:
            response = await self.source.availability(missing, requested_date)
            previous = self.stock.get(requested_date)
            self.stock[requested_date] = response.model_copy(
                update={"products": [*(previous.products if previous else []), *response.products]}
            )
            self.stock_ids.setdefault(requested_date, set()).update(missing)
        return self.stock[requested_date]


class UnknownProductError(GatewayError):
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        super().__init__(404, "PRODUCT_NOT_FOUND", f"Unknown catalogue product id: {product_id}.")


class OfferDataError(GatewayError):
    def __init__(self, message: str) -> None:
        super().__init__(502, "OFFER_DATA_INVALID", message)


def _lot(record: SlimRecord) -> Decimal:
    # For fractional lots, the numerator is the smallest lot multiple of whole packs.
    return Decimal(Decimal(str(record.allowed_lot_size or 1)).as_integer_ratio()[0])


def _pack(record: SlimRecord, need_unit: str) -> PackSize | None:
    pack = record.pack_size
    if pack is None:
        pack, _ = resolve_pack_size(
            record.unit_conversions, record.names, record.sales_unit, record.base_unit, record.units
        )
    return pack if pack is not None and pack.unit == need_unit else None


def _allergen_check(record: SlimRecord, allergen: str) -> Check:
    evidence = record.allergens
    if allergen in evidence.contains:
        return "violated"
    if allergen in evidence.may_contain or allergen in evidence.undeclared:
        return "unknown"
    claims = set(record.claims) | set(record.labels)
    if allergen == "lactose" and "low_lactose" in claims:
        return "violated"
    if allergen in evidence.free_from or (
        allergen in {"lactose", "gluten"} and f"{allergen}_free" in claims
    ):
        return "confirmed"
    return "unknown"


def _check(record: SlimRecord, constraint: Constraint) -> Check:
    if constraint.startswith("no_"):
        return _allergen_check(record, constraint[3:])
    if constraint in {"lactose_free", "gluten_free"}:
        return _allergen_check(record, constraint.removesuffix("_free"))
    claims = set(record.claims) | set(record.labels)
    if constraint == "finnish_origin":
        if record.country_of_origin:
            return "confirmed" if record.country_of_origin.casefold() == "fi" else "violated"
        return "confirmed" if "finnish_origin" in claims else "unknown"
    if constraint == "organic":
        # Name-only organic evidence does not establish certification.
        return (
            "confirmed"
            if record.organic.strength == "label" or "organic_label" in claims
            else "unknown"
        )
    if constraint in {"vegan", "vegetarian"}:
        incompatible = _MEAT_ALLERGENS | (
            {"milk", "lactose", "egg"} if constraint == "vegan" else set()
        )
        if incompatible & set(record.allergens.contains):
            return "violated"
        if constraint in claims or "vegan" in claims:
            return "confirmed"
    # No low-carbon threshold has been defined; a carbon number alone proves nothing.
    return "unknown"


def _dietary(record: SlimRecord, body: OffersBody) -> dict[Constraint, Check]:
    keys = list(body.constraints)
    keys.extend(cast(AllergenCheck, f"no_{code}") for code in body.exclude_allergens)
    return {key: _check(record, key) for key in keys}


def _available_date(line: ScheduleLine) -> date | None:
    if line.available_date is None:
        return None
    return datetime.fromisoformat(line.available_date).date()


def _schedule(availability: ProductAvailability) -> ScheduleLine | None:
    # Use the earliest usable schedule; do not sum quantities from different dates.
    candidates = [
        line
        for line in availability.schedule_lines
        if not line.sales_block and line.available_amount > 0
    ]
    return min(candidates, key=lambda line: _available_date(line) or date.max, default=None)


def _deadline(closing_time: str | None, now: datetime) -> datetime | None:
    if closing_time is None:
        return None
    closing = datetime.fromisoformat(closing_time)
    # Valio also sends offset-free local times. Interpret them in the caller's local zone.
    return closing if closing.utcoffset() is not None else closing.replace(tzinfo=now.tzinfo)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _prices(
    price: ProductPrice | None, units: Decimal, unit: str
) -> tuple[float, float, ComparisonPrice, float, float, float, bool]:
    # netPriceFound is false for most products in the Junction API (no customer-specific
    # net price), but catalogPrice is still the confirmed price, so only a missing catalogue
    # price or an error counts as missing.
    missing = price is None or price.error or price.catalog_price is None
    comparison_unit: Literal["KG", "L"] = "L" if unit == "L" else "KG"
    if price is not None and price.comparison_unit in {"KG", "L"}:
        comparison_unit = cast(Literal["KG", "L"], price.comparison_unit)
    if missing:
        # The Offer schema requires numeric price fields, so missing prices use 0.0.
        return 0.0, 0.0, ComparisonPrice(value=0.0, unit=comparison_unit), 0.0, 0.0, 0.0, True
    assert price is not None and price.catalog_price is not None
    net = price.catalog_price.value
    gross = (
        price.price_with_tax.value
        if price.price_with_tax is not None
        else price.catalog_price.value_with_tax
    )
    return (
        float(net),
        float(price.list_price.value) if price.list_price else 0.0,
        ComparisonPrice(
            value=float(price.comparison_price.value) if price.comparison_price else 0.0,
            unit=comparison_unit,
        ),
        float(price.tax.tax_rate) if price.tax else 0.0,
        _money(net * units),
        _money(gross * units),
        False,
    )


def _reason(offer: Offer) -> str:
    facts = []
    if "PRICE_MISSING" not in offer.flags:
        if offer.comparison_price.value > 0:
            facts.append(
                f"{offer.comparison_price.value:g} EUR/{offer.comparison_price.unit.lower()}"
            )
        else:
            facts.append(f"{offer.unit_price:g} EUR/{offer.sales_unit}")
    if offer.country_of_origin:
        facts.append(f"origin {offer.country_of_origin.upper()}")
    if offer.labels:
        facts.append(offer.labels[0].replace("_", " "))
    if "NOT_DELIVERABLE" in offer.flags:
        facts.append("not deliverable")
    elif "PARTIAL" in offer.flags:
        facts.append(
            f"only {offer.quantity_sales_units:g} {offer.sales_unit} available in full lots"
        )
    elif offer.logistic_model == "Terminal":
        facts.append("Terminal stock amount unknown")
    elif offer.available_amount is not None:
        facts.append(f"{offer.available_amount:g} {offer.sales_unit} in stock")
    return ", ".join(facts or ["Price missing"]) + "."


def _build_offer(
    body: OffersBody,
    record: SlimRecord,
    price: ProductPrice | None,
    availability: ProductAvailability,
    now: datetime,
    max_overbuy_percent: float,
    fixed_units: Decimal | None = None,
) -> Offer:
    flags: set[OfferFlag] = set()
    need_unit, factor = _NEED_UNITS[body.unit]
    need = Decimal(str(body.quantity)) * factor
    pack = _pack(record, need_unit)
    lot = _lot(record)
    units = lot
    if pack is None:
        flags.add("PACK_SIZE_UNKNOWN")
    else:
        whole_packs = (need / Decimal(str(pack.value))).to_integral_value(rounding=ROUND_CEILING)
        units = (whole_packs / lot).to_integral_value(rounding=ROUND_CEILING) * lot

    if fixed_units is not None:
        units = fixed_units

    logistic_model = availability.logistic_model
    if logistic_model not in {"Stock", "Terminal"}:
        raise OfferDataError(f"Missing or unsupported logistic model for {record.product_id}.")
    line = _schedule(availability)
    blocked = (
        record.sales_block
        or availability.sales_block
        or (line is None and any(entry.sales_block for entry in availability.schedule_lines))
    )
    if blocked:
        flags.add("SALES_BLOCK")
    available_amount = None
    available_date = None
    amount_line = line or next(iter(availability.schedule_lines), None)
    if logistic_model == "Stock" and amount_line is not None:
        normalized = normalize_amount(logistic_model, amount_line.available_amount)
        if normalized.amount is not None:
            available_amount = float(normalized.amount)
    if not availability.deliverable or record.sales_block or line is None:
        flags.add("NOT_DELIVERABLE")
        if fixed_units is None:
            units = Decimal(0)
    else:
        normalized = normalize_amount(logistic_model, line.available_amount)
        if logistic_model == "Stock" and normalized.amount is not None:
            available_amount = float(normalized.amount)
            if normalized.amount < units:
                if fixed_units is None:
                    units = (normalized.amount / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
                else:
                    flags.add("NOT_DELIVERABLE")
                flags.add("PARTIAL")
        available_date = _available_date(line)
        if units == 0:
            flags.add("NOT_DELIVERABLE")
            available_date = None

    covered = units * Decimal(str(pack.value)) / factor if pack is not None else Decimal(0)
    if covered > Decimal(str(body.quantity)) * (1 + Decimal(str(max_overbuy_percent)) / 100):
        flags.add("OVERBUY")
    if available_date is not None and available_date > body.requested_date:
        flags.add("LATE_DELIVERY")
    closing = _deadline(availability.closing_time, now)
    deadline = closing if closing is not None and closing > now else None
    if closing is not None and deadline is None:
        flags.add("CUTOFF_PASSED")
    if (
        logistic_model == "Terminal"
        and record.delivery_reliability is not None
        and record.delivery_reliability < 0.90
    ):
        flags.add("TERMINAL_LOW_RELIABILITY")
    excluded = set(body.exclude_allergens) | {
        key[3:] for key in body.constraints if key.startswith("no_")
    }
    if excluded and not record.allergens.data_available:
        flags.add("ALLERGEN_DATA_MISSING")
    if excluded & set(record.allergens.may_contain):
        flags.add("MAY_CONTAIN_EXCLUDED")
    unit_price, list_price, comparison, tax, total, gross, missing = _prices(
        price, units, need_unit
    )
    if missing:
        flags.add("PRICE_MISSING")
    sales_unit = record.sales_unit or ""
    description = f"{pack.value:g} {pack.unit.lower()}" if pack else sales_unit
    offer = Offer(
        product_id=record.product_id,
        item_id=body.item_id,
        name=record.names.en or record.names.fi or record.names.sv or record.product_id,
        vendor=record.vendor or "",
        sales_unit=sales_unit,
        pack_description=description,
        pack_size=Quantity(value=pack.value, unit=pack.unit) if pack else None,
        quantity_sales_units=float(units),
        quantity_requested=Quantity(value=body.quantity, unit=body.unit),
        quantity_covered=Quantity(value=float(covered), unit=body.unit),
        unit_price=unit_price,
        list_price=list_price,
        comparison_price=comparison,
        line_total=total,
        line_total_with_tax=gross,
        tax_rate=tax,
        available_amount=available_amount,
        quantity_text=f"{units:g} x {description}" if pack else f"{units:g} {sales_unit}",
        available_date=available_date,
        requested_date=body.requested_date,
        delivery_date=(
            max(body.requested_date, available_date)
            if available_date is not None and "NOT_DELIVERABLE" not in flags
            else None
        ),
        closing_time=closing,
        order_deadline=deadline,
        logistic_model=cast(Literal["Stock", "Terminal"], logistic_model),
        delivery_reliability=record.delivery_reliability,
        carbon_kg_co2e_per_kg=record.carbon_kg_co2e_per_kg,
        country_of_origin=record.country_of_origin or "",
        labels=list(record.labels),
        allergens=Allergens(
            contains=list(record.allergens.contains),
            may_contain=list(record.allergens.may_contain),
            data_available=record.allergens.data_available,
        ),
        dietary_check=_dietary(record, body),
        tier=None,
        score=0.0,
        reason="",
        is_split=False,
        is_substitute=False,
        substitute_for=None,
        substitute_source=None,
        flags=sorted(flags),
    )
    offer.reason = _reason(offer)
    return offer


async def build_offers(
    body: OffersBody,
    *,
    catalog: Catalog,
    valio: OfferValio,
    now: datetime,
    max_overbuy_percent: float = 25,
) -> list[Offer]:
    """Fetch the shortlist once per endpoint, preserving input order and catalogue evidence.

    ``now`` must be aware and use the local zone of offset-free Valio closing times.
    Transport errors propagate from the client; per-product missing prices become flags.
    """
    if not isfinite(body.quantity) or body.quantity <= 0:
        raise ValueError("Requested quantity must be positive and finite.")
    if not isfinite(max_overbuy_percent) or max_overbuy_percent < 0:
        raise ValueError("Maximum overbuy percent must be finite and nonnegative.")
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    records = []
    for product_id in body.product_ids:
        record = catalog.lookup(product_id)
        if record is None:
            raise UnknownProductError(product_id)
        records.append(record)
    if not records:
        return []
    prices = await valio.price(body.product_ids, body.requested_date)
    availability = await valio.availability(body.product_ids, body.requested_date)
    prices_by_id = {entry.product_id: entry for entry in prices.products}
    availability_by_id = {entry.product_id: entry for entry in availability.products}
    offers = []
    for record in records:
        stock = availability_by_id.get(record.product_id)
        if stock is None:
            raise OfferDataError(f"Missing availability for {record.product_id}.")
        offers.append(
            _build_offer(
                body, record, prices_by_id.get(record.product_id), stock, now, max_overbuy_percent
            )
        )
    return offers


async def price_fixed_lines(
    lines: Sequence[QuoteLine],
    *,
    buyer_id: str,
    agent_id: str,
    requested_date: date,
    catalog: Catalog,
    valio: OfferValio,
    now: datetime,
    needs: Mapping[str, Quantity] | None = None,
    item_context: Mapping[str, "ItemContext"] | None = None,
    max_overbuy_percent: float = 25,
    refresh: bool = False,
    on_skip: Callable[[str, str, str], None] | None = None,
) -> list[Offer]:
    """Price the caller's exact sales units; shortage is evidence, never a quantity edit.

    Shared item needs are required when split lines cover one original need.
    Without a need, each line's requested quantity equals its covered quantity.
    """
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    if not isfinite(max_overbuy_percent) or max_overbuy_percent < 0:
        raise ValueError("Maximum overbuy percent must be finite and nonnegative.")
    records = []
    for line in lines:
        if line.substitute_source == "valio":
            original = catalog.lookup(line.substitute_for or "")
            if original is None or not any(
                rule.product == line.substitute_for and rule.substitute_product == line.product_id
                for rule in original.substitutions
            ):
                raise GatewayError(422, "VALIDATION_ERROR", "No matching Valio substitution.")
        record = catalog.lookup(line.product_id)
        if record is None:
            raise UnknownProductError(line.product_id)
        units = Decimal(str(line.quantity_sales_units))
        if not units.is_finite() or units <= 0 or units % _lot(record):
            raise ValueError("Sales units must be positive, finite and a whole lot multiple.")
        records.append(record)
    if not lines:
        return []
    codes = list(dict.fromkeys(line.product_id for line in lines))
    if refresh and isinstance(valio, ValioClient):
        prices = await valio.price(codes, requested_date, refresh=True)
        availability = await valio.availability(codes, requested_date, refresh=True)
    else:
        prices = await valio.price(codes, requested_date)
        availability = await valio.availability(codes, requested_date)
    prices_by_id = {entry.product_id: entry for entry in prices.products}
    stock_by_id = {entry.product_id: entry for entry in availability.products}
    inferred: dict[str, Quantity] = {}
    for line, record in zip(lines, records, strict=True):
        if needs and line.item_id in needs:
            continue
        pack = next((pack for unit in ("kg", "L", "pcs") if (pack := _pack(record, unit))), None)
        amount = (
            Decimal(str(pack.value)) * Decimal(str(line.quantity_sales_units))
            if pack
            else Decimal(0)
        )
        unit = pack.unit if pack else "pcs"
        previous = inferred.get(line.item_id)
        if previous is not None and previous.unit != unit:
            raise GatewayError(422, "VALIDATION_ERROR", "Split lines need compatible units.")
        inferred[line.item_id] = Quantity(
            value=float(amount + (Decimal(str(previous.value)) if previous else Decimal(0))),
            unit=unit,
        )
    result = []
    seen_items: set[str] = set()
    for line, record in zip(lines, records, strict=True):
        stock = stock_by_id.get(line.product_id)
        if stock is None:
            if on_skip is not None:
                on_skip(line.item_id, line.product_id, "OFFER_DATA_INVALID")
                continue
            raise OfferDataError(f"Missing availability for {line.product_id}.")
        need = (needs or {}).get(line.item_id) or inferred[line.item_id]
        if not isfinite(need.value) or need.value < 0:
            raise ValueError("Item need must be finite and nonnegative.")
        context = (item_context or {}).get(line.item_id, ItemContext())
        body = OffersBody(
            buyer_id=buyer_id,
            agent_id=agent_id,
            product_ids=[line.product_id],
            item_id=line.item_id,
            quantity=need.value,
            unit=need.unit,
            requested_date=requested_date,
            constraints=list(dict.fromkeys([*context.constraints, *context.required_preferences])),
            exclude_allergens=list(context.exclude_allergens),
        )
        try:
            offer = _build_offer(
                body,
                record,
                prices_by_id.get(line.product_id),
                stock,
                now,
                max_overbuy_percent,
                Decimal(str(line.quantity_sales_units)),
            )
        except (GatewayError, ValueError, ArithmeticError) as exc:
            if on_skip is None:
                raise
            on_skip(
                line.item_id,
                line.product_id,
                exc.code if isinstance(exc, GatewayError) else "OFFER_DATA_INVALID",
            )
            continue
        result.append(offer)
        result[-1].is_split = line.item_id in seen_items
        result[-1].is_substitute = line.substitute_for is not None
        result[-1].substitute_for = line.substitute_for
        result[-1].substitute_source = line.substitute_source
        seen_items.add(line.item_id)
    return result
