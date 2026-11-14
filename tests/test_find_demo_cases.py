"""Offline discovery and mocked live verification use invented catalogue records only."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from gateway.catalog.index import Catalog
from gateway.catalog.models import SlimRecord
from gateway.settings import Settings
from gateway.valio.models import AvailabilityResponse, PriceResponse
from scripts import find_demo_cases as finder
from tests.test_presets import recording


def invented_catalog() -> Catalog:
    records = [
        record
        for preset in ("restaurant_weekly", "wedding_120", "birthday_kids_25", "company_lunch_60")
        for record in recording(preset)["catalog"]
    ]
    return Catalog(TypeAdapter(list[SlimRecord]).validate_python(records))


def test_offline_selection_covers_each_case_without_inventing_stock() -> None:
    catalog = invented_catalog()
    rows = finder.find_cases(catalog)
    assert rows == finder.find_cases(catalog)
    assert {row["preset"] for row in rows} == {
        "restaurant_weekly",
        "wedding_120",
        "birthday_kids_25",
        "company_lunch_60",
    }
    assert {row["case"] for row in rows} == {
        "auto",
        "split_stock",
        "valio_substitute",
        "may_contain_nuts",
        "finnish",
        "non_finnish",
    }
    assert {row["status"] for row in rows} == {"catalogue_candidate"}
    assert not any("observed" in row for row in rows)
    weekly = [row for row in rows if row["preset"] == "restaurant_weekly"]
    assert {row["item"] for row in weekly} == {"lactose-free milk", "butter", "potatoes", "eggs"}
    for row in rows:
        record = catalog.lookup(row["product_id"])
        companion = (
            catalog.lookup(row["companion_product_id"]) if row["companion_product_id"] else None
        )
        assert record is not None and not record.sales_block
        if row["case"] == "may_contain_nuts":
            assert finder.NUTS & set(record.allergens.may_contain)
            assert companion is not None and finder.nut_free(companion)
        elif row["case"] == "valio_substitute":
            assert companion is not None
            assert any(
                rule.product == record.product_id
                and rule.substitute_product == companion.product_id
                for rule in record.substitutions
            )
        elif row["case"] == "split_stock":
            assert companion is not None and companion.product_id != record.product_id
            assert record.pack_size.unit == companion.pack_size.unit == "L"
        elif row["case"] == "finnish":
            assert record.country_of_origin == "fi"
        elif row["case"] == "non_finnish":
            assert record.country_of_origin and record.country_of_origin != "fi"


def test_offline_does_not_treat_missing_or_contradictory_allergens_as_safe() -> None:
    records = TypeAdapter(list[SlimRecord]).validate_python(
        recording("birthday_kids_25")["catalog"]
    )
    safe = records[1]
    safe.allergens.free_from = []
    assert not finder.nut_free(safe)
    safe.allergens.free_from = ["tree_nuts", "peanuts"]
    safe.allergens.data_available = False
    assert not finder.nut_free(safe)
    safe.allergens.data_available = True
    safe.allergens.undeclared = ["tree_nuts"]
    assert not finder.nut_free(safe)
    assert finder.find_cases(Catalog([])) == []


def test_offline_cli_writes_runtime_json_without_settings_or_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = tmp_path / "invented.json"
    catalog_path.write_text(json.dumps(recording("restaurant_weekly")["catalog"]), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline discovery must not load settings or open a service.")

    monkeypatch.setattr(finder, "ROOT", tmp_path)
    monkeypatch.setattr(finder, "Settings", forbidden)
    monkeypatch.setattr(finder, "ValioClient", forbidden)
    # The weekly fields come from the real committed preset, not the output destination.
    presets = tmp_path / "presets"
    presets.mkdir()
    source = Path(__file__).resolve().parents[1] / "presets" / "restaurant_weekly.json"
    (presets / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(sys, "argv", ["find_demo_cases", "--catalog", str(catalog_path)])
    assert finder.main() == 0
    result = json.loads((tmp_path / ".runtime" / "demo_cases.json").read_bytes())
    assert result["live"] is False and result["cases"]
    output = capsys.readouterr().out
    assert "Preset | Case" in output and "Catalogue candidates only" in output


async def test_live_verification_uses_small_mocked_shortlists_and_actual_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = invented_catalog()
    rows = finder.find_cases(catalog)
    fixtures = [
        recording(name)
        for name in ("restaurant_weekly", "wedding_120", "birthday_kids_25", "company_lunch_60")
    ]
    prices = PriceResponse.model_validate(fixtures[0]["valio"]["price"])
    stocks = AvailabilityResponse.model_validate(fixtures[0]["valio"]["availability"])
    for fixture in fixtures[1:]:
        prices.products.extend(PriceResponse.model_validate(fixture["valio"]["price"]).products)
        stocks.products.extend(
            AvailabilityResponse.model_validate(fixture["valio"]["availability"]).products
        )
    calls = []

    class MockValio:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def ping(self):
            calls.append(("ping", []))

        async def price(self, codes, requested):
            calls.append(("price", list(codes)))
            return prices.model_copy(
                update={"products": [p for p in prices.products if p.product_id in codes]}
            )

        async def availability(self, codes, requested):
            calls.append(("availability", list(codes)))
            return stocks.model_copy(
                update={"products": [s for s in stocks.products if s.product_id in codes]}
            )

    monkeypatch.setattr(finder, "ValioClient", MockValio)
    await finder.verify_live(rows, catalog, Settings(valio_api_key=""), date(2026, 11, 20))
    assert calls[0] == ("ping", [])
    assert all(1 <= len(codes) <= 20 for method, codes in calls if method != "ping")
    assert {row["case"] for row in rows if row["status"] == "verified"} == {
        "auto",
        "split_stock",
        "valio_substitute",
        "may_contain_nuts",
        "finnish",
        "non_finnish",
    }
    # The reverse cream pair is not short: successful transport alone cannot verify it.
    assert any(row["case"] == "split_stock" and row["status"] == "not_matched" for row in rows)


def test_live_failure_message_never_contains_transport_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Mock the entire live boundary; this test never creates a Valio client or reads .env.
    monkeypatch.setattr(finder.Catalog, "load", lambda path: Catalog([]))
    monkeypatch.setattr(finder, "find_cases", lambda catalog: [])
    monkeypatch.setattr(finder, "ROOT", tmp_path)

    async def failure(*args):
        raise RuntimeError("Invented private transport details")

    monkeypatch.setattr(finder, "verify_live", failure)
    monkeypatch.setattr(sys, "argv", ["find_demo_cases", "--live"])
    assert finder.main() == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.strip() == (
        "Could not check Valio. Check the connection and environment authentication (RuntimeError)."
    )
    assert "Invented private" not in output.out
    assert not (tmp_path / ".runtime" / "demo_cases.json").exists()
