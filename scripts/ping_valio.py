"""Usage: python -m scripts.ping_valio [CODE CODE CODE] [--date YYYY-MM-DD]."""

import argparse
import asyncio
from datetime import date
from decimal import Decimal

from gateway.settings import Settings
from gateway.valio.client import ValioClient, ValioError
from gateway.valio.models import AmountKind

# The API's ProductsRequest examples supply these product codes.
DEFAULT_CODES = ["460013", "460063", "463989"]


def money(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.2f}"


async def run(settings: Settings, codes: list[str], requested_date: date) -> None:
    async with ValioClient(settings) as client:
        await client.ping()
        print("Valio ping: OK")
        prices = await client.price(codes, requested_date)
        availability = await client.availability(codes, requested_date)
    by_code = {product.product_id: product for product in availability.products}
    print("Code | Net price | Comparison price/unit | Stock kind and amount | Available date")
    for product in prices.products:
        net = money(None if product.catalog_price is None else product.catalog_price.value)
        comparison = money(
            None if product.comparison_price is None else product.comparison_price.value
        )
        stock = by_code[product.product_id]
        if not stock.schedule_lines:
            print(
                f"{product.product_id} | {net} | {comparison}/{product.comparison_unit or '-'}"
                " | not deliverable | -"
            )
            continue
        for line, amount in zip(stock.schedule_lines, stock.normalized_amounts, strict=True):
            quantity = (
                str(amount.amount)
                if amount.kind == AmountKind.EXACT
                else "ample"
                if amount.kind == AmountKind.AMPLE
                else "unknown amount"
            )
            status = "" if stock.deliverable else " (not deliverable)"
            print(
                f"{product.product_id} | {net} | {comparison}/{product.comparison_unit or '-'}"
                f" | {stock.logistic_model or '-'} {quantity} {line.sales_unit}{status}"
                f" | {line.available_date or '-'}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="*", default=DEFAULT_CODES)
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    if len(args.codes) != 3:
        parser.error("Supply exactly three product codes, or omit them to use the API examples.")
    settings = Settings()
    if not settings.valio_api_key:
        print("Set VALIO_API_KEY in the environment before running this script.")
        return 1
    if not settings.valio_base_url:
        print("Set VALIO_BASE_URL in the environment before running this script.")
        return 1
    try:
        asyncio.run(run(settings, args.codes, args.date))
    except ValioError as error:
        print(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
