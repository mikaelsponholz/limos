"""Public API contract v0.6. Money and quantities serialize as JSON numbers."""

from datetime import date
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    model_validator,
)

from gateway.catalog.models import AllergenCode as AllergenCode
from gateway.catalog.models import ClaimCode as ClaimCode
from gateway.catalog.models import LabelCode as LabelCode

CONTRACT_VERSION = "0.6"

PreferenceKey = Literal[
    "lactose_free", "gluten_free", "finnish_origin", "organic", "vegetarian", "vegan", "low_carbon"
]
AllergenCheck = Literal[
    "no_milk",
    "no_lactose",
    "no_gluten",
    "no_wheat",
    "no_egg",
    "no_fish",
    "no_crustaceans",
    "no_molluscs",
    "no_peanuts",
    "no_tree_nuts",
    "no_soy",
    "no_sesame",
    "no_celery",
    "no_mustard",
    "no_sulphites",
    "no_lupin",
]
Constraint = PreferenceKey | AllergenCheck
Unit = Literal["g", "kg", "ml", "L", "pcs"]
Priority = Literal["price", "balanced", "quality"]
AgeGroup = Literal["under_35", "35_to_54", "55_plus"]
PresetId = Literal["restaurant_weekly", "wedding_120", "birthday_kids_25", "company_lunch_60"]
Actor = Literal["user", "agent", "gateway", "policy", "human"]
Outcome = Literal["AUTO", "NEEDS_APPROVAL", "BLOCK"]
RuleId = Literal[
    "AGENT_NOT_PERMITTED",
    "SALES_BLOCK",
    "NOT_DELIVERABLE",
    "PRICE_MISSING",
    "ALLERGEN_CONTAINS",
    "DIETARY_VIOLATED",
    "SPEND_HARD_CAP",
    "BUDGET_HARD_STOP",
    "ALLERGEN_MAY_CONTAIN",
    "ALLERGEN_DATA_MISSING",
    "DIETARY_UNKNOWN",
    "PREFERENCE_REQUIRED_UNMET",
    "SPEND_SOFT_LIMIT",
    "LINE_SOFT_LIMIT",
    "BUDGET_EXCEEDED",
    "LATE_DELIVERY",
    "PARTIAL_QUANTITY",
    "SPLIT_DELIVERY",
    "SUBSTITUTION",
    "OVERBUY",
    "PACK_SIZE_UNKNOWN",
    "TERMINAL_LOW_RELIABILITY",
    "PRICE_CHANGED",
    "APPROVAL_ALWAYS_REQUIRED",
    "CUTOFF_PASSED",
]
RequestStatus = Literal[
    "RUNNING", "NEEDS_INPUT", "WAITING_APPROVAL", "ORDERED", "BLOCKED", "REJECTED", "FAILED"
]
QuoteStatus = Literal[
    "DRAFT",
    "AUTO_APPROVED",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "BLOCKED",
    "EXPIRED",
    "ORDERED",
]
ApprovalStatus = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"]
StepType = Literal[
    "REQUEST_RECEIVED",
    "CUSTOMER_LOADED",
    "INTENT_PARSED",
    "CLARIFICATION_REQUESTED",
    "CLARIFICATION_ANSWERED",
    "SUGGESTIONS_PROPOSED",
    "SUGGESTION_ACCEPTED",
    "EVENT_EXPANDED",
    "TOOL_CALL",
    "CANDIDATES",
    "CHOICE",
    "EXCEPTION",
    "SUBSTITUTE_PROPOSED",
    "QUOTE_CREATED",
    "POLICY_DECISION",
    "APPROVAL_REQUESTED",
    "APPROVED",
    "REJECTED",
    "QUOTE_EXPIRED",
    "REQUOTED",
    "AVAILABILITY_RECHECKED",
    "ORDER_PLACED",
    "ERROR",
]
OfferFlag = Literal[
    "LATE_DELIVERY",
    "PARTIAL",
    "NOT_DELIVERABLE",
    "SALES_BLOCK",
    "PRICE_MISSING",
    "CUTOFF_PASSED",
    "TERMINAL_LOW_RELIABILITY",
    "MAY_CONTAIN_EXCLUDED",
    "ALLERGEN_DATA_MISSING",
    "PACK_SIZE_UNKNOWN",
    "OVERBUY",
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Error(ContractModel):
    code: str
    message: str


class ErrorEnvelope(ContractModel):
    error: Error


class Health(ContractModel):
    status: Literal["ok"]
    # Health failure values are unspecified; preserve the existing route's unconfigured state.
    valio_api: Literal["ok", "unconfigured"]
    # "unavailable": DATABASE_URL is set but the database does not answer (wrong password, down).
    database: Literal["ok", "unconfigured", "unavailable"]


class Quantity(ContractModel):
    value: float
    unit: Unit


class ComparisonPrice(ContractModel):
    value: float
    # Comparison units are unspecified in prose; fixtures use Valio's uppercase KG and L.
    unit: Literal["KG", "L"]


class Allergens(ContractModel):
    contains: list[AllergenCode]
    may_contain: list[AllergenCode]
    data_available: bool


class Preference(ContractModel):
    key: PreferenceKey
    level: Literal["require", "prefer"]


class CustomerDefaults(ContractModel):
    priority: Priority
    typical_guests: int | None
    budget: float | None


class PastOrderLine(ContractModel):
    product_id: str
    name: str
    quantity_sales_units: float
    sales_unit: str
    quantity_text: str
    unit_price: float
    line_total: float


class PastOrder(ContractModel):
    order_ref: str
    date: date
    title: str
    event_type: str
    guests: int | None
    lines: list[PastOrderLine]
    total: float
    source: Literal["seeded", "limos"]
    simulated: Literal[True]


class Customer(ContractModel):
    customer_id: str
    name: str
    company: str
    role: str
    buyer_id: str
    age_group: AgeGroup
    playful: bool
    preferences: list[Preference]
    exclude_allergens: list[AllergenCode]
    defaults: CustomerDefaults
    order_history: list[PastOrder]
    simulated: Literal[True]


class LastOrder(ContractModel):
    order_ref: str
    date: date
    title: str


class CustomerSummary(ContractModel):
    customer_id: str
    name: str
    company: str
    role: str
    age_group: AgeGroup
    playful: bool
    last_order: LastOrder | None
    simulated: Literal[True]


class Customers(RootModel[list[CustomerSummary]]):
    model_config = ConfigDict(strict=True)


class RequestItem(ContractModel):
    description: str
    quantity: float
    unit: Unit
    constraints: list[Constraint]


class EventDish(ContractModel):
    name: str
    servings: int | None


class Event(ContractModel):
    # Event type has examples but no closed enum in the contract.
    type: str
    date: date
    guests: int


class RequestEvent(Event):
    dishes: list[EventDish]


class RequestCreate(ContractModel):
    buyer_id: str | None = None
    customer_id: str | None = None
    reorder_of: str | None = None
    # An earlier request this one changes; its open approval is replaced, not ordered.
    revises: str | None = None
    # Create-field defaults are unspecified; omissions enable each of the five input forms.
    preset_id: PresetId | None = None
    text: str | None = None
    requested_date: date | None = None
    event: RequestEvent | None = None
    items: list[RequestItem] = Field(default_factory=list)
    preferences: list[Preference] = Field(default_factory=list)
    exclude_allergens: list[AllergenCode] = Field(default_factory=list)
    budget: float | None = None
    priority: Priority = "balanced"

    @model_validator(mode="after")
    def require_input(self) -> Self:
        if not (self.buyer_id or self.customer_id or self.revises):
            raise ValueError("At least one of buyer_id or customer_id must be set.")
        if not (self.preset_id or self.text or self.event or self.items or self.reorder_of):
            raise ValueError(
                "At least one of preset_id, text, event, items or reorder_of must be set."
            )
        if self.revises and not self.text:
            raise ValueError("A revision needs the buyer's change as text.")
        return self


class RequestCreated(ContractModel):
    request_id: str
    status: Literal["RUNNING"]


class Dish(ContractModel):
    dish_id: str
    name: str
    servings: int


class ItemUse(ContractModel):
    dish_id: str
    quantity: float


class IntentItem(RequestItem):
    item_id: str
    description_fi: str
    used_in: list[ItemUse]


class BasedOn(ContractModel):
    order_ref: str
    title: str
    date: date
    guests_before: int | None
    guests_now: int | None
    scale: float


class FilledGap(ContractModel):
    field: str
    value: JsonValue
    source: Literal["text", "form", "history", "profile", "default", "answer"]
    note: str


class Intent(ContractModel):
    event: Event | None
    dishes: list[Dish]
    items: list[IntentItem]
    deliver_by: date
    preferences: list[Preference]
    exclude_allergens: list[AllergenCode]
    budget: float | None
    priority: Priority
    assumptions: list[str]
    based_on: BasedOn | None
    filled_gaps: list[FilledGap]


class Question(ContractModel):
    question_id: str
    field: Literal["guests", "deliver_by", "items", "event_type", "other"]
    text: str
    choices: list[str]
    default: str | None
    asked_at: AwareDatetime


class AnswerBody(ContractModel):
    question_id: str
    answer: str | None


class Step(ContractModel):
    seq: int
    at: AwareDatetime
    actor: Actor
    type: StepType
    title: str
    # Detail has no fixed schema; fixtures carry different JSON facts for each step type.
    detail: dict[str, JsonValue]
    model: str | None
    prev_hash: str | None
    hash: str


class Steps(RootModel[list[Step]]):
    model_config = ConfigDict(strict=True)


class Offer(ContractModel):
    product_id: str
    item_id: str
    name: str
    vendor: str
    # Sales unit codes are upstream identifiers, not the contract's quantity-unit enum.
    sales_unit: str
    pack_description: str
    pack_size: Quantity | None
    quantity_sales_units: float
    quantity_requested: Quantity
    quantity_covered: Quantity
    # Missing-price nullability is unspecified; fixtures always supply numeric price fields.
    unit_price: float
    list_price: float
    comparison_price: ComparisonPrice
    line_total: float
    line_total_with_tax: float
    tax_rate: float
    available_amount: float | None
    quantity_text: str
    available_date: date | None
    requested_date: date
    delivery_date: date | None
    # Closing-time nullability is unspecified; the undeliverable fixture uses null.
    closing_time: AwareDatetime | None
    order_deadline: AwareDatetime | None
    logistic_model: Literal["Stock", "Terminal"]
    delivery_reliability: float | None
    carbon_kg_co2e_per_kg: float | None
    country_of_origin: str
    labels: list[LabelCode]
    allergens: Allergens
    dietary_check: dict[Constraint, Literal["confirmed", "violated", "unknown"]]
    tier: Literal["good", "better", "best"] | None
    score: float
    reason: str
    is_split: bool
    is_substitute: bool
    substitute_for: str | None
    substitute_source: Literal["valio", "agent"] | None
    flags: list[OfferFlag]


class PriceInsight(ContractModel):
    """A price fact for the buyer; it informs only and never changes the quote."""

    kind: Literal[
        "delivery_date",
        "price_change",
        "season",
        "waste",
        "pack_size",
        "discount",
        "per_guest",
        "price_trend",
    ]
    item_id: str | None
    product_id: str | None
    text: str
    # Euros the buyer could save by acting on it, when that is known.
    saving: float | None


class Suggestion(ContractModel):
    suggestion_id: str
    kind: Literal["seasonal", "holiday", "trend"]
    title: str
    reason: str
    replaces_item_id: str | None
    offer: Offer | None
    status: Literal["OFFERED", "ACCEPTED", "DECLINED"]


class FiredRule(ContractModel):
    rule_id: RuleId
    description: str
    outcome: Outcome
    item_id: str | None = None
    product_id: str | None = None


class Mandate(ContractModel):
    buyer_id: str
    auto_limit: float
    line_limit: float
    hard_cap: float
    max_overbuy_percent: float
    waste_percent: float
    always_require_approval: bool = False


class PolicyDecision(ContractModel):
    outcome: Outcome
    rules_fired: list[FiredRule]
    mandate: Mandate


class Budget(ContractModel):
    limit: float
    status: Literal["within", "over", "hard_stop"]


class Quote(ContractModel):
    quote_id: str
    request_id: str
    buyer_id: str
    agent_id: str
    status: QuoteStatus
    option_label: str | None
    delivery_date: date
    order_deadline: AwareDatetime | None
    lines: list[Offer]
    total: float
    total_with_tax: float
    co2e_total_kg: float | None
    budget: Budget | None
    created_at: AwareDatetime
    expires_at: AwareDatetime
    content_hash: str
    policy: PolicyDecision
    agent_explanation: str


class PlacedBy(ContractModel):
    # Placed-by types are not enumerated separately; use the contract's actor codes.
    type: Actor
    name: str


class Order(ContractModel):
    order_id: str
    quote_id: str
    request_id: str
    status: Literal["CONFIRMED", "FAILED"]
    placed_at: AwareDatetime
    placed_by: PlacedBy
    approved_by: str | None
    simulated: bool
    authority: str
    idempotency_key: str
    delivery_date: date
    order_deadline: AwareDatetime | None
    lines: list[Offer]
    total: float
    total_with_tax: float


class RequestInput(ContractModel):
    text: str | None
    image_url: str | None


class RequestDetail(ContractModel):
    request_id: str
    buyer_id: str
    customer_id: str | None
    preset_id: PresetId | None
    status: RequestStatus
    created_at: AwareDatetime
    input: RequestInput
    # Pre-intent running responses are unspecified; the fixture supplies a complete intent.
    intent: Intent
    question: Question | None
    steps: list[Step]
    candidates: list[Offer]
    quote: Quote | None
    suggestions: list[Suggestion]
    approval_id: str | None
    order: Order | None
    error: Error | None
    # Older stored requests have no insights; they are computed once the quote exists.
    insights: list[PriceInsight] = Field(default_factory=list)


class RequestListEntry(ContractModel):
    request_id: str
    customer_id: str | None
    created_at: AwareDatetime
    title: str
    status: RequestStatus
    # Pre-quote totals are unspecified; request-list fixtures supply numeric totals.
    total: float
    delivery_date: date | None
    order_deadline: AwareDatetime | None


class RequestList(RootModel[list[RequestListEntry]]):
    model_config = ConfigDict(strict=True)


class Preset(ContractModel):
    preset_id: PresetId
    customer_id: str | None
    title: str
    description: str
    expected: str
    request: RequestCreate


class Presets(RootModel[list[Preset]]):
    model_config = ConfigDict(strict=True)


class ApprovalListEntry(ContractModel):
    approval_id: str
    request_id: str
    quote_id: str
    buyer_id: str
    created_at: AwareDatetime
    summary: str
    reasons: list[RuleId]
    order_deadline: AwareDatetime | None
    expires_at: AwareDatetime


class ApprovalList(RootModel[list[ApprovalListEntry]]):
    model_config = ConfigDict(strict=True)


class ApprovalRequestSummary(ContractModel):
    request_id: str
    title: str
    intent: Intent


class ApprovalDetail(ContractModel):
    approval_id: str
    status: ApprovalStatus
    quote: Quote
    request: ApprovalRequestSummary
    options: list[Quote] = Field(max_length=3)
    decided_by: str | None
    decided_at: AwareDatetime | None
    comment: str | None


class ApproveBody(ContractModel):
    approver: str
    quote_id: str
    content_hash: str
    # Approve-comment omission/nullability is unspecified; the fixture supplies a string.
    comment: str


class ApproveResponse(ContractModel):
    status: Literal["APPROVED"]
    order: Order


class RejectBody(ContractModel):
    approver: str
    reason: str


class RejectResponse(ContractModel):
    status: Literal["REJECTED"]


class Audit(ContractModel):
    request_id: str
    chain_valid: bool
    steps: list[Step]


class AgentIdentity(ContractModel):
    # The tool table omits some identities; its introductory rule requires both on every call.
    buyer_id: str
    agent_id: str


class SearchBody(AgentIdentity):
    query: str
    constraints: list[Constraint]
    exclude_allergens: list[AllergenCode]
    limit: int


class SearchProduct(ContractModel):
    product_id: str
    name: str
    vendor: str
    sales_unit: str
    pack_size: Quantity | None
    country_of_origin: str
    labels: list[LabelCode]
    allergens: Allergens


class SearchResponse(ContractModel):
    products: list[SearchProduct]


class OffersBody(AgentIdentity):
    product_ids: list[str]
    item_id: str
    quantity: float
    unit: Unit
    requested_date: date
    constraints: list[Constraint]
    exclude_allergens: list[AllergenCode]


class OffersResponse(ContractModel):
    offers: list[Offer]


class QuoteLine(ContractModel):
    item_id: str
    product_id: str
    quantity_sales_units: float
    substitute_for: str | None = None
    substitute_source: Literal["valio", "agent"] | None = None

    @model_validator(mode="after")
    def paired_substitute_fields(self) -> Self:
        if (self.substitute_for is None) != (self.substitute_source is None):
            raise ValueError("Substitute fields must be set together.")
        return self


class QuoteBody(AgentIdentity):
    request_id: str | None = None
    requested_date: date
    lines: list[QuoteLine]
    budget: float | None = None
    option_label: str | None = None
    exclude_allergens: list[AllergenCode] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)

    @model_validator(mode="after")
    def at_most_two_lines_per_item(self) -> Self:
        counts: dict[str, int] = {}
        for line in self.lines:
            counts[line.item_id] = counts.get(line.item_id, 0) + 1
            if counts[line.item_id] > 2:
                raise ValueError("An item may have at most two quote lines.")
        return self


class RequestApprovalBody(AgentIdentity):
    explanation: str
    alternative_quote_ids: list[str] | None = None


class RequestApprovalResponse(ContractModel):
    approval_id: str
    status: Literal["PENDING"]


class OrderBody(AgentIdentity):
    quote_id: str
    idempotency_key: str


def contract_models() -> tuple[type[BaseModel], ...]:
    """Discover locally defined models so new shapes cannot silently miss the export."""
    return tuple(
        value
        for name, value in sorted(globals().items())
        # Pydantic also inserts generated RootModel[list[...]] bases into module globals.
        if name.isidentifier()
        and isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == __name__
        and value is not ContractModel
    )
