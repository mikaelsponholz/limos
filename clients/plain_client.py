"""Run a fixed purchasing flow using only HTTP and the Python standard library."""

import argparse
import sys
from typing import Any

import httpx

AGENT_ID = "limos-script"
REQUESTED_DATE = "2026-11-17"
BLOCKING_FLAGS = {"NOT_DELIVERABLE", "SALES_BLOCK", "PRICE_MISSING"}


class ClientError(Exception):
    """A gateway error or an item with no usable offers."""


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=body)
    if not response.is_success:
        try:
            error = response.json()["error"]
            message = f"{error['code']}: {error['message']}"
        except (ValueError, KeyError, TypeError):
            message = f"HTTP_{response.status_code}: Gateway request failed."
        raise ClientError(message)
    return response.json()


def run_flow(client: httpx.Client, buyer_id: str = "demo-catering") -> int:
    """Use an injected HTTP client and print only the order or approval summary."""
    identity = {"buyer_id": buyer_id, "agent_id": AGENT_ID}
    try:
        lines = []
        for item_id, query, quantity, unit, constraints in (
            ("it_butter", "butter", 5.0, "kg", []),
            ("it_milk", "milk", 40.0, "L", ["lactose_free"]),
        ):
            requirements = {"constraints": constraints, "exclude_allergens": []}
            products = _post(
                client,
                "/gateway/search",
                {**identity, **requirements, "query": query, "limit": 5},
            )["products"]
            if not products:
                raise ClientError(f"NO_CANDIDATES: No products match {query}.")
            offers = _post(
                client,
                "/gateway/offers",
                {
                    **identity,
                    **requirements,
                    "product_ids": [product["product_id"] for product in products],
                    "item_id": item_id,
                    "quantity": quantity,
                    "unit": unit,
                    "requested_date": REQUESTED_DATE,
                },
            )["offers"]
            chosen = next(
                (offer for offer in offers if not BLOCKING_FLAGS.intersection(offer["flags"])),
                None,
            )
            if chosen is None:
                raise ClientError(f"NO_USABLE_OFFERS: No offer without blocking flags for {query}.")
            lines.append(
                {
                    "item_id": item_id,
                    "product_id": chosen["product_id"],
                    "quantity_sales_units": chosen["quantity_sales_units"],
                }
            )
        quote = _post(
            client,
            "/gateway/quotes",
            {**identity, "requested_date": REQUESTED_DATE, "lines": lines},
        )
        outcome = quote["policy"]["outcome"]
        reference = None
        if outcome == "AUTO":
            order = _post(
                client,
                "/gateway/orders",
                {
                    **identity,
                    "quote_id": quote["quote_id"],
                    "idempotency_key": f"limos-script-{quote['quote_id']}",
                },
            )
            reference = f"Order id: {order['order_id']} (simulated)"
        elif outcome == "NEEDS_APPROVAL":
            approval = _post(
                client,
                f"/gateway/quotes/{quote['quote_id']}/request-approval",
                {
                    **identity,
                    "explanation": "Please review the butter and lactose-free milk order.",
                },
            )
            reference = f"Approval id: {approval['approval_id']}"

        print(f"Outcome: {outcome}")
        for line in quote["lines"]:
            print(f"{line['name']}: {line['quantity_text']} | {line['line_total']:.2f} EUR net")
        print(f"Total: {quote['total']:.2f} EUR net")
        print(f"Delivery date: {quote['delivery_date']}")
        print(f"Order deadline: {quote['order_deadline'] or 'unknown'}")
        if reference:
            print(reference)
        rules = quote["policy"]["rules_fired"]
        print(
            "Rules fired: "
            + ("; ".join(f"{rule['rule_id']}: {rule['description']}" for rule in rules) or "none")
        )
        return 1 if outcome == "BLOCK" else 0
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except httpx.RequestError:
        print("GATEWAY_UNAVAILABLE: Could not reach the gateway.", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description="Order butter and lactose-free milk through Limos")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--buyer-id", default="demo-catering")
    args = parser.parse_args(argv)
    if client is not None:
        return run_flow(client, args.buyer_id)
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=10) as http_client:
        return run_flow(http_client, args.buyer_id)


if __name__ == "__main__":
    raise SystemExit(main())
