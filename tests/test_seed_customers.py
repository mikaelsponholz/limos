"""The resolver uses invented local catalogues and never edits committed seeds."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.catalog.models import LocalizedText, PackSize, SlimRecord
from gateway.customers import CUSTOMERS_DIR, load_customers
from scripts.seed_customers import seed_customers

ROOT = Path(__file__).resolve().parents[1]


def invented_catalog(path: Path) -> list[SlimRecord]:
    records = [
        SlimRecord(
            product_id="900701", names=LocalizedText(fi="Example juustokakku"), sales_unit="ST"
        ),
        SlimRecord(
            product_id="900705", names=LocalizedText(fi="Example täytekakku"), sales_unit="ST"
        ),
        SlimRecord(
            product_id="900702",
            names=LocalizedText(en="Example punajuuri"),
            sales_unit="KI",
            pack_size=PackSize(value=2.0, unit="kg"),
        ),
        SlimRecord(
            product_id="900703", names=LocalizedText(fi="Example vehnäjauho"), sales_unit="PRK"
        ),
        SlimRecord(product_id="900704", names=LocalizedText(fi="Example peruna"), sales_unit="ST"),
        SlimRecord(
            product_id="900700",
            names=LocalizedText(fi="Example juustokakku blocked"),
            sales_unit="ST",
            sales_block=True,
        ),
    ]
    path.write_text(
        json.dumps([record.model_dump(mode="json") for record in records]), encoding="utf-8"
    )
    return records


def seed_bytes() -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in CUSTOMERS_DIR.iterdir() if path.is_file()}


def test_cli_resolves_overlay_offline_with_only_count_output(tmp_path: Path) -> None:
    before = seed_bytes()
    catalog_path = tmp_path / "catalog.slim.json"
    invented_catalog(catalog_path)
    out = tmp_path / ".runtime" / "customers"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "seed_customers.py"),
            "--catalog",
            str(catalog_path),
            "--out",
            str(out),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.strip() == "Customers written: 2. History lines resolved: 5."
    assert seed_bytes() == before
    original = load_customers(overlay_dir=None)
    resolved = load_customers(overlay_dir=out)
    assert {p.name for p in out.iterdir()} == {"c_pena.json", "c_chef.json"}
    expected = {
        "900310": "900701",
        "900311": "900701",
        "900401": "900702",
        "900402": "900703",
        "900403": "900704",
    }
    for customer_id, customer in resolved.items():
        for order, previous in zip(
            customer.order_history, original[customer_id].order_history, strict=True
        ):
            assert order.total == previous.total and order.source == "seeded" and order.simulated
            for line, old in zip(order.lines, previous.lines, strict=True):
                assert line.product_id == expected[old.product_id]
                assert line.name.startswith("Example ")
                assert line.unit_price == old.unit_price and line.line_total == old.line_total
                assert line.quantity_sales_units == old.quantity_sales_units
    beetroot = resolved["c_chef"].order_history[0].lines[0]
    assert beetroot.quantity_text == "4 x 2 kg" and beetroot.sales_unit == "KI"
    assert resolved["c_pena"].order_history[0].lines[0].quantity_text == "2 ST"
    # A rerun starts from committed seed ids, never the resolved overlay codes.
    assert seed_customers(catalog_path, out=out) == (2, 5)
    assert seed_bytes() == before


def test_custom_seed_directory_and_spec(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    customer = load_customers(overlay_dir=None)["c_pena"]
    (seeds / "c_pena.json").write_text(customer.model_dump_json(), encoding="utf-8")
    spec = tmp_path / "queries.json"
    spec.write_text('{"900310":"juustokakku", "900311":"täytekakku"}', encoding="utf-8")
    catalog_path = tmp_path / "catalog.slim.json"
    invented_catalog(catalog_path)
    assert seed_customers(
        catalog_path, customers_dir=seeds, seed_spec=spec, out=tmp_path / "out"
    ) == (1, 2)


def test_missing_match_writes_nothing_and_cli_does_not_echo_input(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.slim.json"
    catalog.write_text("[]", encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="No usable catalogue match"):
        seed_customers(catalog, out=out)
    assert not out.exists()
    catalog.write_text(
        '[{"product_id":"900999", "unexpected":"Example private input"}]', encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.seed_customers",
            "--catalog",
            str(catalog),
            "--out",
            str(out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and result.stderr == ""
    assert (
        result.stdout.strip()
        == "Customers written: 0. Seeding failed; check input files and seed matches."
    )
    assert not out.exists()


def test_resolver_refuses_writing_into_seed_directory(tmp_path: Path) -> None:
    before = seed_bytes()
    for out in (CUSTOMERS_DIR, CUSTOMERS_DIR / "nested", tmp_path / "seeds" / "nested"):
        with pytest.raises(ValueError, match="outside the committed customers directory"):
            seed_customers(tmp_path / "missing.json", customers_dir=tmp_path / "seeds", out=out)
    assert seed_bytes() == before
