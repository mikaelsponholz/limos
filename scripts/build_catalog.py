"""Build the slim catalogue offline: python -m scripts.build_catalog --src data/valio."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from gateway.catalog.build import build_record
from gateway.catalog.models import SlimRecord
from gateway.catalog.sanitize import Counts, degraded, number
from gateway.catalog.sanitize import text as clean_text

SOURCE_NAMES = (
    ("valio_aimo_junction26_product_data.json", "products.json"),
    ("valio_aimo_junction26_delivery_reliability.csv", "delivery-reliability.csv"),
    ("valio_aimo_junction26_carbon_footprint.csv", "carbon-footprint.csv"),
)


def source_file(folder: Path, names: tuple[str, ...]) -> Path:
    for name in names:
        path = folder / name
        if path.is_file():
            return path
    raise ValueError(f"Missing source file in {folder}: expected {names[0]}")


def read_metric(
    path: Path,
    column: str,
    *,
    maximum: float | None = None,
    degradations: Counts | None = None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not {"productId", column}.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing expected CSV headers in {path.name}")
        for row in reader:
            code = clean_text(row.get("productId"))
            value = number(row.get(column), maximum=maximum)
            if code is None:
                degraded(degradations, f"{column}.missing_code")
                continue
            if code in seen:
                result.pop(code, None)
                degraded(degradations, f"{column}.duplicate")
            elif value is None:
                degraded(degradations, column)
            else:
                result[code] = value
            seen.add(code)
    return result


def build_catalog(
    src: Path,
    out: Path,
    *,
    skips: Counts | None = None,
    degradations: Counts | None = None,
) -> list[SlimRecord]:
    products_path, reliability_path, carbon_path = (
        source_file(src, names) for names in SOURCE_NAMES
    )
    reliability = read_metric(
        reliability_path, "deliveryReliability", maximum=1, degradations=degradations
    )
    carbon = read_metric(carbon_path, "AVG kg CO2-eq/kg", degradations=degradations)
    # Load the source array exactly once. No network, settings or environment files.
    with products_path.open(encoding="utf-8-sig") as source:
        products: list[dict[str, Any]] = json.load(source)
    if not isinstance(products, list):
        raise ValueError("Expected a product array")
    records_by_code: dict[str, SlimRecord] = {}
    duplicates: set[str] = set()
    for raw in products:
        if not isinstance(raw, dict):
            degraded(skips, "invalid_record")
            continue
        if clean_text(raw.get("id")) is None:
            degraded(skips, "missing_product_code")
            continue
        try:
            record = build_record(raw, reliability, carbon, degradations=degradations)
        except Exception:
            # This boundary isolates unforeseen record failures. Never log supplier data.
            degraded(skips, "invalid_record")
            continue
        code = record.product_id
        if code in records_by_code or code in duplicates:
            degraded(skips, "duplicate_product_code")
            if code in records_by_code:
                del records_by_code[code]
                degraded(skips, "duplicate_product_code")
            duplicates.add(code)
            continue
        records_by_code[code] = record
    records = sorted(records_by_code.values(), key=lambda record: record.product_id)
    payload = (
        json.dumps(
            [record.model_dump(mode="json") for record in records],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload.encode("utf-8"))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=Path("data/valio"))
    parser.add_argument("--out", type=Path, default=Path("data/valio/catalog.slim.json"))
    args = parser.parse_args()
    skips: Counts = Counter()
    degradations: Counts = Counter()
    records = build_catalog(args.src, args.out, skips=skips, degradations=degradations)
    print(f"Products written: {len(records)}")
    print(f"With names: {sum(any(r.names.model_dump().values()) for r in records)}")
    print(f"With allergen data: {sum(r.allergens.data_available for r in records)}")
    print(f"With pack size: {sum(r.pack_size is not None for r in records)}")
    print(f"With reliability: {sum(r.delivery_reliability is not None for r in records)}")
    print(f"With CO2: {sum(r.carbon_kg_co2e_per_kg is not None for r in records)}")
    unknown = sorted({code for r in records for code in r.allergens.unknown_codes})
    statuses = sorted({status for r in records for status in r.allergens.unknown_statuses})
    if unknown:
        print(f"Unknown allergen codes (retained): {len(unknown)}")
    if statuses:
        print(f"Unknown allergen statuses (retained): {len(statuses)}")
    print(f"Records skipped: {sum(skips.values())}")
    for kind, count in sorted(skips.items()):
        print(f"Skipped {kind}: {count}")
    print(f"Fields degraded: {sum(degradations.values())}")
    for kind, count in sorted(degradations.items()):
        print(f"Degraded {kind}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
