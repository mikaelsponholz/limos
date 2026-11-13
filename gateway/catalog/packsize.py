"""Resolve net contents without prices. Phase 1 may use a price-ratio fallback."""

import re
from collections.abc import Sequence
from decimal import Decimal, DecimalException

from pydantic import ValidationError

from gateway.catalog.models import (
    LocalizedText,
    PackagingUnit,
    PackSize,
    PackSource,
    Quantity,
    UnitConversion,
)

_NAME_SIZE = re.compile(
    r"(?<![\w.,+\-])(?:(?P<count>\d+)\s*[x×]\s*)?"
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g|ml|l|pcs|kpl)\b",
    re.IGNORECASE,
)
_UNITS: dict[str, tuple[str, Decimal]] = {
    "kg": ("kg", Decimal(1)),
    "kgm": ("kg", Decimal(1)),
    "g": ("kg", Decimal("0.001")),
    "grm": ("kg", Decimal("0.001")),
    "l": ("L", Decimal(1)),
    "ltr": ("L", Decimal(1)),
    "ml": ("L", Decimal("0.001")),
    "mlt": ("L", Decimal("0.001")),
    "pcs": ("pcs", Decimal(1)),
    "kpl": ("pcs", Decimal(1)),
}


def normalize_quantity(quantity: Quantity, multiplier: float = 1) -> PackSize | None:
    conversion = _UNITS.get(quantity.unit.strip().casefold())
    if conversion is None:
        return None
    unit, factor = conversion
    try:
        value = Decimal(str(quantity.value)) * factor * Decimal(str(multiplier))
        return PackSize.model_validate({"value": float(value), "unit": unit})
    except (DecimalException, ValueError, OverflowError):
        return None


def pack_size_from_name(name: object) -> PackSize | None:
    if not isinstance(name, str):
        return None
    matches = list(_NAME_SIZE.finditer(name))
    if len(matches) != 1:
        return None
    match = matches[0]
    # A chain such as 2 x 6 x 1 L is outside the supported single-multiplier grammar.
    if re.search(r"[x×]\s*$", name[: match.start()], re.IGNORECASE):
        return None
    try:
        value = Decimal(match["value"].replace(",", ".")) * Decimal(match["count"] or "1")
        if value <= 0:
            return None
        return normalize_quantity(Quantity(value=float(value), unit=match["unit"]))
    except (DecimalException, ValidationError, OverflowError):
        return None


def pack_size_from_conversions(
    conversions: list[UnitConversion],
    sales_unit: str | None = None,
    base_unit: str | None = None,
    units: Sequence[PackagingUnit] = (),
) -> PackSize | None:
    """Use consumer/base net contents, scaled by the known sales/base hierarchy.

    Technical sales-unit codes have no published packaging-level translations. Never
    choose an arbitrary CASE or PALLET, or apply consumer contents to an unknown case.
    With no unit context, this function resolves the consumer/base packaging only.
    """
    multiplier = 1.0
    if sales_unit is not None and sales_unit != base_unit:
        sizes = {unit.size_in_base_units for unit in units if unit.unit_id == sales_unit}
        if len(sizes) != 1:
            return None
        multiplier = next(iter(sizes))
    base_levels = [c for c in conversions if c.packaging_type == "BASE_UNIT_OR_EACH"]
    for attribute in ("net_weight", "volume"):
        sizes_found = set()
        for level in base_levels:
            quantity = getattr(level, attribute)
            if quantity is not None:
                size = normalize_quantity(quantity, multiplier)
                if size is not None:
                    sizes_found.add((size.value, size.unit))
        if len(sizes_found) == 1:
            value, unit = next(iter(sizes_found))
            return PackSize(value=value, unit=unit)
        if sizes_found:
            return None
    return None


def resolve_pack_size(
    conversions: list[UnitConversion],
    names: LocalizedText,
    sales_unit: str | None = None,
    base_unit: str | None = None,
    units: Sequence[PackagingUnit] = (),
) -> tuple[PackSize | None, PackSource]:
    size = pack_size_from_conversions(conversions, sales_unit, base_unit, units)
    if size is not None:
        return size, "unit_conversions"
    for name in (names.fi, names.en, names.sv):
        if name and (size := pack_size_from_name(name)) is not None:
            return size, "name"
    # Runtime offers can try catalogPrice / comparisonPrice here in Phase 1.
    return None, "unknown"
