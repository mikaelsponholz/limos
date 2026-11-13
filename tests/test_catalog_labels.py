from typing import Any

import pytest

from gateway.catalog.labels import (
    dietary_claims,
    finnish_labels,
    organic_evidence,
    product_labels,
    storage_class,
)
from gateway.catalog.models import LocalizedText


def classifications(*codes: str, name: str = "packagingMarkedLabel") -> list[dict[str, Any]]:
    return [{"name": name, "values": [{"id": code} for code in codes]}]


@pytest.mark.parametrize(
    "code",
    [
        "GOODS_FROM_FINLAND_BLUE_SWAN",
        "MADE_IN_FINLAND_FLAG_WITH_KEY",
        "FINNISH_HEART_SYMBOL",
        "PUHTAASTI_KOTIMAINEN",
    ],
)
def test_finnish_label_evidence_is_distinct_from_origin(code: str) -> None:
    data = classifications(code)
    assert finnish_labels(data) == [code]
    assert product_labels(data, LocalizedText(fi="Keksitty tuote"), "se") == ["finnish_label"]
    assert product_labels([], LocalizedText(), "fi") == ["finnish_origin"]


@pytest.mark.parametrize(
    "code",
    [
        "EU_ORGANIC_FARMING",
        "KRAV_MARK",
        "OFFICIAL_ECO_LABEL_SUN",
        "SOIL_ASSOCIATION_ORGANIC_SYMBOL",
    ],
)
def test_certified_organic_beats_name(code: str) -> None:
    data = classifications(code)
    names = LocalizedText(fi="Keksitty luomu kasvis")
    evidence = organic_evidence(data, names)
    assert evidence.strength == "label"
    assert evidence.label_codes == [code]
    assert product_labels(data, names, None) == ["organic_label"]


def test_name_only_organic_is_weaker_and_unknown_is_not_denial() -> None:
    names = LocalizedText(fi="Keksitty LUOMU kasvis")
    assert organic_evidence([], names).strength == "name_only"
    assert organic_evidence([], names).label_codes == []
    assert product_labels([], names, None) == ["organic_name"]
    for name in ("Keksitty kasvis", "Eiluomuinen keksintö"):
        assert organic_evidence([], LocalizedText(fi=name)).strength == "unknown"


def test_claim_codes_and_low_lactose_is_not_lactose_free() -> None:
    low = classifications("LOW_LACTOSE", name="nutritionalClaim")
    assert dietary_claims(low) == ["low_lactose"]
    assert product_labels(low, LocalizedText(), None) == ["low_lactose"]
    data = classifications(
        "FREE_FROM_LACTOSE", "FREE_FROM_GLUTEN", "VEGAN", "VEGETARIAN", name="nutritionalClaim"
    )
    assert dietary_claims(data) == ["gluten_free", "lactose_free", "vegan", "vegetarian"]
    assert dietary_claims(classifications("FREE_FROM_LACTOSE")) == []
    assert dietary_claims([]) == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("warmTemperature", "ambient"),
        ("coolTemperature", "chilled"),
        ("freezeTemperature", "frozen"),
        ("TEST-TEMP", "unknown"),
    ],
)
def test_storage_labels(code: str, expected: str) -> None:
    assert storage_class(classifications(code), 731) == expected


def test_unknown_integer_and_conflicting_temperature_labels() -> None:
    assert storage_class([], 731) == "unknown"
    assert storage_class([]) == "unknown"
    assert storage_class(classifications("coolTemperature", "freezeTemperature")) == "unknown"


def test_synkka_label_id_is_used() -> None:
    data = [
        {
            "name": "packagingMarkedLabel",
            "values": [{"id": "internal-feature", "synkkaId": "EU_ORGANIC_FARMING"}],
        }
    ]
    assert product_labels(data, LocalizedText(), None) == ["organic_label"]


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        "garbage",
        [None],
        [{"name": [], "values": []}],
        [{"name": "packagingMarkedLabel", "values": None}],
        [{"name": "nutritionalClaim", "values": [{"id": {}}]}],
    ],
)
def test_malformed_classifications_have_no_label_evidence(data: Any) -> None:
    assert product_labels(data, LocalizedText(), None) == []
    assert dietary_claims(data) == []
    assert organic_evidence(data, LocalizedText()).strength == "unknown"
    assert storage_class(data) == "unknown"
