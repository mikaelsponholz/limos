"""Frank's three-line seed uses invented catalogues and isolated runtime files only."""

import json
from pathlib import Path

import pytest

from gateway.catalog.index import Catalog
from gateway.customers import CUSTOMERS_DIR, load_customers
from scripts import find_demo_cases as finder
from scripts.seed_customers import resolve_customers
from tests.test_offers import product


def oat_catalog(*, lot: float | None = None, companion: str = "same_unit") -> Catalog:
    records = [
        product("990101", names={"fi": "Example juustokakku"}),
        product(
            "990104",
            names={"fi": "Santa Maria Viherpippuri Kokonainen 13 g"},
            sales_unit="ST",
            pack_size={"value": 0.013, "unit": "kg"},
        ),
        product(
            "990102",
            names={"en": "Example oat drink iKaffe 1L"},
            sales_unit="ST",
            pack_size={"value": 2, "unit": "L"},
            allowedLotSize=lot,
            substitutions=(
                []
                if companion == "no_substitute"
                else [{"product": "990102", "substituteProduct": "990103"}]
            ),
        ),
    ]
    if companion != "missing_record":
        records.append(
            product(
                "990103",
                names={"en": "Example coffee drink replacement"},
                sales_unit="ST",
                pack_size=(
                    None
                    if companion == "missing_pack"
                    else {"value": 1, "unit": "kg" if companion == "different_unit" else "L"}
                ),
            )
        )
    return Catalog(records)


@pytest.mark.parametrize("history_product_id", ["900312", "900999", None])
def test_verified_override_replaces_only_matching_history_line(
    tmp_path: Path, history_product_id: str | None
) -> None:
    seeds = tmp_path / "customers"
    seeds.mkdir()
    original = (CUSTOMERS_DIR / "c_pena.json").read_bytes()
    (seeds / "c_pena.json").write_bytes(original)
    cases = tmp_path / "demo_cases.json"
    row = {"case": "reorder_shortage", "status": "verified", "product_id": "990103"}
    if history_product_id is not None:
        row["history_product_id"] = history_product_id
    cases.write_text(json.dumps({"live": True, "cases": [row]}), encoding="utf-8")
    out = tmp_path / "overlays"

    assert resolve_customers(oat_catalog(), customers_dir=seeds, out=out, demo_cases=cases) == (
        1,
        3,
    )

    customer = load_customers(customers_dir=seeds, overlay_dir=out)["c_pena"]
    lines = customer.order_history[0].lines
    assert [line.product_id for line in lines] == [
        "990101",
        "990103" if history_product_id == "900312" else "990102",
        "990104",
    ]
    assert lines[0].name == "Example juustokakku"
    assert [line.quantity_sales_units for line in lines] == [2, 6, 14]
    assert [line.line_total for line in lines] == [77, 15, 35]
    assert customer.order_history[0].total == 127.0
    assert customer.defaults.budget == 1500
    assert customer.exclude_allergens == []
    assert (seeds / "c_pena.json").read_bytes() == original


@pytest.mark.parametrize("lot,units", [(None, 16), (1, 16), (3, 18), (2.5, 20)])
def test_offline_finder_scales_oat_drink_to_80_guests_by_lot(lot: float | None, units: int) -> None:
    catalog = oat_catalog(lot=lot)
    rows = [row for row in finder.find_cases(catalog) if row["case"] == "reorder_shortage"]

    assert rows == [
        {
            "preset": "pena_reorder",
            "case": "reorder_shortage",
            "item": "Example oat drink for coffee 1 l",
            "product_id": "990102",
            "companion_product_id": "990103",
            "status": "catalogue_candidate",
            "quantity": units * 2,
            "quantity_sales_units": units,
            "unit": "L",
            "history_product_id": "900312",
        }
    ]
    assert finder.REORDER_GUESTS == 80
    assert finder.find_cases(catalog) == finder.find_cases(catalog)


@pytest.mark.parametrize(
    "companion", ["different_unit", "missing_pack", "no_substitute", "missing_record"]
)
def test_offline_finder_rejects_missing_or_incompatible_oat_substitute(companion: str) -> None:
    rows = finder.find_cases(oat_catalog(companion=companion))
    assert [row for row in rows if row["case"] == "reorder_shortage"] == []
