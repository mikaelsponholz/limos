"""A re-seed on deploy updates a seeded customer's display name in place, never duplicating it."""

from pathlib import Path

from gateway.customers import CUSTOMERS_DIR, load_customers
from scripts.seed_customers import seed_customers
from tests.test_seed_customers import invented_catalog


def test_reseed_replaces_old_display_name_without_duplicate(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.slim.json"
    invented_catalog(catalog_path)
    out = tmp_path / ".runtime" / "customers"
    out.mkdir(parents=True)
    # A deployment seeded before the rename still has the old name in its overlay.
    old = (CUSTOMERS_DIR / "c_pena.json").read_text(encoding="utf-8").replace("Frank", "Pena")
    (out / "c_pena.json").write_text(old, encoding="utf-8")

    seed_customers(catalog_path, customers_dir=CUSTOMERS_DIR, out=out)

    customers = load_customers(CUSTOMERS_DIR, out)
    assert sorted(customers) == sorted(load_customers(CUSTOMERS_DIR, None))
    frank = customers["c_pena"]
    assert (frank.name, frank.buyer_id) == ("Frank", "ab-oy")
    assert sorted(path.name for path in out.iterdir()) == sorted(
        path.name for path in CUSTOMERS_DIR.glob("c_*.json")
    )
    assert "Pena" not in (out / "c_pena.json").read_text(encoding="utf-8")
