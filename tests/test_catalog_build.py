import json
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from gateway.catalog.build import build_record
from gateway.catalog.index import Catalog
from scripts.build_catalog import SOURCE_NAMES, build_catalog, read_metric


def invented_product(code: str = "TEST-CATALOG-B") -> dict[str, Any]:
    return {
        "id": code,
        "allowedLotSize": 0.5,
        "salesUnit": "TEST-EACH",
        "baseUnit": "TEST-EACH",
        "category": "TEST-CATEGORY",
        "deleted": False,
        "deposits": [],
        "vendorName": "Invented supplier",
        "countryOfOrigin": "fi",
        "temperatureCondition": 731,
        "units": [{"unitId": "TEST-EACH", "sizeInBaseUnits": 1}],
        "substitutions": [{"product": code, "substituteProduct": "TEST-REPLACEMENT"}],
        "synkkaData": {
            "names": [
                {"language": "fi", "value": "Keksitty luomu keitto 2 kg"},
                {"language": "en", "value": "Invented soup 2 kg"},
                {"language": "sv", "value": "Påhittad soppa 2 kg"},
            ],
            "keyIngredients": [{"language": "fi", "value": "Keksitty kasvis ja MAITO"}],
            "classifications": [
                {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                {
                    "name": "packagingMarkedLabel",
                    "type": "ENUM",
                    "values": [
                        {"id": "EU_ORGANIC_FARMING"},
                        {"id": "GOODS_FROM_FINLAND_BLUE_SWAN"},
                        {"id": "coolTemperature"},
                    ],
                },
                {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "LOW_LACTOSE"}]},
            ],
            "unitConversions": [
                {
                    "synkkaPackagingType": "BASE_UNIT_OR_EACH",
                    "netWeight": {"value": 1500, "unit": "GRM"},
                    "grossWeight": {"value": 1700, "unit": "GRM"},
                    "height": {"value": 10, "unit": "CMT"},
                    "length": {"value": 20, "unit": "CMT"},
                    "width": {"value": 15, "unit": "CMT"},
                }
            ],
            "variableUnit": False,
            "marketingTexts": [],
            "materialAdditionalDescriptions": [],
            "medias": [],
            "nutritionalContent": [],
        },
    }


def test_build_preserves_catalogue_facts_without_mutating_inputs() -> None:
    raw = invented_product()
    before = deepcopy(raw)
    reliability = {raw["id"]: 0.97}
    carbon = {raw["id"]: 2.1}
    record = build_record(raw, reliability, carbon)
    assert raw == before
    assert reliability == {"TEST-CATALOG-B": 0.97}
    assert carbon == {"TEST-CATALOG-B": 2.1}
    assert record.product_id == "TEST-CATALOG-B"
    assert record.names.fi == "Keksitty luomu keitto 2 kg"
    assert record.names.en == "Invented soup 2 kg"
    assert record.names.sv == "Påhittad soppa 2 kg"
    assert record.vendor == "Invented supplier"
    assert record.key_ingredients.fi == "Keksitty kasvis ja MAITO"
    assert record.country_of_origin == "fi"
    assert record.finnish_label_codes == ["GOODS_FROM_FINLAND_BLUE_SWAN"]
    assert record.organic.strength == "label"
    assert record.organic.label_codes == ["EU_ORGANIC_FARMING"]
    assert record.labels == ["finnish_label", "finnish_origin", "low_lactose", "organic_label"]
    assert record.allergens.contains == ["milk"]
    assert record.claims == ["low_lactose"]
    assert record.sales_unit == record.base_unit == "TEST-EACH"
    assert record.units[0].size_in_base_units == 1
    assert record.unit_conversions[0].net_weight is not None
    assert record.unit_conversions[0].net_weight.value == 1500
    assert record.allowed_lot_size == 0.5
    assert record.pack_size is not None
    assert record.pack_size.value == 1.5 and record.pack_size.unit == "kg"
    assert record.pack_size_source == "unit_conversions"
    assert record.variable_unit is False
    assert record.storage_class == "chilled"
    assert record.temperature_condition == 731
    assert record.sales_block is None  # No such field in the product schema.
    assert record.substitutions[0].substitute_product == "TEST-REPLACEMENT"
    assert record.category == "TEST-CATEGORY"
    assert record.delivery_reliability == 0.97
    assert record.carbon_kg_co2e_per_kg == 2.1


def test_missing_facts_stay_unknown_and_metrics_match_by_code() -> None:
    record = build_record({"id": "TEST-EMPTY"}, {"TEST-OTHER": 0.2}, {"TEST-OTHER": 9})
    assert record.names.fi is None
    assert record.key_ingredients.fi is None
    assert record.organic.strength == "unknown"
    assert record.allergens.data_available is False
    assert record.allergens.free_from == []
    assert record.pack_size is None and record.pack_size_source == "unknown"
    assert record.storage_class == "unknown"
    assert record.delivery_reliability is None
    assert record.carbon_kg_co2e_per_kg is None
    assert record.sales_block is None


def test_zero_metrics_are_present_and_sales_block_is_retained_if_supplied() -> None:
    record = build_record(
        {"id": "TEST-ZERO", "salesBlock": True}, {"TEST-ZERO": 0}, {"TEST-ZERO": 0}
    )
    assert record.delivery_reliability == 0
    assert record.carbon_kg_co2e_per_kg == 0
    assert record.sales_block is True


@pytest.mark.parametrize(
    ("reliability", "carbon"), [(1.1, 1), (-0.1, 1), (1, -1), (1, float("nan"))]
)
def test_invalid_metrics_degrade_to_missing(reliability: float, carbon: float) -> None:
    record = build_record(
        {"id": "TEST-INVALID"}, {"TEST-INVALID": reliability}, {"TEST-INVALID": carbon}
    )
    assert record.delivery_reliability == (reliability if 0 <= reliability <= 1 else None)
    assert record.carbon_kg_co2e_per_kg == (carbon if carbon >= 0 else None)


@pytest.mark.parametrize("filename_index", [0, 1])
def test_cli_end_to_end_deterministic_bytes(tmp_path: Path, filename_index: int) -> None:
    src = tmp_path / "source"
    src.mkdir()
    out = tmp_path / "output" / "catalog.slim.json"
    product = invented_product()
    empty = invented_product("TEST-CATALOG-A")
    empty["synkkaData"] = {}
    empty["substitutions"] = []
    product["synkkaData"]["classifications"].append(
        {"name": "allergen", "values": [{"id": "TEST-UNKNOWN", "unit": "MAY_CONTAIN"}]}
    )
    products_path = src / SOURCE_NAMES[0][filename_index]
    products_path.write_text(json.dumps([product, empty]), encoding="utf-8")
    (src / SOURCE_NAMES[1][filename_index]).write_text(
        "productId,deliveryReliability\nTEST-CATALOG-B,0.97\nTEST-CATALOG-A,\n", encoding="utf-8"
    )
    (src / SOURCE_NAMES[2][filename_index]).write_text(
        "productId,AVG kg CO2-eq/kg\nTEST-CATALOG-B,2.1\nTEST-CATALOG-A,\n", encoding="utf-8"
    )
    command = [sys.executable, "-m", "scripts.build_catalog", "--src", str(src), "--out", str(out)]
    first = subprocess.run(command, capture_output=True, text=True, check=True)
    first_bytes = out.read_bytes()
    # Input array and evidence order must not affect the output.
    product["synkkaData"]["classifications"].reverse()
    products_path.write_text(json.dumps([empty, product]), encoding="utf-8")
    second = subprocess.run(command, capture_output=True, text=True, check=True)
    assert out.read_bytes() == first_bytes
    assert first.stdout == second.stdout
    assert first.stdout.splitlines() == [
        "Products written: 2",
        "With names: 1",
        "With allergen data: 1",
        "With pack size: 1",
        "With reliability: 1",
        "With CO2: 1",
        "Unknown allergen codes (retained): 1",
        "Records skipped: 0",
        "Fields degraded: 2",
        "Degraded AVG kg CO2-eq/kg: 1",
        "Degraded deliveryReliability: 1",
    ]
    payload = json.loads(first_bytes)
    assert [r["product_id"] for r in payload] == ["TEST-CATALOG-A", "TEST-CATALOG-B"]
    assert payload[1]["pack_size"] == {"value": 1.5, "unit": "kg"}
    assert payload[1]["delivery_reliability"] == 0.97
    assert payload[1]["carbon_kg_co2e_per_kg"] == 2.1
    assert payload[0]["delivery_reliability"] is None
    assert payload[0]["carbon_kg_co2e_per_kg"] is None
    catalog = Catalog.load(out)
    assert len(catalog) == 2
    assert catalog.search("keitto")[0].product_id == "TEST-CATALOG-B"


def test_metric_csv_validation_and_unknown_blanks(tmp_path: Path) -> None:
    path = tmp_path / "invented.csv"
    path.write_text("productId,deliveryReliability\nTEST-A,\nTEST-B,0\n", encoding="utf-8")
    assert read_metric(path, "deliveryReliability", maximum=1) == {"TEST-B": 0}
    path.write_text("wrong,headers\n", encoding="utf-8")
    with pytest.raises(ValueError, match="headers"):
        read_metric(path, "deliveryReliability", maximum=1)
    for cell in ("", "NaN", "1.2", "-1", "garbage", "Infinity"):
        path.write_text(f"productId,deliveryReliability\nTEST-A,{cell}\n", encoding="utf-8")
        counts: Counter[str] = Counter()
        assert read_metric(path, "deliveryReliability", maximum=1, degradations=counts) == {}
        assert counts == {"deliveryReliability": 1}
    path.write_text('productId,deliveryReliability\nTEST-A,"0,8"\n', encoding="utf-8")
    assert read_metric(path, "deliveryReliability", maximum=1) == {"TEST-A": 0.8}
    path.write_text("productId,deliveryReliability\nTEST-A,0.8\nTEST-A,0.9\n", encoding="utf-8")
    assert read_metric(path, "deliveryReliability", maximum=1) == {}


def test_missing_source_file_does_not_create_output(tmp_path: Path) -> None:
    out = tmp_path / "catalog.slim.json"
    with pytest.raises(ValueError, match="Missing source file"):
        build_catalog(tmp_path, out)
    assert not out.exists()


@pytest.mark.parametrize("bad", [None, "", "garbage", [], {}, True, 0, -1, float("inf")])
def test_malformed_optional_fields_are_missing(bad: Any) -> None:
    raw = {
        "id": "TEST-MESSY",
        "vendorName": bad,
        "countryOfOrigin": bad,
        "salesUnit": bad,
        "baseUnit": bad,
        "allowedLotSize": bad,
        "units": bad,
        "substitutions": bad,
        "category": bad,
        "synkkaData": {
            "names": bad,
            "keyIngredients": bad,
            "classifications": bad,
            "unitConversions": bad,
        },
    }
    record = build_record(raw, {}, {})
    assert record.vendor == (bad if isinstance(bad, str) and bad else None)
    assert record.country_of_origin is None
    assert record.allowed_lot_size is None
    assert record.names == record.key_ingredients == type(record.names)()
    assert record.units == record.substitutions == record.unit_conversions == []
    assert record.pack_size is None and record.pack_size_source == "unknown"
    assert record.allergens.data_available is False
    assert record.allergens.free_from == []
    assert record.labels == record.claims == []


def test_malformed_entries_preserve_valid_siblings_and_count_degradations() -> None:
    counts: Counter[str] = Counter()
    raw = {
        "id": "TEST-MIXED",
        "salesBlock": "false",
        "temperatureCondition": 1.5,
        "units": [None, {"unitId": "TEST-EACH", "sizeInBaseUnits": 0}],
        "substitutions": [{"product": [], "substituteProduct": "TEST-B"}],
        "synkkaData": {
            "variableUnit": 1,
            "names": [
                None,
                {"language": [], "value": "bad"},
                {"language": "fi", "value": "Invented parcel 2 kg"},
            ],
            "classifications": [{"name": "allergen", "values": None}],
        },
    }
    record = build_record(raw, {}, {}, degradations=counts)
    assert record.names.fi == "Invented parcel 2 kg"
    assert record.pack_size is not None and record.pack_size.value == 2
    assert record.sales_block is record.variable_unit is record.temperature_condition is None
    assert record.allergens.data_available is False
    assert counts == {
        "units": 2,
        "substitutions": 1,
        "names": 2,
        "classifications.values": 1,
        "salesBlock": 1,
        "variableUnit": 1,
        "temperatureCondition": 1,
    }


def test_bad_records_are_skipped_and_cli_only_prints_counts(tmp_path: Path) -> None:
    products = [{}, {"id": " "}, None, {"id": "TEST-PRIVATE", "vendorName": 12}]
    (tmp_path / "products.json").write_text(json.dumps(products), encoding="utf-8")
    (tmp_path / "delivery-reliability.csv").write_text(
        "productId,deliveryReliability\nTEST-PRIVATE,garbage\n", encoding="utf-8"
    )
    (tmp_path / "carbon-footprint.csv").write_text(
        "productId,AVG kg CO2-eq/kg\nTEST-PRIVATE,\n", encoding="utf-8"
    )
    skips: Counter[str] = Counter()
    fields: Counter[str] = Counter()
    out = tmp_path / "slim.json"
    records = build_catalog(tmp_path, out, skips=skips, degradations=fields)
    assert [r.product_id for r in records] == ["TEST-PRIVATE"]
    assert skips == {"missing_product_code": 2, "invalid_record": 1}
    assert fields == {"deliveryReliability": 1, "AVG kg CO2-eq/kg": 1, "vendorName": 1}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.build_catalog", "--src", str(tmp_path), "--out", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "TEST-PRIVATE" not in result.stdout + result.stderr
    assert "Records skipped: 3" in result.stdout
    assert "Skipped missing_product_code: 2" in result.stdout
    assert "Fields degraded: 3" in result.stdout


def test_unexpected_record_failure_does_not_abort_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import build_catalog as builder

    (tmp_path / "products.json").write_text(
        json.dumps([{"id": "TEST-BROKEN"}, {"id": "TEST-GOOD"}]), encoding="utf-8"
    )
    (tmp_path / "delivery-reliability.csv").write_text(
        "productId,deliveryReliability\n", encoding="utf-8"
    )
    (tmp_path / "carbon-footprint.csv").write_text("productId,AVG kg CO2-eq/kg\n", encoding="utf-8")

    def fail_one(raw: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        if raw["id"] == "TEST-BROKEN":
            raise RuntimeError("Invented record failure")
        return build_record(raw, *args, **kwargs)

    monkeypatch.setattr(builder, "build_record", fail_one)
    skips: Counter[str] = Counter()
    records = builder.build_catalog(tmp_path, tmp_path / "output.json", skips=skips)
    assert [r.product_id for r in records] == ["TEST-GOOD"]
    assert skips == {"invalid_record": 1}
