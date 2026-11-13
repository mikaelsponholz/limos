"""Explicit label evidence, dietary claims and storage classes."""

import re

from gateway.catalog.models import (
    ClaimCode,
    LabelCode,
    LocalizedText,
    OrganicEvidence,
    StorageClass,
)
from gateway.catalog.sanitize import classifications as clean_classifications

FINNISH_LABELS = {
    "GOODS_FROM_FINLAND_BLUE_SWAN",
    "MADE_IN_FINLAND_FLAG_WITH_KEY",
    "FINNISH_HEART_SYMBOL",
    "PUHTAASTI_KOTIMAINEN",
}
ORGANIC_LABELS = {
    "EU_ORGANIC_FARMING",
    "KRAV_MARK",
    "OFFICIAL_ECO_LABEL_SUN",
    "SOIL_ASSOCIATION_ORGANIC_SYMBOL",
}
CLAIMS: dict[str, ClaimCode] = {
    "FREE_FROM_LACTOSE": "lactose_free",
    "LOW_LACTOSE": "low_lactose",
    "FREE_FROM_GLUTEN": "gluten_free",
    "VEGAN": "vegan",
    "VEGETARIAN": "vegetarian",
}
OTHER_LABELS: dict[str, LabelCode] = {
    "MARINE_STEWARDSHIP_COUNCIL_LABEL": "msc",
    "MSC": "msc",
    "FAIR_TRADE_MARK": "fair_trade",
    "FAIRTRADE": "fair_trade",
    "RAINFOREST_ALLIANCE": "rainforest_alliance",
    "NORDIC_SWAN": "nordic_swan",
}
TEMPERATURE_LABELS: dict[str, StorageClass] = {
    "warmTemperature": "ambient",
    "coolTemperature": "chilled",
    "freezeTemperature": "frozen",
}


def classification_codes(classifications: object, name: str) -> set[str]:
    return {
        entry.get("synkkaId") or entry["id"]
        for group in clean_classifications(classifications)
        if group["name"] == name
        for entry in group["values"]
    }


def finnish_labels(classifications: object) -> list[str]:
    return sorted(classification_codes(classifications, "packagingMarkedLabel") & FINNISH_LABELS)


def organic_evidence(classifications: object, names: LocalizedText) -> OrganicEvidence:
    codes = sorted(classification_codes(classifications, "packagingMarkedLabel") & ORGANIC_LABELS)
    if codes:
        return OrganicEvidence(strength="label", label_codes=codes)
    if any(
        re.search(r"\bluomu\b", name, re.IGNORECASE) for name in names.model_dump().values() if name
    ):
        return OrganicEvidence(strength="name_only")
    return OrganicEvidence()


def dietary_claims(classifications: object) -> list[ClaimCode]:
    codes = classification_codes(classifications, "nutritionalClaim")
    return sorted({CLAIMS[code] for code in codes if code in CLAIMS})


def product_labels(
    classifications: object, names: LocalizedText, country_of_origin: str | None
) -> list[LabelCode]:
    labels: set[LabelCode] = set()
    if finnish_labels(classifications):
        labels.add("finnish_label")
    if country_of_origin == "fi":
        labels.add("finnish_origin")
    organic = organic_evidence(classifications, names)
    if organic.strength == "label":
        labels.add("organic_label")
    elif organic.strength == "name_only":
        labels.add("organic_name")
    labels.update(dietary_claims(classifications))
    labels.update(
        OTHER_LABELS[code]
        for code in classification_codes(classifications, "packagingMarkedLabel")
        if code in OTHER_LABELS
    )
    return sorted(labels)


def storage_class(
    classifications: object, temperature_condition: int | None = None
) -> StorageClass:
    # The supplied schema explicitly leaves integer temperatureCondition meanings undefined.
    # Preserve that code in SlimRecord; do not invent a temperature-code mapping here.
    classes = {
        TEMPERATURE_LABELS[code]
        for code in classification_codes(classifications, "packagingMarkedLabel")
        if code in TEMPERATURE_LABELS
    }
    return next(iter(classes)) if len(classes) == 1 else "unknown"
