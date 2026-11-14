"""Customer behavior uses committed invented seeds and isolated runtime paths only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.customers import (
    CUSTOMERS_DIR,
    append_placed_order,
    apply_profile,
    get_customer,
    latest_order,
    list_customers,
    load_customers,
    resolve_reorder,
)
from gateway.errors import GatewayError
from gateway.schemas import Customer, CustomerDefaults, Order, PlacedBy, Preference, RequestCreate
from gateway.store import FileStore, MemoryStore, Store
from tests.test_offers import NOW, REQUESTED, offer


def pena() -> Customer:
    return load_customers(overlay_dir=None)["c_pena"]


def test_both_seeds_validate_and_match_demo_profiles() -> None:
    customers = load_customers(overlay_dir=None)
    assert set(customers) == {"c_pena", "c_chef"}
    for customer in customers.values():
        assert Customer.model_validate_json(customer.model_dump_json()) == customer
        assert customer.simulated is True
        assert customer.order_history == sorted(
            customer.order_history, key=lambda o: o.date, reverse=True
        )
        for order in customer.order_history:
            assert order.simulated is True and order.source == "seeded"
            assert sum(line.line_total for line in order.lines) == order.total
            for line in order.lines:
                assert line.product_id.startswith("900") and len(line.product_id) == 6
                assert line.name.startswith("Example ")
    executive, chef = customers["c_pena"], customers["c_chef"]
    assert (executive.name, executive.company, executive.role) == ("Pena", "Ab Oy", "Executive")
    assert executive.buyer_id == "ab-oy" and executive.age_group == "55_plus"
    assert executive.playful is False
    assert executive.preferences == [Preference(key="finnish_origin", level="prefer")]
    assert executive.defaults == CustomerDefaults(
        priority="quality", typical_guests=30, budget=None
    )
    assert {o.event_type for o in executive.order_history} == {"company_meeting", "staff_coffee"}
    assert chef.buyer_id == "bistro-example" and chef.age_group == "under_35" and chef.playful
    assert "Example" in chef.name and "Example" in chef.company and chef.role == "Chef"
    assert {p.key for p in chef.preferences} == {"organic", "low_carbon"}
    assert chef.defaults.priority == "balanced"
    assert {o.event_type for o in chef.order_history} == {"restaurant_weekly", "tasting_menu"}


def test_overlay_wins_and_only_seeded_ids_are_recognised(tmp_path: Path) -> None:
    original = pena()
    overlay = original.model_copy(deep=True)
    overlay.order_history[0].lines[0].product_id = "900999"
    (tmp_path / "c_pena.json").write_text(overlay.model_dump_json(), encoding="utf-8")
    unknown = original.model_copy(update={"customer_id": "unknown"})
    (tmp_path / "unknown.json").write_text(unknown.model_dump_json(), encoding="utf-8")
    store = MemoryStore()
    loaded = get_customer("c_pena", store, overlay_dir=tmp_path)
    assert loaded.order_history[0].lines[0].product_id == "900999"
    assert pena() == original
    assert {c.customer_id for c in list_customers(store, overlay_dir=tmp_path)} == {
        "c_pena",
        "c_chef",
    }
    with pytest.raises(GatewayError) as error:
        get_customer("unknown", store, overlay_dir=tmp_path)
    assert (error.value.status_code, error.value.code) == (404, "CUSTOMER_NOT_FOUND")


def test_overlay_must_validate_and_retain_ownership(tmp_path: Path) -> None:
    path = tmp_path / "c_pena.json"
    path.write_text('{"customer_id": "c_pena"}', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_customers(overlay_dir=tmp_path)
    for changes in ({"customer_id": "other"}, {"buyer_id": "other"}):
        path.write_text(pena().model_copy(update=changes).model_dump_json(), encoding="utf-8")
        with pytest.raises(ValueError, match="retain customer and buyer ids"):
            load_customers(overlay_dir=tmp_path)


def test_loader_paths_and_newest_seeded_summary(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    customer = pena()
    customer.order_history.reverse()  # The loader cannot assume input order.
    (profiles / "c_pena.json").write_text(customer.model_dump_json(), encoding="utf-8")
    store = MemoryStore()
    loaded = get_customer("c_pena", store, customers_dir=profiles, overlay_dir=tmp_path)
    assert loaded.order_history[0].order_ref == "ph_pena_01"
    summaries = list_customers(store, customers_dir=profiles, overlay_dir=tmp_path)
    assert len(summaries) == 1 and summaries[0].last_order is not None
    assert summaries[0].last_order.order_ref == "ph_pena_01"
    customer.order_history.clear()
    (profiles / "c_pena.json").write_text(customer.model_dump_json(), encoding="utf-8")
    assert list_customers(store, customers_dir=profiles, overlay_dir=tmp_path)[0].last_order is None


def test_profile_fills_each_gap_and_keeps_inputs_isolated() -> None:
    customer = pena()
    customer.defaults.budget = 200.0
    customer.exclude_allergens = ["tree_nuts"]
    request = RequestCreate(customer_id="c_pena", text="Example coffee table")
    before_request, before_customer = request.model_dump(), customer.model_dump()
    resolved, gaps = apply_profile(request, customer)
    assert resolved.buyer_id == "ab-oy" and resolved.priority == "quality"
    assert resolved.budget == 200.0 and resolved.preferences == customer.preferences
    assert resolved.exclude_allergens == ["tree_nuts"]
    assert {gap.field: gap.value for gap in gaps} == {
        "buyer_id": "ab-oy",
        "priority": "quality",
        "budget": 200.0,
        "preferences.finnish_origin": {"key": "finnish_origin", "level": "prefer"},
        "exclude_allergens": ["tree_nuts"],
    }
    assert all(gap.source == "profile" and "Pena's profile" in gap.note for gap in gaps)
    assert (
        next(g.note for g in gaps if g.field == "priority")
        == "Priority quality, from Pena's profile"
    )
    assert request.model_dump() == before_request and customer.model_dump() == before_customer
    resolved.preferences[0].level = "require"
    assert customer.preferences[0].level == "prefer"


@pytest.mark.parametrize("budget", [None, 0.0, 99.0])
def test_explicit_request_values_including_schema_defaults_win(budget: float | None) -> None:
    customer = pena()
    customer.defaults.budget = 200.0
    customer.exclude_allergens = ["milk"]
    request = RequestCreate(
        buyer_id="ab-oy",
        customer_id="c_pena",
        text="Example coffee table",
        priority="balanced",
        budget=budget,
        preferences=[Preference(key="finnish_origin", level="require")],
        exclude_allergens=["egg"],
    )
    resolved, gaps = apply_profile(request, customer)
    assert resolved == request and gaps == []
    assert "priority" in request.model_fields_set and "budget" in request.model_fields_set


def test_preferences_merge_by_key_and_empty_allergens_use_profile() -> None:
    customer = load_customers(overlay_dir=None)["c_chef"]
    customer.exclude_allergens = ["milk"]
    request = RequestCreate(
        customer_id="c_chef",
        text="Example staples",
        priority="balanced",
        preferences=[
            Preference(key="organic", level="require"),
            Preference(key="vegan", level="require"),
        ],
        exclude_allergens=[],
    )
    resolved, gaps = apply_profile(request, customer)
    assert [(p.key, p.level) for p in resolved.preferences] == [
        ("organic", "require"),
        ("vegan", "require"),
        ("low_carbon", "prefer"),
    ]
    assert resolved.exclude_allergens == ["milk"]
    assert {gap.field for gap in gaps} == {
        "buyer_id",
        "preferences.low_carbon",
        "exclude_allergens",
    }


def test_buyer_mismatch_is_domain_error() -> None:
    request = RequestCreate(buyer_id="other", customer_id="c_pena", text="Example staples")
    with pytest.raises(GatewayError) as error:
        apply_profile(request, pena())
    assert (error.value.status_code, error.value.code) == (422, "BUYER_MISMATCH")


def test_without_profile_records_only_missing_system_priority() -> None:
    request = RequestCreate(buyer_id="invented-buyer", text="Example staples")
    resolved, gaps = apply_profile(request, None)
    assert resolved == request and resolved.priority == "balanced"
    assert len(gaps) == 1
    assert (gaps[0].field, gaps[0].value, gaps[0].source) == ("priority", "balanced", "default")
    assert gaps[0].note == "Priority balanced, system default"
    explicit = RequestCreate(buyer_id="invented-buyer", text="Example staples", priority="balanced")
    assert apply_profile(explicit)[1] == []


@pytest.mark.parametrize(
    ("guests_now", "scale", "quantity"),
    [(80, 80 / 30, 6), (10, 1 / 3, 1), (30, 1.0, 2), (None, 1.0, 2)],
)
def test_reorder_scaling_and_provenance(
    guests_now: int | None, scale: float, quantity: int
) -> None:
    customer = pena()
    before = customer.model_dump()
    based_on, lines = resolve_reorder(customer, "ph_pena_01", guests_now)
    past = customer.order_history[0]
    assert based_on.model_dump() == {
        "order_ref": past.order_ref,
        "title": past.title,
        "date": past.date,
        "guests_before": 30,
        "guests_now": guests_now,
        "scale": scale,
    }
    assert len(lines) == 1 and lines[0].quantity_sales_units == quantity
    assert (lines[0].product_id, lines[0].name, lines[0].sales_unit) == (
        past.lines[0].product_id,
        past.lines[0].name,
        past.lines[0].sales_unit,
    )
    assert customer.model_dump() == before


def test_reorder_null_previous_guests_fractional_quantities_and_exact_rounding() -> None:
    chef = load_customers(overlay_dir=None)["c_chef"]
    based_on, lines = resolve_reorder(chef, "ph_chef_02", 80)
    assert based_on.guests_before is None and based_on.scale == 1.0
    assert [line.quantity_sales_units for line in lines] == [3, 2]
    customer = pena()
    customer.order_history[0].lines[0].quantity_sales_units = 6.0
    assert resolve_reorder(customer, "ph_pena_01", 5)[1][0].quantity_sales_units == 1
    customer.order_history[0].lines[0].quantity_sales_units = 1.5
    assert resolve_reorder(customer, "ph_pena_01", 30)[1][0].quantity_sales_units == 2
    assert resolve_reorder(customer, "ph_pena_01", 1)[1][0].quantity_sales_units == 1


def test_reorder_missing_cross_customer_and_invalid_guests() -> None:
    for order_ref in ("missing", "ph_chef_01"):
        with pytest.raises(GatewayError) as error:
            resolve_reorder(pena(), order_ref, 30)
        assert (error.value.status_code, error.value.code) == (404, "ORDER_NOT_FOUND")
    for guests in (0, -1):
        with pytest.raises(GatewayError) as error:
            resolve_reorder(pena(), "ph_pena_01", guests)
        assert (error.value.status_code, error.value.code) == (422, "INVALID_GUESTS")


def test_latest_order_finds_newest_matching_event_and_handles_empty_history() -> None:
    customer = pena()
    customer.order_history.reverse()
    assert latest_order(customer).order_ref == "ph_pena_01"
    assert latest_order(customer, "staff_coffee").order_ref == "ph_pena_02"
    assert latest_order(customer, "missing") is None
    customer.order_history.clear()
    assert latest_order(customer) is None


async def placed_order() -> Order:
    line = await offer()
    return Order(
        order_id="o_example_01",
        quote_id="q_example",
        request_id="r_example",
        status="CONFIRMED",
        placed_at=NOW,
        placed_by=PlacedBy(type="agent", name="Example agent"),
        approved_by=None,
        simulated=True,
        authority="Invented order for history tests",
        idempotency_key="example-key",
        delivery_date=REQUESTED,
        order_deadline=None,
        lines=[line],
        total=line.line_total,
        total_with_tax=line.line_total_with_tax,
    )


@pytest.mark.parametrize("kind", ["memory", "file"])
async def test_append_idempotence_persistence_summary_and_buyer_scope(
    kind: str, tmp_path: Path
) -> None:
    path = tmp_path / "store.json"
    store: Store = FileStore(path) if kind == "file" else MemoryStore()
    order = await placed_order()
    kwargs = {"overlay_dir": tmp_path / "overlays"}

    def append():
        return append_placed_order(
            store, "c_pena", order, "Example meeting", "company_meeting", 50, NOW, **kwargs
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        appended = list(pool.map(lambda _: append(), range(4)))
    past = appended[0]
    assert all(record == past for record in appended)
    retry = append_placed_order(
        store, "c_pena", order, "Changed retry", "other", None, NOW + timedelta(days=1), **kwargs
    )
    assert retry == past
    assert past.order_ref == "ph_o_example_01" and past.date == NOW.date()
    assert past.source == "limos" and past.simulated is True and past.guests == 50
    assert past.total == order.total
    assert past.lines[0].model_dump() == {
        key: order.lines[0].model_dump()[key] for key in past.lines[0].model_dump()
    }
    if kind == "file":
        store = FileStore(path)
        assert append() == past  # Idempotence survives a file-store reload.
    loaded = get_customer("c_pena", store, **kwargs)
    assert [o.order_ref for o in loaded.order_history] == [
        "ph_o_example_01",
        "ph_pena_01",
        "ph_pena_02",
    ]
    assert latest_order(loaded).order_ref == past.order_ref
    summary = next(c for c in list_customers(store, **kwargs) if c.customer_id == "c_pena")
    assert summary.last_order is not None and summary.last_order.order_ref == past.order_ref
    assert store.get_past_orders("c_pena", "ab-oy") == [past]
    assert store.get_past_orders("c_pena", "bistro-example") == []
    assert len(get_customer("c_chef", store, **kwargs).order_history) == 2
    before = store.get_past_orders("c_pena", "ab-oy")
    with pytest.raises(GatewayError) as error:
        append_placed_order(store, "unknown", order, "Example", "other", None, NOW, **kwargs)
    assert error.value.code == "CUSTOMER_NOT_FOUND"
    assert store.get_past_orders("c_pena", "ab-oy") == before


async def test_seeded_order_can_remain_latest_and_same_date_appends_win(tmp_path: Path) -> None:
    store = MemoryStore()
    order = await placed_order()
    append_placed_order(
        store,
        "c_pena",
        order,
        "Older example",
        "other",
        None,
        NOW.replace(month=9),
        overlay_dir=tmp_path,
    )
    summary = next(
        c for c in list_customers(store, overlay_dir=tmp_path) if c.customer_id == "c_pena"
    )
    assert summary.last_order.date == date(2026, 10, 30)
    later = order.model_copy(update={"order_id": "o_example_02"})
    past = append_placed_order(
        store,
        "c_pena",
        later,
        "Same date example",
        "other",
        None,
        NOW.replace(month=10, day=30),
        overlay_dir=tmp_path,
    )
    assert get_customer("c_pena", store, overlay_dir=tmp_path).order_history[0] == past


def test_bad_seed_filename_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "wrong.json").write_text(pena().model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="file name"):
        load_customers(customers_dir=tmp_path, overlay_dir=None)
    assert (CUSTOMERS_DIR / "c_pena.json").is_file()
