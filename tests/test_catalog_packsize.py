from collections import Counter
from typing import Any

import pytest

from gateway.catalog.build import build_record
from gateway.catalog.models import LocalizedText, PackagingUnit, Quantity, UnitConversion
from gateway.catalog.packsize import pack_size_from_name, resolve_pack_size


@pytest.mark.parametrize("field", ["netWeight", "volume"])
@pytest.mark.parametrize("bad", [0, -1, "garbage", None, True, float("nan"), float("inf")])
def test_invalid_quantity_falls_back(field: str, bad: Any) -> None:
    counts: Counter[str] = Counter()
    raw = {
        "id": "TEST-QUANTITY",
        "synkkaData": {
            "names": [{"language": "fi", "value": "Invented parcel 3 kg"}],
            "unitConversions": [
                {"synkkaPackagingType": "BASE_UNIT_OR_EACH", field: {"value": bad, "unit": "KGM"}}
            ],
        },
    }
    record = build_record(raw, {}, {}, degradations=counts)
    assert record.pack_size is not None and record.pack_size.value == 3
    assert record.pack_size_source == "name"
    assert counts == {f"unitConversions.{field}": 1}
    raw["synkkaData"]["names"] = []
    record = build_record(raw, {}, {})
    assert record.pack_size is None and record.pack_size_source == "unknown"


@pytest.mark.parametrize(
    "name", [None, {}, "Invented -2 kg", "Invented 0 x 2 L", "Invented " + "9" * 1000 + " kg"]
)
def test_invalid_name_sizes_do_not_crash_or_become_positive(name: Any) -> None:
    assert pack_size_from_name(name) is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "garbage",
        {},
        {"value": 1},
        {"value": 1, "unit": []},
        {"value": 1, "unit": ""},
        {"value": 1, "unit": "TEST-UNKNOWN"},
    ],
)
def test_malformed_quantity_is_missing_but_valid_sibling_survives(bad: Any) -> None:
    counts: Counter[str] = Counter()
    record = build_record(
        {
            "id": "TEST-SIBLING",
            "synkkaData": {
                "unitConversions": [
                    {
                        "synkkaPackagingType": "BASE_UNIT_OR_EACH",
                        "netWeight": bad,
                        "volume": {"value": 2, "unit": "LTR"},
                    }
                ]
            },
        },
        {},
        {},
        degradations=counts,
    )
    assert record.unit_conversions[0].net_weight is None
    assert record.pack_size is not None
    assert (record.pack_size.value, record.pack_size.unit) == (2, "L")
    assert record.pack_size_source == "unit_conversions"
    assert counts == {"unitConversions.netWeight": 1}


def test_overflow_and_underflow_from_scaling_are_unknown() -> None:
    for amount, multiplier in [(1e308, 1e308), (5e-324, 5e-324)]:
        conversion = UnitConversion(
            synkkaPackagingType="BASE_UNIT_OR_EACH", netWeight=Quantity(value=amount, unit="GRM")
        )
        assert resolve_pack_size(
            [conversion],
            LocalizedText(),
            "TEST-CASE",
            "TEST-EACH",
            [PackagingUnit(unitId="TEST-CASE", sizeInBaseUnits=multiplier)],
        ) == (None, "unknown")


@pytest.mark.parametrize(
    ("name", "value", "unit"),
    [
        ("Invented parcel 1 kg", 1, "kg"),
        ("Invented parcel 500 g", 0.5, "kg"),
        ("Invented parcel 2,5 l", 2.5, "L"),
        ("Invented parcel 6x1 l", 6, "L"),
        ("Invented parcel 10 x 200 g", 2, "kg"),
        ("Invented parcel 2 × 1,25 L", 2.5, "L"),
        ("Invented parcel 250 ml", 0.25, "L"),
        ("Invented parcel 12 pcs", 12, "pcs"),
        ("Invented parcel 2.5 KG", 2.5, "kg"),
    ],
)
def test_name_patterns(name: str, value: float, unit: str) -> None:
    size, source = resolve_pack_size([], LocalizedText(fi=name))
    assert size is not None
    assert size.value == value
    assert size.unit == unit
    assert source == "name"


@pytest.mark.parametrize(
    "name",
    [
        "Invented parcel",
        "Invented 500",
        "Invented 0 kg",
        "Invented 1 kg + 500 g",
        "Invented 2 x 6 x 1 L",
    ],
)
def test_unknown_or_ambiguous_name(name: str) -> None:
    assert pack_size_from_name(name) is None
    assert resolve_pack_size([], LocalizedText(fi=name)) == (None, "unknown")


@pytest.mark.parametrize(
    ("field", "value", "unit", "expected", "expected_unit"),
    [
        ("netWeight", 750, "GRM", 0.75, "kg"),
        ("netWeight", 2.5, "KGM", 2.5, "kg"),
        ("volume", 750, "MLT", 0.75, "L"),
        ("volume", 2.5, "LTR", 2.5, "L"),
    ],
)
def test_conversions_precede_name(
    field: str, value: float, unit: str, expected: float, expected_unit: str
) -> None:
    conversion = UnitConversion.model_validate(
        {"synkkaPackagingType": "BASE_UNIT_OR_EACH", field: {"value": value, "unit": unit}}
    )
    size, source = resolve_pack_size([conversion], LocalizedText(fi="Invented parcel 9 kg"))
    assert size is not None
    assert (size.value, size.unit, source) == (expected, expected_unit, "unit_conversions")


def test_sales_hierarchy_scales_consumer_contents_and_ignores_pallet() -> None:
    base = UnitConversion(
        synkkaPackagingType="BASE_UNIT_OR_EACH", netWeight=Quantity(value=250, unit="GRM")
    )
    pallet = UnitConversion(synkkaPackagingType="PALLET", netWeight=Quantity(value=400, unit="KGM"))
    size, source = resolve_pack_size(
        [pallet, base],
        LocalizedText(),
        "TEST-CASE",
        "TEST-EACH",
        [PackagingUnit(unitId="TEST-CASE", sizeInBaseUnits=8)],
    )
    assert size is not None
    assert (size.value, size.unit, source) == (2, "kg", "unit_conversions")
    assert resolve_pack_size([pallet, base], LocalizedText(), "TEST-CASE", "TEST-EACH") == (
        None,
        "unknown",
    )
    assert resolve_pack_size([pallet], LocalizedText()) == (None, "unknown")


def test_gross_weight_is_not_used_and_unknown_units_fall_back_to_name() -> None:
    gross_only = UnitConversion(synkkaPackagingType="BASE_UNIT_OR_EACH")
    unknown = UnitConversion(
        synkkaPackagingType="BASE_UNIT_OR_EACH", netWeight=Quantity(value=5, unit="TEST-UNIT")
    )
    assert resolve_pack_size([gross_only], LocalizedText()) == (None, "unknown")
    size, source = resolve_pack_size([unknown], LocalizedText(en="Invented parcel 1 L"))
    assert size is not None
    assert (size.value, size.unit, source) == (1, "L", "name")


def test_net_weight_precedes_volume_and_conflicting_base_weights_are_unknown() -> None:
    conversion = UnitConversion(
        synkkaPackagingType="BASE_UNIT_OR_EACH",
        netWeight=Quantity(value=2, unit="KGM"),
        volume=Quantity(value=3, unit="LTR"),
    )
    size, _ = resolve_pack_size([conversion], LocalizedText())
    assert size is not None and size.value == 2 and size.unit == "kg"
    other = conversion.model_copy(update={"net_weight": Quantity(value=4, unit="KGM")})
    assert resolve_pack_size([conversion, other], LocalizedText()) == (None, "unknown")
