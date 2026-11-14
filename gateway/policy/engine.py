"""Policy reads offer evidence, never re-prices, ranks, or infers product claims."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Literal, cast

from gateway.policy.mandates import MandateEntry
from gateway.schemas import (
    AllergenCode,
    Budget,
    Constraint,
    FiredRule,
    Offer,
    OfferFlag,
    Outcome,
    PolicyDecision,
    PreferenceKey,
    Quantity,
    RuleId,
)

# Required dietary preferences are hard; required sourcing preferences need approval.
# Explicit item constraints remain hard regardless of this split.
REQUIRE_HARD_DIETARY_KEYS: frozenset[PreferenceKey] = frozenset(
    {"lactose_free", "gluten_free", "vegetarian", "vegan"}
)
_HARD_STOP_FACTOR = Decimal("1.15")
_BLOCK_RULES: tuple[RuleId, ...] = (
    "AGENT_NOT_PERMITTED",
    "SALES_BLOCK",
    "NOT_DELIVERABLE",
    "PRICE_MISSING",
    "ALLERGEN_CONTAINS",
    "DIETARY_VIOLATED",
    "SPEND_HARD_CAP",
    "BUDGET_HARD_STOP",
)
_APPROVAL_RULES: tuple[RuleId, ...] = (
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
)
_RULE_ORDER = {rule: index for index, rule in enumerate(_BLOCK_RULES + _APPROVAL_RULES)}
_FLAG_RULES: tuple[tuple[OfferFlag, RuleId, str], ...] = (
    ("SALES_BLOCK", "SALES_BLOCK", "has a sales block"),
    ("NOT_DELIVERABLE", "NOT_DELIVERABLE", "is not deliverable"),
    ("PRICE_MISSING", "PRICE_MISSING", "has no confirmed price"),
    ("MAY_CONTAIN_EXCLUDED", "ALLERGEN_MAY_CONTAIN", "may contain an excluded allergen"),
    ("ALLERGEN_DATA_MISSING", "ALLERGEN_DATA_MISSING", "has missing excluded-allergen data"),
    ("LATE_DELIVERY", "LATE_DELIVERY", "is available after the requested delivery date"),
    ("OVERBUY", "OVERBUY", "exceeds the permitted overbuy after pack rounding"),
    ("PACK_SIZE_UNKNOWN", "PACK_SIZE_UNKNOWN", "has an unknown pack size"),
    (
        "TERMINAL_LOW_RELIABILITY",
        "TERMINAL_LOW_RELIABILITY",
        "has Terminal delivery reliability below the mandate threshold",
    ),
)
_UNITS = {
    "g": ("kg", Decimal("0.001")),
    "kg": ("kg", Decimal(1)),
    "ml": ("L", Decimal("0.001")),
    "L": ("L", Decimal(1)),
    "pcs": ("pcs", Decimal(1)),
}


@dataclass(frozen=True)
class ItemContext:
    """Effective item requirements, after resolving customer/request precedence.

    Pass only require-level keys in required_preferences; prefer-level keys belong
    to ranking. Excluded allergens are converted to hard no_<allergen> constraints.
    Every quoted item must have a context, even when all three collections are empty.
    """

    constraints: Sequence[Constraint] = ()
    required_preferences: Sequence[PreferenceKey] = ()
    exclude_allergens: Sequence[AllergenCode] = ()


def _decimal(value: float) -> Decimal:
    if not isfinite(value) or value < 0:
        raise ValueError("Policy amounts must be finite and nonnegative.")
    return Decimal(str(value))


def budget_status(total: float | Decimal, budget: float | None) -> Budget | None:
    """Share exact, strictly-greater budget thresholds with the quote builder."""
    if budget is None:
        return None
    amount = _decimal(float(total)) if not isinstance(total, Decimal) else total
    if not amount.is_finite() or amount < 0:
        raise ValueError("Policy amounts must be finite and nonnegative.")
    limit = _decimal(budget)
    status: Literal["within", "over", "hard_stop"] = "within"
    if amount > limit * _HARD_STOP_FACTOR:
        status = "hard_stop"
    elif amount > limit:
        status = "over"
    return Budget(limit=budget, status=status)


def _quantity(quantity: Quantity) -> tuple[str, Decimal]:
    unit, factor = _UNITS[quantity.unit]
    return unit, _decimal(quantity.value) * factor


def evaluate(
    lines: Sequence[Offer],
    item_context: Mapping[str, ItemContext],
    mandate: MandateEntry,
    agent_id: str,
    budget: float | None = None,
    previous_prices: Mapping[tuple[str, str], float] | None = None,
    *,
    needs: Mapping[str, Quantity] | None = None,
    item_descriptions: Mapping[str, str] | None = None,
) -> PolicyDecision:
    """Evaluate chosen lines, retaining all rules in PLAN section 4 order.

    PARTIAL_QUANTITY is item-wide (no product_id); split coverage is summed in
    compatible units. Each line must carry the same original item need.
    Missing context or inconsistent quantities are invalid engine inputs.
    """
    fired: dict[tuple[RuleId, str | None, str | None], FiredRule] = {}

    def fire(
        rule: RuleId,
        description: str,
        line: Offer | None = None,
        *,
        item_id: str | None = None,
    ) -> None:
        item = line.item_id if line else item_id
        product = line.product_id if line else None
        outcome: Outcome = "BLOCK" if rule in _BLOCK_RULES else "NEEDS_APPROVAL"
        fired.setdefault(
            (rule, item, product),
            FiredRule(
                rule_id=rule,
                description=description,
                outcome=outcome,
                item_id=item,
                product_id=product,
            ),
        )

    if agent_id not in mandate.allowed_agents:
        fire(
            "AGENT_NOT_PERMITTED",
            f"Agent {agent_id} is not permitted to buy for {mandate.buyer_id}.",
        )

    total = sum((_decimal(line.line_total) for line in lines), Decimal(0))
    for limit, rule, label in (
        (mandate.hard_cap, "SPEND_HARD_CAP", "hard cap"),
        (mandate.auto_limit, "SPEND_SOFT_LIMIT", "auto-approval limit"),
    ):
        if total > _decimal(limit):
            fire(
                cast(RuleId, rule),
                f"Total {total:.2f} EUR is over the {limit:.2f} EUR {label}.",
            )
    budget_view = budget_status(total, budget)
    if budget_view is not None and budget_view.status != "within":
        fire(
            "BUDGET_EXCEEDED",
            f"Total {total:.2f} EUR is over the {budget_view.limit:.2f} EUR event budget.",
        )
        if budget_view.status == "hard_stop":
            stop = _decimal(budget_view.limit) * _HARD_STOP_FACTOR
            fire(
                "BUDGET_HARD_STOP",
                f"Total {total:.2f} EUR is over the {stop:.2f} EUR budget hard-stop limit.",
            )

    by_item: dict[str, list[Offer]] = defaultdict(list)
    for line in sorted(lines, key=lambda entry: (entry.item_id, entry.product_id)):
        if line.item_id not in item_context:
            raise ValueError(f"Missing policy context for item: {line.item_id}.")
        by_item[line.item_id].append(line)
        context = item_context[line.item_id]
        previous = (previous_prices or {}).get((line.item_id, line.product_id))
        if previous is not None and _decimal(line.unit_price) != _decimal(previous):
            fire(
                "PRICE_CHANGED",
                f"{line.name} unit price changed from {previous:.2f} EUR "
                f"to {line.unit_price:.2f} EUR.",
                line,
            )
        for flag, rule, fact in _FLAG_RULES:
            if flag in line.flags:
                fire(rule, f"{line.name} {fact}.", line)

        hard = set(context.constraints) | (
            set(context.required_preferences) & REQUIRE_HARD_DIETARY_KEYS
        )
        hard.update(cast(Constraint, f"no_{code}") for code in context.exclude_allergens)
        for key in sorted(hard):
            check = line.dietary_check.get(key, "unknown")
            label = key.replace("_", " ")
            if check == "violated":
                rule = "ALLERGEN_CONTAINS" if key.startswith("no_") else "DIETARY_VIOLATED"
                fire(cast(RuleId, rule), f"{line.name} violates the {label} constraint.", line)
            elif check == "unknown" and not key.startswith("no_"):
                fire("DIETARY_UNKNOWN", f"{line.name} cannot confirm the {label} constraint.", line)

        for key in sorted(set(context.required_preferences) - hard):
            if line.dietary_check.get(key) != "confirmed":
                fire(
                    "PREFERENCE_REQUIRED_UNMET",
                    f"{line.name} does not confirm the required "
                    f"{key.replace('_', ' ')} preference.",
                    line,
                )
        if _decimal(line.line_total) > _decimal(mandate.line_limit):
            fire(
                "LINE_SOFT_LIMIT",
                f"{line.name} totals {line.line_total:.2f} EUR, over the "
                f"{mandate.line_limit:.2f} EUR line limit.",
                line,
            )
        if line.is_split:
            fire("SPLIT_DELIVERY", f"{line.name} is part of a split across products.", line)
        if line.is_substitute:
            source = {"valio": "a Valio-approved", "agent": "an agent-proposed"}.get(
                line.substitute_source or "", "a"
            )
            fire("SUBSTITUTION", f"{line.name} is {source} substitute.", line)

    for item_id, item_lines in sorted(by_item.items()):
        need_unit, need = _quantity(item_lines[0].quantity_requested)
        covered = Decimal(0)
        unknown = False
        for line in item_lines:
            unit, amount = _quantity(line.quantity_covered)
            if _quantity(line.quantity_requested) != (need_unit, need):
                raise ValueError(
                    f"Inconsistent requested or covered quantities for item: {item_id}."
                )
            if "PACK_SIZE_UNKNOWN" in line.flags:
                unknown = True
                continue
            if unit != need_unit:
                raise ValueError(
                    f"Inconsistent requested or covered quantities for item: {item_id}."
                )
            covered += amount
        if not unknown and covered < need:
            names = ", ".join(sorted({line.name for line in item_lines}))
            fire(
                "PARTIAL_QUANTITY",
                f"{names} cover {covered:g} {need_unit} of the requested {need:g} {need_unit} "
                f"for item {item_id}.",
                item_id=item_id,
            )

    for item_id, quantity in sorted((needs or {}).items()):
        if item_id not in by_item:
            unit, need = _quantity(quantity)
            if need > 0:
                name = (item_descriptions or {}).get(item_id, item_id)
                fire(
                    "PARTIAL_QUANTITY",
                    f"No suitable product found for the {name}; a person needs to choose one.",
                    item_id=item_id,
                )

    if mandate.always_require_approval and not any(
        rule.outcome == "BLOCK" for rule in fired.values()
    ):
        fire("APPROVAL_ALWAYS_REQUIRED", "This buyer approves every order personally.")

    rules = sorted(
        fired.values(),
        key=lambda rule: (
            0 if rule.outcome == "BLOCK" else 1,
            _RULE_ORDER[rule.rule_id],
            rule.item_id or "",
            rule.product_id or "",
        ),
    )
    return PolicyDecision(
        outcome=rules[0].outcome if rules else "AUTO",
        rules_fired=rules,
        mandate=mandate.contract,
    )
