from typing import Any

import pytest

from gateway.catalog.allergens import extract_allergens


def group(code: str, status: str, name: str = "allergen") -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "type": "ENUM",
            "values": [{"id": "internal-feature", "synkkaId": code, "unit": status}],
        }
    ]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AM", ["milk"]),
        ("ML", ["lactose"]),
        ("AW", ["gluten"]),
        ("UW", ["gluten", "wheat"]),
        ("GB", ["gluten"]),
        ("NR", ["gluten"]),
        ("GO", ["gluten"]),
        ("ST", ["gluten"]),
        ("AE", ["egg"]),
        ("AF", ["fish"]),
        ("AC", ["crustaceans"]),
        ("UM", ["molluscs"]),
        ("AP", ["peanuts"]),
        ("AN", ["tree_nuts"]),
        ("AY", ["soy"]),
        ("AS", ["sesame"]),
        ("BC", ["celery"]),
        ("BM", ["mustard"]),
        ("AU", ["sulphites"]),
        ("NL", ["lupin"]),
    ],
)
def test_gs1_codes(code: str, expected: list[str]) -> None:
    result = extract_allergens(group(code, "CONTAINS"))
    assert result.contains == expected
    assert result.data_available is True
    assert result.unknown_codes == []


@pytest.mark.parametrize(
    ("status", "field"),
    [
        ("CONTAINS", "contains"),
        ("MAY_CONTAIN", "may_contain"),
        ("UNDECLARED", "undeclared"),
        ("FREE_FROM", "free_from"),
    ],
)
def test_statuses_are_distinct(status: str, field: str) -> None:
    result = extract_allergens(
        group("AM", status, "nonAllergen" if status == "FREE_FROM" else "allergen")
    )
    for bucket in ("contains", "may_contain", "undeclared", "free_from"):
        assert getattr(result, bucket) == (["milk"] if bucket == field else [])


@pytest.mark.parametrize(
    ("code", "specific"),
    [
        ("SA", "almond"),
        ("SH", "hazelnut"),
        ("SW", "walnut"),
        ("SC", "cashew"),
        ("SP", "pecan"),
        ("SR", "brazil_nut"),
        ("SM", "macadamia"),
    ],
)
def test_specific_nut_is_preserved_and_rolled_up(code: str, specific: str) -> None:
    result = extract_allergens(group(code, "MAY_CONTAIN"))
    assert result.may_contain == ["tree_nuts"]
    assert result.evidence[0].code == code
    assert result.evidence[0].specific == specific


@pytest.mark.parametrize("code", ["SA", "GB", "UW"])
def test_free_from_subtype_does_not_claim_parent_safety(code: str) -> None:
    result = extract_allergens(group(code, "FREE_FROM", "nonAllergen"))
    assert "tree_nuts" not in result.free_from
    assert "gluten" not in result.free_from
    assert result.evidence[0].status == "FREE_FROM"
    if code == "UW":
        assert result.free_from == ["wheat"]


def test_unknown_codes_and_statuses_are_retained() -> None:
    result = extract_allergens(group("TEST-ALLERGEN", "MAY_CONTAIN") + group("AM", "TEST-STATUS"))
    assert result.unknown_codes == ["TEST-ALLERGEN"]
    assert result.unknown_statuses == ["TEST-STATUS"]
    assert [(e.code, e.status) for e in result.evidence] == [
        ("AM", "TEST-STATUS"),
        ("TEST-ALLERGEN", "MAY_CONTAIN"),
    ]
    assert result.contains == []
    assert result.data_available is True


def test_missing_and_empty_data_is_not_free_from() -> None:
    for classifications in (
        [],
        [{"name": "allergen", "values": []}],
        [{"name": "nonAllergen", "values": []}],
    ):
        result = extract_allergens(classifications)
        assert result.data_available is False
        assert result.free_from == []
        assert result.contains == []


def test_id_fallback_and_absent_status_stays_undeclared() -> None:
    result = extract_allergens([{"name": "nonAllergen", "values": [{"id": "AM"}]}])
    assert result.undeclared == ["milk"]
    assert result.free_from == []


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        "garbage",
        [None],
        [{"name": "nonAllergen", "values": None}],
        [{"name": "allergen", "values": [{"id": []}]}],
    ],
)
def test_malformed_allergen_data_stays_missing(data: Any) -> None:
    result = extract_allergens(data)
    assert result.data_available is False
    assert result.free_from == result.contains == result.evidence == []


@pytest.mark.parametrize("status", [None, [], {}, 42, ""])
def test_invalid_status_cannot_establish_safety(status: Any) -> None:
    result = extract_allergens([{"name": "nonAllergen", "values": [{"id": "AM", "unit": status}]}])
    assert result.undeclared == ["milk"]
    assert result.free_from == []
    assert result.evidence[0].status == "UNDECLARED"
