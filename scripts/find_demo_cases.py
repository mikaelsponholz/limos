"""Find catalogue candidates offline, or verify small Valio shortlists with --live.

Usage: python scripts/find_demo_cases.py [--catalog PATH] [--live] [--date YYYY-MM-DD]
Offline rows are candidates only: the slim catalogue contains no prices or stock.
Real codes are written only to the ignored .runtime/demo_cases.json output.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.catalog.index import Catalog
from gateway.catalog.models import SlimRecord
from gateway.offers import OfferDataError, build_offers
from gateway.policy import ItemContext
from gateway.policy.engine import evaluate
from gateway.policy.mandates import load_mandates
from gateway.schemas import OffersBody
from gateway.settings import Settings
from gateway.valio.client import ValioClient
from gateway.valio.models import AmountKind, AvailabilityResponse, PriceResponse
from scripts.seed_customers import SEED_SPEC

ROOT = Path(__file__).resolve().parents[1]
# Guests in the demo request: "same as last time for the wedding, 80 people, Saturday".
REORDER_GUESTS = 80
# substitute_scan: an original with at most this much exact stock counts as short, and its
# substitute needs at least this much (or ample stock).
SCAN_MAX_STOCK = 200
SCAN_MIN_SUBSTITUTE_STOCK = 100
NUTS = {"tree_nuts", "peanuts"}
AGENT = "limos-runner"


def lactose_free(record: SlimRecord) -> bool:
    evidence = record.allergens
    return not {"lactose"} & set(
        evidence.contains + evidence.may_contain + evidence.undeclared
    ) and ("lactose" in evidence.free_from or "lactose_free" in {*record.claims, *record.labels})


def nut_free(record: SlimRecord) -> bool:
    evidence = record.allergens
    return (
        evidence.data_available
        and NUTS <= set(evidence.free_from)
        and not NUTS & set(evidence.contains + evidence.may_contain + evidence.undeclared)
        and not evidence.unknown_codes
        and not evidence.unknown_statuses
    )


def find_cases(catalog: Catalog, *, scan_substitutes: bool = False) -> list[dict]:
    """Deterministic shortlists; never infer stock, prices or safety from missing data."""
    rows: list[dict] = []

    def matches(query: str) -> list[SlimRecord]:
        return [
            record
            for record in catalog.search(query, limit=len(catalog), exclude_sales_blocked=True)
            if record.pack_size and record.sales_unit
        ]

    def add(
        preset: str,
        case: str,
        item: str,
        record: SlimRecord,
        companion: SlimRecord | None = None,
        **details: object,
    ) -> None:
        rows.append(
            {
                "preset": preset,
                "case": case,
                "item": item,
                "product_id": record.product_id,
                "companion_product_id": companion.product_id if companion else None,
                "status": "catalogue_candidate",
                **details,
            }
        )

    cream = [
        r
        for r in matches("whipping cream kuohukerma")
        if lactose_free(r) and r.pack_size.unit == "L"
    ]
    for record in cream[:3]:
        companion = next((r for r in cream if r.product_id != record.product_id), None)
        if companion:
            add(
                "wedding_120",
                "split_stock",
                "whipping cream",
                record,
                companion,
                quantity=6.3,
                unit="L",
            )
    vegetables = matches("cucumber courgette kurkku kesäkurpitsa")
    for record in vegetables:
        for substitution in record.substitutions:
            companion = catalog.lookup(substitution.substitute_product)
            if (
                substitution.product == record.product_id
                and companion
                and not companion.sales_block
                and companion.pack_size
                and record.pack_size.unit == companion.pack_size.unit
                and record.pack_size.unit == "kg"
            ):
                add(
                    "wedding_120",
                    "valio_substitute",
                    "salad vegetable",
                    record,
                    companion,
                    quantity=6.3,
                    unit="kg",
                )
        if sum(row["case"] == "valio_substitute" for row in rows) >= 3:
            break

    for item, query in (
        ("cake cream", "cream kerma"),
        ("cake", "cake kakku"),
        ("sandwich bread", "bread leipä"),
        ("ham", "ham kinkku"),
        ("fruit", "fruit hedelmä"),
    ):
        pool = matches(query)
        risky = [
            r
            for r in pool
            if NUTS & set(r.allergens.may_contain) and not NUTS & set(r.allergens.contains)
        ]
        for record in risky[:3]:
            companion = next(
                (r for r in pool if nut_free(r) and r.pack_size.unit == record.pack_size.unit), None
            )
            if companion:
                add("birthday_kids_25", "may_contain_nuts", item, record, companion)

    for item, query in (
        ("salmon soup", "salmon potato cream lohi peruna kerma"),
        ("rye bread and butter", "rye bread butter ruisleipä voi"),
        ("green salad", "lettuce cucumber salaatti kurkku"),
    ):
        pool = matches(query)
        for case, candidates in (
            ("finnish", [r for r in pool if r.country_of_origin == "fi"]),
            (
                "non_finnish",
                [r for r in pool if r.country_of_origin and r.country_of_origin != "fi"],
            ),
        ):
            for record in candidates[:3]:
                add("company_lunch_60", case, item, record)

    weekly = json.loads((ROOT / "presets" / "restaurant_weekly.json").read_bytes())["request"]
    aliases = ("milk maito", "butter voi", "potatoes peruna", "eggs kananmuna")
    for item, query in zip(weekly["items"], aliases, strict=True):
        pool = matches(query)
        for record in [
            r
            for r in pool
            if r.pack_size.unit == item["unit"]
            and ("lactose_free" not in item["constraints"] or lactose_free(r))
            and ("finnish_origin" not in item["constraints"] or r.country_of_origin == "fi")
        ][:3]:
            add(
                "restaurant_weekly",
                "auto",
                item["description"],
                record,
                quantity=item["quantity"],
                unit=item["unit"],
                constraints=item["constraints"],
            )
    # Frank's demo reorder: his latest order, resolved with the committed seed terms, for
    # REORDER_GUESTS guests. A line whose product has a Valio substitute with the same
    # pack unit is a candidate; --live checks that it is short and the substitute is not.
    seed_terms = json.loads(SEED_SPEC.read_bytes())
    frank = json.loads((SEED_SPEC.parents[1] / "customers" / "c_pena.json").read_bytes())
    past = max(frank["order_history"], key=lambda order: order["date"])
    for history_line in past["lines"]:
        record = next(
            (
                found
                for found in catalog.search(
                    seed_terms[history_line["product_id"]], exclude_sales_blocked=True
                )
                if (found.names.fi or found.names.en) and found.sales_unit
            ),
            None,
        )
        if record is None or record.pack_size is None:
            continue
        for substitution in record.substitutions:
            companion = catalog.lookup(substitution.substitute_product)
            if (
                substitution.product != record.product_id
                or companion is None
                or companion.product_id == record.product_id
                or companion.sales_block
                or companion.pack_size is None
                or companion.pack_size.unit != record.pack_size.unit
            ):
                continue
            lot = Fraction(str(record.allowed_lot_size or 1)).numerator
            scaled = (
                Fraction(str(history_line["quantity_sales_units"]))
                * REORDER_GUESTS
                / past["guests"]
            )
            units = -(-scaled // lot) * lot
            add(
                "pena_reorder",
                "reorder_shortage",
                history_line["name"],
                record,
                companion,
                quantity=float(units) * record.pack_size.value,
                quantity_sales_units=float(units),
                unit=record.pack_size.unit,
                history_product_id=history_line["product_id"],
            )
    # --scan-substitutes: every product with a same-unit Valio substitute. --live keeps those
    # whose own stock is small and exact while the substitute has stock, for a reorder shortage.
    for record in catalog.records() if scan_substitutes else ():
        if record.sales_block or record.pack_size is None or not record.substitutions:
            continue
        for substitution in record.substitutions:
            companion = catalog.lookup(substitution.substitute_product)
            if (
                substitution.product == record.product_id
                and companion is not None
                and companion.product_id != record.product_id
                and not companion.sales_block
                and companion.pack_size is not None
                and companion.pack_size.unit == record.pack_size.unit
            ):
                add(
                    "pena_reorder",
                    "substitute_scan",
                    record.names.en or record.names.fi or record.product_id,
                    record,
                    companion,
                )
                break
    return rows


class SnapshotValio:
    """Use the fetched shortlists for deterministic offer and policy checks."""

    def __init__(self, prices: PriceResponse, availability: AvailabilityResponse) -> None:
        self.prices = prices
        self.stock = availability

    async def price(self, codes: Iterable[str], requested_date: date) -> PriceResponse:
        return self.prices

    async def availability(
        self, codes: Iterable[str], requested_date: date
    ) -> AvailabilityResponse:
        return self.stock


async def verify_live(
    rows: list[dict], catalog: Catalog, settings: Settings, requested: date
) -> None:
    codes = list(
        dict.fromkeys(
            code
            for row in rows
            for code in (row["product_id"], row["companion_product_id"])
            if code
        )
    )
    prices = PriceResponse(
        customerId="demo", currency="EUR", pricingDate=str(requested), products=[]
    )
    availability = AvailabilityResponse(
        timeNow=datetime.now().astimezone().isoformat(), requestedDate=str(requested), products=[]
    )
    async with ValioClient(settings) as client:
        await client.ping()
        for start in range(0, len(codes), 20):
            shortlist = codes[start : start + 20]
            prices.products.extend((await client.price(shortlist, requested)).products)
            availability.products.extend((await client.availability(shortlist, requested)).products)
    by_price = {p.product_id: p for p in prices.products}
    by_stock = {p.product_id: p for p in availability.products}
    snapshot = SnapshotValio(prices, availability)
    mandates = load_mandates()
    for row in rows:
        try:
            ids = [code for code in (row["product_id"], row["companion_product_id"]) if code]
            row["status"] = "not_matched"
            if any(code not in by_price or code not in by_stock for code in ids):
                continue
            row["observed"] = [
                {
                    "product_id": code,
                    "price": by_price[code].model_dump(mode="json", by_alias=True),
                    "availability": by_stock[code].model_dump(mode="json", by_alias=True),
                }
                for code in ids
            ]
            if any(by_price[code].error or by_price[code].catalog_price is None for code in ids):
                continue
            stock = by_stock[ids[0]]
            timely = all(
                by_stock[code].deliverable
                and any(
                    not line.sales_block
                    and line.available_amount > 0
                    and line.available_date
                    and datetime.fromisoformat(line.available_date).date() <= requested
                    for line in by_stock[code].schedule_lines
                )
                for code in ids
            )
            if row["case"] == "reorder_shortage":
                offers = await build_offers(
                    OffersBody(
                        buyer_id="ab-oy",
                        agent_id=AGENT,
                        item_id="demo",
                        product_ids=ids,
                        quantity=row["quantity"],
                        unit=row["unit"],
                        requested_date=requested,
                        constraints=[],
                        exclude_allergens=[],
                    ),
                    catalog=catalog,
                    valio=snapshot,
                    now=datetime.now().astimezone(),
                )
                original, replacement = offers
                if (
                    original.available_amount is not None
                    and original.available_amount < row["quantity_sales_units"]
                    and "SALES_BLOCK" not in original.flags
                    and replacement.logistic_model == "Stock"
                    and replacement.quantity_covered.value >= row["quantity"]
                    and replacement.delivery_date is not None
                    and replacement.delivery_date <= requested
                    and not {"PRICE_MISSING", "NOT_DELIVERABLE", "SALES_BLOCK"}
                    & set(replacement.flags)
                ):
                    row["status"] = "verified"
            elif row["case"] == "substitute_scan":
                own = by_stock[ids[0]].normalized_amounts
                other = by_stock[ids[1]].normalized_amounts
                exact = [
                    a.amount for a in own if a.kind == AmountKind.EXACT and a.amount is not None
                ]
                if exact and len(exact) == len(own) and sum(exact) <= SCAN_MAX_STOCK:
                    spare = [
                        a.amount
                        for a in other
                        if a.kind == AmountKind.EXACT and a.amount is not None
                    ]
                    ample = any(a.kind == AmountKind.AMPLE for a in other)
                    if ample or sum(spare, Decimal(0)) >= SCAN_MIN_SUBSTITUTE_STOCK:
                        row["status"] = "verified"
                        row["stock"] = float(sum(exact))
                        row["substitute_stock"] = "ample" if ample else float(sum(spare))
            elif row["case"] == "valio_substitute":
                replacement = by_stock[ids[1]]
                if not stock.deliverable and replacement.deliverable:
                    offer = (
                        await build_offers(
                            OffersBody(
                                buyer_id="demo-catering",
                                agent_id=AGENT,
                                item_id="demo",
                                product_ids=[ids[1]],
                                quantity=row["quantity"],
                                unit=row["unit"],
                                requested_date=requested,
                                constraints=["lactose_free"],
                                exclude_allergens=[],
                            ),
                            catalog=catalog,
                            valio=snapshot,
                            now=datetime.now().astimezone(),
                        )
                    )[0]
                    policy = evaluate(
                        [offer],
                        {"demo": ItemContext(constraints=["lactose_free"])},
                        mandates["demo-catering"],
                        AGENT,
                    )
                    if policy.outcome == "AUTO":
                        row["status"] = "verified"
            elif row["case"] in {"auto", "split_stock"}:
                constraints = row.get("constraints", ["lactose_free"])
                body = OffersBody(
                    buyer_id="bistro-example",
                    agent_id=AGENT,
                    item_id="demo",
                    product_ids=ids,
                    quantity=row["quantity"],
                    unit=row["unit"],
                    requested_date=requested,
                    constraints=constraints,
                    exclude_allergens=[],
                )
                offers = await build_offers(
                    body, catalog=catalog, valio=snapshot, now=datetime.now().astimezone()
                )
                if row["case"] == "auto":
                    policy = evaluate(
                        offers,
                        {"demo": ItemContext(constraints=constraints)},
                        mandates["bistro-example"],
                        AGENT,
                    )
                    if policy.outcome == "AUTO":
                        row["status"] = "verified"
                elif (
                    timely
                    and 0 < offers[0].quantity_covered.value < row["quantity"]
                    and offers[1].quantity_covered.value >= row["quantity"]
                    and all(o.dietary_check.get("lactose_free") == "confirmed" for o in offers)
                    and any(a.kind == AmountKind.EXACT for a in stock.normalized_amounts)
                ):
                    row["status"] = "verified"
            elif timely:
                row["status"] = "verified"
        except OfferDataError:
            # Incomplete Valio data (for example no logistic model) rules out this row only.
            row["status"] = "offer_data_invalid"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/valio/catalog.slim.json")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--scan-substitutes", action="store_true")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 11, 20))
    args = parser.parse_args()
    try:
        catalog = Catalog.load(args.catalog)
        rows = (
            find_cases(catalog, scan_substitutes=True)
            if args.scan_substitutes
            else find_cases(catalog)
        )
    except (OSError, ValueError):
        print("Could not read the catalogue. Check its path and format.")
        return 1
    if args.live:
        try:
            asyncio.run(verify_live(rows, catalog, Settings(), args.date))
        except Exception as error:
            # The type only: a message could carry a URL or key.
            print(
                "Could not check Valio. Check the connection and environment authentication "
                f"({type(error).__name__})."
            )
            return 1
    destination = ROOT / ".runtime" / "demo_cases.json"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {"live": args.live, "requested_date": str(args.date), "cases": rows}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        print("Could not write the runtime demo cases.")
        return 1
    print("Preset | Case | Item | Product | Companion | Status")
    for row in rows:
        print(
            " | ".join(
                str(row[key] or "-")
                for key in (
                    "preset",
                    "case",
                    "item",
                    "product_id",
                    "companion_product_id",
                    "status",
                )
            )
        )
    if not args.live:
        print("Catalogue candidates only; run --live to check price and availability.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
