from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from gateway.valio.models import (
    AmountKind,
    AvailabilityResponse,
    PingResponse,
    PriceResponse,
    ProductAvailability,
    ProductPrice,
    ProductsRequest,
    normalize_amount,
)


@pytest.mark.parametrize(
    ("stock_kind", "amount", "kind", "exact"),
    [
        ("Stock", "9999999999", AmountKind.AMPLE, None),
        ("Stock", "10000000000", AmountKind.AMPLE, None),
        ("Terminal", "999999", AmountKind.UNKNOWN, None),
        ("Stock", "999999", AmountKind.EXACT, Decimal(999999)),
        ("Terminal", "42", AmountKind.EXACT, Decimal(42)),
        ("Stock", "0", AmountKind.EXACT, Decimal(0)),
        (None, "1.25", AmountKind.EXACT, Decimal("1.25")),
    ],
)
def test_normalize_amount(
    stock_kind: str | None, amount: str, kind: AmountKind, exact: Decimal | None
) -> None:
    result = normalize_amount(stock_kind, Decimal(amount))
    assert result.kind == kind
    assert result.amount == exact


def test_price_preserves_all_fields_and_aliases() -> None:
    price = {
        "value": Decimal("4.25"),
        "valueWithTax": Decimal("4.80"),
        "validFrom": "2026-01-01",
        "validTo": "9999-12-31",
        "currency": "EUR",
    }
    wire = {
        "customerId": "invented-customer",
        "currency": "EUR",
        "pricingDate": "2026-11-16",
        "products": [
            {
                "productId": "TEST-PRICE-1",
                "quantity": 1,
                "catalogPrice": price,
                "priceWithTax": {
                    key: value for key, value in price.items() if key != "valueWithTax"
                },
                "comparisonPrice": price,
                "comparisonUnit": "KG",
                "tax": {"taxRate": 13, "taxValue": Decimal("0.55")},
                "listPrice": price,
                "netPriceFound": True,
                "error": False,
            }
        ],
    }
    response = PriceResponse.model_validate(wire)
    product = response.products[0]
    assert product.catalog_price is not None
    assert product.catalog_price.value == Decimal("4.25")
    assert product.catalog_price.valid_to == "9999-12-31"
    assert product.tax is not None and product.tax.tax_rate == Decimal(13)
    assert response.model_dump(by_alias=True) == wire


def test_optional_price_fields() -> None:
    product = ProductPrice.model_validate(
        {"productId": "TEST-MISSING", "quantity": 1, "netPriceFound": False, "error": True}
    )
    assert product.catalog_price is None
    assert product.comparison_price is None
    assert product.error


@pytest.mark.parametrize("amount", [0, 12, 9999999999])
@pytest.mark.parametrize("blocked", [False, True])
def test_availability_fields_and_deliverability(amount: int, blocked: bool) -> None:
    wire = {
        "timeNow": "2026-11-13T12:00:00",
        "requestedDate": "2026-11-16T00:00:00",
        "products": [
            {
                "productId": "TEST-STOCK",
                "closingTime": "2026-11-15T15:00:00",
                "salesBlock": False,
                "logisticModel": "Stock",
                "scheduleLines": [
                    {
                        "salesBlock": blocked,
                        "availableAmount": amount,
                        "availableDate": "2026-11-16",
                        "salesUnit": "KG",
                    }
                ],
            }
        ],
    }
    response = AvailabilityResponse.model_validate(wire)
    assert response.model_dump(by_alias=True) == wire
    product = response.products[0]
    assert product.deliverable == (not blocked and amount > 0)
    assert product.normalized_amounts[0] == normalize_amount("Stock", Decimal(amount))
    product.sales_block = True
    assert not product.deliverable


def test_unknown_product_empty_schedule_is_not_deliverable() -> None:
    product = ProductAvailability.model_validate(
        {"productId": "TEST-UNKNOWN", "salesBlock": False, "scheduleLines": []}
    )
    assert not product.deliverable
    assert product.normalized_amounts == []


@pytest.mark.parametrize("size", [0, 1001])
def test_request_batch_limits(size: int) -> None:
    with pytest.raises(ValidationError):
        ProductsRequest(
            customer="test", requested_date=date(2026, 11, 16), products=["TEST"] * size
        )


def test_request_date_alias_and_ping_unconstrained_json() -> None:
    request = ProductsRequest(
        customer="test", requested_date=date(2026, 11, 16), products=["TEST-1"]
    )
    assert request.model_dump(mode="json", by_alias=True) == {
        "customer": "test",
        "requestedDate": "2026-11-16",
        "products": ["TEST-1"],
    }
    assert PingResponse.model_validate({"test": True}).root == {"test": True}
