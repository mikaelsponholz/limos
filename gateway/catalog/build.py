"""Pure transformation of a raw product and CSV lookup maps into catalogue facts."""

import re
from collections.abc import Mapping
from typing import Any

from gateway.catalog.allergens import extract_allergens
from gateway.catalog.labels import (
    dietary_claims,
    finnish_labels,
    organic_evidence,
    product_labels,
    storage_class,
)
from gateway.catalog.models import (
    LocalizedText,
    PackagingUnit,
    Quantity,
    SlimRecord,
    Substitution,
    UnitConversion,
)
from gateway.catalog.packsize import resolve_pack_size
from gateway.catalog.sanitize import Counts, classifications, degraded, items, number, text


def localized_text(
    entries: object, counts: Counts | None = None, kind: str = "names"
) -> LocalizedText:
    values: dict[str, str] = {}
    valid = []
    for entry in items(entries, kind, counts):
        language, value = text(entry.get("language")), text(entry.get("value"))
        if language is None or value is None:
            degraded(counts, kind)
        elif language in {"fi", "en", "sv"}:
            valid.append((language, value))
    for language, value in sorted(valid):
        values.setdefault(language, value)
    return LocalizedText.model_validate(values)


def build_record(
    raw: dict[str, Any],
    reliability: Mapping[str, Any],
    carbon: Mapping[str, Any],
    *,
    degradations: Counts | None = None,
) -> SlimRecord:
    product_id = text(raw.get("id"))
    if product_id is None:
        raise ValueError("missing_product_code")

    def optional_text(data: dict[str, Any], key: str) -> str | None:
        value = text(data.get(key))
        if key in data and value is None:
            degraded(degradations, key)
        return value

    def optional_number(
        data: Mapping[str, Any],
        key: str,
        kind: str,
        *,
        positive: bool = False,
        maximum: float | None = None,
    ) -> float | None:
        value = number(data.get(key), positive=positive, maximum=maximum)
        if key in data and value is None:
            degraded(degradations, kind)
        return value

    def optional_bool(data: dict[str, Any], key: str) -> bool | None:
        value = data.get(key)
        if type(value) is bool:
            return value
        if key in data:
            degraded(degradations, key)
        return None

    synkka = raw.get("synkkaData", {})
    if not isinstance(synkka, dict):
        degraded(degradations, "synkkaData")
        synkka = {}
    groups = classifications(synkka.get("classifications", []), degradations)
    names = localized_text(synkka.get("names", []), degradations)
    units = []
    for unit in items(raw.get("units", []), "units", degradations):
        unit_id = text(unit.get("unitId"))
        unit_size = number(unit.get("sizeInBaseUnits"), positive=True)
        if unit_id is None or unit_size is None:
            degraded(degradations, "units")
            continue
        units.append(PackagingUnit(unitId=unit_id, sizeInBaseUnits=unit_size))
    units.sort(key=lambda u: (u.unit_id, u.size_in_base_units))

    def quantity(value: object, kind: str) -> Quantity | None:
        if isinstance(value, dict):
            amount, unit = number(value.get("value"), positive=True), text(value.get("unit"))
            supported = (
                {"kg", "kgm", "g", "grm"}
                if kind.endswith("netWeight")
                else {"l", "ltr", "ml", "mlt"}
            )
            if amount is not None and unit is not None and unit.casefold() in supported:
                return Quantity(value=amount, unit=unit)
        degraded(degradations, kind)
        return None

    conversions = []
    for conversion in items(synkka.get("unitConversions", []), "unitConversions", degradations):
        packaging = text(conversion.get("synkkaPackagingType"))
        if packaging is None:
            degraded(degradations, "unitConversions.packagingType")
            continue
        quantities = {
            key: quantity(conversion[key], f"unitConversions.{key}")
            for key in ("netWeight", "volume")
            if key in conversion
        }
        conversions.append(UnitConversion(synkkaPackagingType=packaging, **quantities))
    conversions.sort(key=lambda c: c.model_dump_json())
    sales_unit, base_unit = optional_text(raw, "salesUnit"), optional_text(raw, "baseUnit")
    size, source = resolve_pack_size(conversions, names, sales_unit, base_unit, units)
    country = optional_text(raw, "countryOfOrigin")
    if country is not None and re.fullmatch(r"[a-z]{2}", country) is None:
        degraded(degradations, "countryOfOrigin")
        country = None
    substitutions = []
    for entry in items(raw.get("substitutions", []), "substitutions", degradations):
        product, substitute = text(entry.get("product")), text(entry.get("substituteProduct"))
        if product is None or substitute is None:
            degraded(degradations, "substitutions")
            continue
        substitutions.append(Substitution(product=product, substituteProduct=substitute))
    temperature = raw.get("temperatureCondition")
    if type(temperature) is not int:
        if "temperatureCondition" in raw:
            degraded(degradations, "temperatureCondition")
        temperature = None
    return SlimRecord(
        product_id=product_id,
        names=names,
        vendor=optional_text(raw, "vendorName"),
        key_ingredients=localized_text(
            synkka.get("keyIngredients", []), degradations, "keyIngredients"
        ),
        country_of_origin=country,
        labels=product_labels(groups, names, country),
        finnish_label_codes=finnish_labels(groups),
        organic=organic_evidence(groups, names),
        allergens=extract_allergens(groups),
        claims=dietary_claims(groups),
        sales_unit=sales_unit,
        base_unit=base_unit,
        units=units,
        unit_conversions=conversions,
        allowedLotSize=optional_number(raw, "allowedLotSize", "allowedLotSize", positive=True),
        pack_size=size,
        pack_size_source=source,
        variable_unit=optional_bool(synkka, "variableUnit"),
        storage_class=storage_class(groups, temperature),
        temperature_condition=temperature,
        sales_block=optional_bool(raw, "salesBlock"),
        substitutions=sorted(substitutions, key=lambda s: (s.product, s.substitute_product)),
        category=optional_text(raw, "category"),
        delivery_reliability=optional_number(
            reliability, product_id, "deliveryReliability", maximum=1
        ),
        carbon_kg_co2e_per_kg=optional_number(carbon, product_id, "carbon"),
    )
