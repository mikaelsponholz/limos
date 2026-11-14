"""Resolve simulated customer history offline, writing only a runtime overlay."""

import argparse
import json
import sys
from pathlib import Path

# Support the documented direct-script invocation as well as python -m scripts.seed_customers.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import TypeAdapter

from gateway.catalog.index import Catalog
from gateway.customers import CUSTOMERS_DIR, OVERLAY_DIR, ROOT, load_customers
from gateway.schemas import Customer

SEED_SPEC = ROOT / "scripts" / "customer_seed_spec.json"
DEMO_CASES = ROOT / ".runtime" / "demo_cases.json"


def _check_destination(out: Path, customers_dir: Path) -> None:
    destination = out.resolve()
    if any(
        destination.is_relative_to(folder.resolve()) for folder in (CUSTOMERS_DIR, customers_dir)
    ):
        raise ValueError(
            "The overlay destination must be outside the committed customers directory."
        )


def seed_customers(
    catalog_path: Path,
    *,
    customers_dir: Path = CUSTOMERS_DIR,
    out: Path = OVERLAY_DIR,
    seed_spec: Path = SEED_SPEC,
    demo_cases: Path = DEMO_CASES,
) -> tuple[int, int]:
    _check_destination(out, customers_dir)  # before the slow catalogue load
    return resolve_customers(
        Catalog.load(catalog_path),
        customers_dir=customers_dir,
        out=out,
        seed_spec=seed_spec,
        demo_cases=demo_cases,
    )


def resolve_customers(
    catalog: Catalog,
    *,
    customers_dir: Path = CUSTOMERS_DIR,
    out: Path = OVERLAY_DIR,
    seed_spec: Path = SEED_SPEC,
    demo_cases: Path = DEMO_CASES,
) -> tuple[int, int]:
    """Resolve the history against an already loaded catalogue (the gateway reuses its own)."""
    _check_destination(out, customers_dir)
    queries = TypeAdapter(dict[str, str]).validate_json(seed_spec.read_bytes())
    customers = load_customers(customers_dir, overlay_dir=None)
    override = None
    if demo_cases.is_file():
        cases = json.loads(demo_cases.read_bytes())
        if cases.get("live") is True:
            override = next(
                (
                    catalog.lookup(row["product_id"])
                    for row in cases.get("cases", [])
                    if row.get("case") == "reorder_shortage" and row.get("status") == "verified"
                ),
                None,
            )
            if override and (override.sales_block or not override.sales_unit):
                override = None
    resolved_customers: list[Customer] = []
    line_count = 0
    for customer in customers.values():
        for order in customer.order_history:
            for line in order.lines:
                query = queries.get(line.product_id)
                if not query:
                    raise ValueError("Missing seed search term for a history line.")
                product = (override if customer.customer_id == "c_pena" else None) or next(
                    (
                        record
                        for record in catalog.search(query, exclude_sales_blocked=True)
                        if (record.names.fi or record.names.en) and record.sales_unit
                    ),
                    None,
                )
                if product is None:
                    raise ValueError("No usable catalogue match for a history line.")
                line.product_id = product.product_id
                line.name = product.names.fi or product.names.en or line.name
                line.sales_unit = product.sales_unit or line.sales_unit
                pack = product.pack_size
                line.quantity_text = (
                    f"{line.quantity_sales_units:g} x {pack.value:g} {pack.unit}"
                    if pack
                    else f"{line.quantity_sales_units:g} {line.sales_unit}"
                )
                # SlimRecord has no price fields. Keep the seed prices and totals, never guess.
                line_count += 1
        resolved_customers.append(Customer.model_validate_json(customer.model_dump_json()))
    # Resolve and validate everything before writing any overlays.
    out.mkdir(parents=True, exist_ok=True)
    for customer in resolved_customers:
        (out / f"{customer.customer_id}.json").write_text(
            customer.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    return len(resolved_customers), line_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--customers-dir", type=Path, default=CUSTOMERS_DIR)
    parser.add_argument("--out", type=Path, default=OVERLAY_DIR)
    parser.add_argument("--seed-spec", type=Path, default=SEED_SPEC)
    args = parser.parse_args()
    try:
        customers, lines = seed_customers(
            args.catalog, customers_dir=args.customers_dir, out=args.out, seed_spec=args.seed_spec
        )
    except (OSError, ValueError):
        # Validation exceptions can contain supplier data; keep CLI output data-free.
        print("Customers written: 0. Seeding failed; check input files and seed matches.")
        return 1
    print(f"Customers written: {customers}. History lines resolved: {lines}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
