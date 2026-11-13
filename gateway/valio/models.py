"""Wire models retain the fields and optionality in Valio's OpenAPI schema."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel


class ValioModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class PingResponse(RootModel[JsonValue]):
    """The ping endpoint specifies an unconstrained JSON response and no request body."""


class ProductsRequest(ValioModel):
    customer: str
    requested_date: date = Field(alias="requestedDate")
    products: list[str] = Field(min_length=1, max_length=1000)


class Price(ValioModel):
    value: Decimal
    value_with_tax: Decimal = Field(alias="valueWithTax")
    valid_from: str = Field(alias="validFrom")
    valid_to: str = Field(alias="validTo")
    currency: str


class PriceWithTax(ValioModel):
    value: Decimal
    valid_from: str = Field(alias="validFrom")
    valid_to: str = Field(alias="validTo")
    currency: str


class Tax(ValioModel):
    tax_rate: Decimal = Field(alias="taxRate")
    tax_value: Decimal = Field(alias="taxValue")


class ProductPrice(ValioModel):
    product_id: str = Field(alias="productId")
    quantity: Decimal
    catalog_price: Price | None = Field(default=None, alias="catalogPrice")
    price_with_tax: PriceWithTax | None = Field(default=None, alias="priceWithTax")
    comparison_price: Price | None = Field(default=None, alias="comparisonPrice")
    comparison_unit: str | None = Field(default=None, alias="comparisonUnit")
    tax: Tax | None = None
    list_price: Price | None = Field(default=None, alias="listPrice")
    net_price_found: bool = Field(alias="netPriceFound")
    error: bool


class PriceResponse(ValioModel):
    customer_id: str = Field(alias="customerId")
    currency: str
    pricing_date: str = Field(alias="pricingDate")
    products: list[ProductPrice]


class AmountKind(StrEnum):
    EXACT = "exact"
    AMPLE = "ample"
    UNKNOWN = "unknown"


class NormalizedAmount(ValioModel):
    kind: AmountKind
    # Decimal preserves fractional quantities, which the wire schema permits.
    amount: Decimal | None = None


# Stock values of a billion or more are ample sentinels, including 9 999 999 999.
STOCK_AMPLE_THRESHOLD = Decimal(1_000_000_000)
TERMINAL_UNKNOWN_AMOUNT = Decimal(999_999)


def normalize_amount(logistic_model: str | None, amount: Decimal) -> NormalizedAmount:
    if logistic_model == "Stock" and amount >= STOCK_AMPLE_THRESHOLD:
        return NormalizedAmount(kind=AmountKind.AMPLE)
    if logistic_model == "Terminal" and amount == TERMINAL_UNKNOWN_AMOUNT:
        return NormalizedAmount(kind=AmountKind.UNKNOWN)
    return NormalizedAmount(kind=AmountKind.EXACT, amount=amount)


class ScheduleLine(ValioModel):
    sales_block: bool = Field(alias="salesBlock")
    available_amount: Decimal = Field(alias="availableAmount")
    available_date: str | None = Field(default=None, alias="availableDate")
    sales_unit: str = Field(alias="salesUnit")


class ProductAvailability(ValioModel):
    product_id: str = Field(alias="productId")
    closing_time: str | None = Field(default=None, alias="closingTime")
    sales_block: bool = Field(alias="salesBlock")
    logistic_model: str | None = Field(default=None, alias="logisticModel")
    schedule_lines: list[ScheduleLine] = Field(alias="scheduleLines")

    @property
    def normalized_amounts(self) -> list[NormalizedAmount]:
        return [
            normalize_amount(self.logistic_model, line.available_amount)
            for line in self.schedule_lines
        ]

    @property
    def deliverable(self) -> bool:
        return not self.sales_block and any(
            not line.sales_block and line.available_amount > 0 for line in self.schedule_lines
        )


class AvailabilityResponse(ValioModel):
    time_now: str = Field(alias="timeNow")
    requested_date: str = Field(alias="requestedDate")
    products: list[ProductAvailability]
