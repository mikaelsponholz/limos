"""Deterministic guardrails exercised with invented offers, without I/O in evaluation."""

from datetime import date
from typing import Any

import pytest

from gateway.policy import ItemContext, budget_status, evaluate, get_mandate
from gateway.schemas import Offer, PolicyDecision


def offer(**changes: Any) -> Offer:
    return Offer.model_validate(
        {
            "product_id": "900301",
            "item_id": "it_pantry",
            "name": "Invented pantry beans",
            "vendor": "Invented Pantry",
            "sales_unit": "BOX",
            "pack_description": "1 kg box",
            "pack_size": {"value": 1.0, "unit": "kg"},
            "quantity_sales_units": 1.0,
            "quantity_requested": {"value": 1.0, "unit": "kg"},
            "quantity_covered": {"value": 1.0, "unit": "kg"},
            "unit_price": 10.0,
            "list_price": 10.0,
            "comparison_price": {"value": 10.0, "unit": "KG"},
            "line_total": 10.0,
            "line_total_with_tax": 11.35,
            "tax_rate": 13.5,
            "available_amount": 10.0,
            "quantity_text": "1 x 1 kg box",
            "available_date": date(2026, 11, 20),
            "requested_date": date(2026, 11, 20),
            "delivery_date": date(2026, 11, 20),
            "closing_time": None,
            "order_deadline": None,
            "logistic_model": "Stock",
            "delivery_reliability": None,
            "carbon_kg_co2e_per_kg": None,
            "country_of_origin": "",
            "labels": [],
            "allergens": {"contains": [], "may_contain": [], "data_available": True},
            "dietary_check": {},
            "tier": None,
            "score": 0.0,
            "reason": "Invented offer for policy tests.",
            "is_split": False,
            "is_substitute": False,
            "substitute_for": None,
            "substitute_source": None,
            "flags": [],
            **changes,
        }
    )


def decision(
    *lines: Offer,
    context: ItemContext | None = None,
    agent: str = "limos-gemini",
    budget: float | None = None,
) -> PolicyDecision:
    return evaluate(
        lines,
        {line.item_id: context or ItemContext() for line in lines},
        get_mandate("demo-catering"),
        agent,
        budget,
    )


def rule_ids(result: PolicyDecision) -> list[str]:
    return [rule.rule_id for rule in result.rules_fired]


def test_auto_has_no_fired_rules_and_returns_contract_mandate() -> None:
    result = decision(offer())
    assert result.outcome == "AUTO"
    assert result.rules_fired == []
    assert result.mandate == get_mandate("demo-catering").contract
    assert PolicyDecision.model_validate_json(result.model_dump_json()) == result


def test_unpermitted_agent_blocks() -> None:
    result = decision(offer(), agent="invented-agent")
    assert result.outcome == "BLOCK"
    assert rule_ids(result) == ["AGENT_NOT_PERMITTED"]
    assert result.rules_fired[0].item_id is None
    assert result.rules_fired[0].product_id is None
    assert "invented-agent" in result.rules_fired[0].description


@pytest.mark.parametrize("agent", ["limos-gemini", "limos-runner", "limos-mcp", "limos-script"])
def test_each_permitted_agent_is_auto(agent: str) -> None:
    assert decision(offer(), agent=agent).outcome == "AUTO"


@pytest.mark.parametrize(
    ("flag", "rule", "outcome"),
    [
        ("SALES_BLOCK", "SALES_BLOCK", "BLOCK"),
        ("NOT_DELIVERABLE", "NOT_DELIVERABLE", "BLOCK"),
        ("PRICE_MISSING", "PRICE_MISSING", "BLOCK"),
        ("MAY_CONTAIN_EXCLUDED", "ALLERGEN_MAY_CONTAIN", "NEEDS_APPROVAL"),
        ("ALLERGEN_DATA_MISSING", "ALLERGEN_DATA_MISSING", "NEEDS_APPROVAL"),
        ("LATE_DELIVERY", "LATE_DELIVERY", "NEEDS_APPROVAL"),
        ("OVERBUY", "OVERBUY", "NEEDS_APPROVAL"),
        ("PACK_SIZE_UNKNOWN", "PACK_SIZE_UNKNOWN", "NEEDS_APPROVAL"),
        ("TERMINAL_LOW_RELIABILITY", "TERMINAL_LOW_RELIABILITY", "NEEDS_APPROVAL"),
    ],
)
def test_offer_flag_rule(flag: str, rule: str, outcome: str) -> None:
    line = offer(flags=[flag])
    result = decision(line)
    assert result.outcome == outcome
    assert rule_ids(result) == [rule]
    fired = result.rules_fired[0]
    assert fired.outcome == outcome
    assert fired.item_id == line.item_id
    assert fired.product_id == line.product_id
    assert line.name in fired.description
    assert fired.description.endswith(".")


def test_cutoff_passed_is_only_a_flag() -> None:
    result = decision(offer(flags=["CUTOFF_PASSED"]))
    assert result.outcome == "AUTO"
    assert result.rules_fired == []


@pytest.mark.parametrize("via_exclusion", [False, True])
def test_excluded_allergen_violation_blocks(via_exclusion: bool) -> None:
    context = (
        ItemContext(exclude_allergens=["tree_nuts"])
        if via_exclusion
        else ItemContext(constraints=["no_tree_nuts"])
    )
    result = decision(offer(dietary_check={"no_tree_nuts": "violated"}), context=context)
    assert result.outcome == "BLOCK"
    assert rule_ids(result) == ["ALLERGEN_CONTAINS"]


@pytest.mark.parametrize("key", ["lactose_free", "gluten_free", "vegetarian", "vegan"])
@pytest.mark.parametrize("via_preference", [False, True])
@pytest.mark.parametrize(
    ("check", "expected_rule", "outcome"),
    [
        ("violated", "DIETARY_VIOLATED", "BLOCK"),
        ("unknown", "DIETARY_UNKNOWN", "NEEDS_APPROVAL"),
        ("confirmed", None, "AUTO"),
    ],
)
def test_hard_dietary_constraints(
    key: str, via_preference: bool, check: str, expected_rule: str | None, outcome: str
) -> None:
    context = (
        ItemContext(required_preferences=[key])
        if via_preference
        else ItemContext(constraints=[key])
    )
    result = decision(offer(dietary_check={key: check}), context=context)
    assert result.outcome == outcome
    assert rule_ids(result) == ([] if expected_rule is None else [expected_rule])


def test_absent_hard_check_is_unknown() -> None:
    result = decision(offer(), context=ItemContext(constraints=["lactose_free"]))
    assert result.outcome == "NEEDS_APPROVAL"
    assert rule_ids(result) == ["DIETARY_UNKNOWN"]


def test_missing_allergen_data_does_not_also_fire_dietary_unknown() -> None:
    result = decision(
        offer(
            flags=["ALLERGEN_DATA_MISSING"],
            allergens={"contains": [], "may_contain": [], "data_available": False},
            dietary_check={"no_tree_nuts": "unknown"},
        ),
        context=ItemContext(exclude_allergens=["tree_nuts"]),
    )
    assert result.outcome == "NEEDS_APPROVAL"
    assert rule_ids(result) == ["ALLERGEN_DATA_MISSING"]


def test_missing_allergen_and_independent_dietary_data_both_fire() -> None:
    result = decision(
        offer(flags=["ALLERGEN_DATA_MISSING"]),
        context=ItemContext(constraints=["no_tree_nuts", "vegan"]),
    )
    assert rule_ids(result) == ["ALLERGEN_DATA_MISSING", "DIETARY_UNKNOWN"]


def test_may_contain_does_not_also_fire_dietary_unknown() -> None:
    result = decision(
        offer(flags=["MAY_CONTAIN_EXCLUDED"], dietary_check={"no_tree_nuts": "unknown"}),
        context=ItemContext(constraints=["no_tree_nuts"]),
    )
    assert rule_ids(result) == ["ALLERGEN_MAY_CONTAIN"]


@pytest.mark.parametrize("key", ["finnish_origin", "organic", "low_carbon"])
@pytest.mark.parametrize("check", ["violated", "unknown", None])
def test_required_sourcing_unmet_needs_approval(key: str, check: str | None) -> None:
    result = decision(
        offer(dietary_check={} if check is None else {key: check}),
        context=ItemContext(required_preferences=[key]),
    )
    assert result.outcome == "NEEDS_APPROVAL"
    assert rule_ids(result) == ["PREFERENCE_REQUIRED_UNMET"]


@pytest.mark.parametrize("key", ["finnish_origin", "organic", "low_carbon"])
def test_required_sourcing_confirmed_is_auto(key: str) -> None:
    result = decision(
        offer(dietary_check={key: "confirmed"}), context=ItemContext(required_preferences=[key])
    )
    assert result.outcome == "AUTO"


@pytest.mark.parametrize(
    "check,rule", [("violated", "DIETARY_VIOLATED"), ("unknown", "DIETARY_UNKNOWN")]
)
def test_explicit_sourcing_item_constraint_is_hard(check: str, rule: str) -> None:
    result = decision(
        offer(dietary_check={"finnish_origin": check}),
        context=ItemContext(
            constraints=["finnish_origin"], required_preferences=["finnish_origin"]
        ),
    )
    assert rule_ids(result) == [rule]


def test_checks_without_requirements_do_not_fire_preference_rules() -> None:
    # Prefer-level keys are not passed in required_preferences; evidence alone is irrelevant.
    result = decision(offer(dietary_check={"organic": "violated", "vegan": "unknown"}))
    assert result.outcome == "AUTO"


@pytest.mark.parametrize(
    ("limit", "over", "rule", "outcome"),
    [
        (250.0, 250.01, "LINE_SOFT_LIMIT", "NEEDS_APPROVAL"),
        (1500.0, 1500.01, "SPEND_SOFT_LIMIT", "NEEDS_APPROVAL"),
        (5000.0, 5000.01, "SPEND_HARD_CAP", "BLOCK"),
    ],
)
def test_spend_boundaries(limit: float, over: float, rule: str, outcome: str) -> None:
    assert rule not in rule_ids(decision(offer(line_total=limit)))
    result = decision(offer(line_total=over))
    assert rule in rule_ids(result)
    assert result.outcome == outcome
    fired = next(entry for entry in result.rules_fired if entry.rule_id == rule)
    assert f"{over:.2f} EUR" in fired.description
    assert f"{limit:.2f} EUR" in fired.description


def test_total_is_sum_of_net_line_totals_not_gross_or_unit_price() -> None:
    lines = [
        offer(item_id=f"it_{i}", line_total=250.0, line_total_with_tax=999.0) for i in range(6)
    ]
    assert decision(*lines).outcome == "AUTO"
    result = decision(*lines, offer(item_id="it_extra", line_total=0.01))
    assert rule_ids(result) == ["SPEND_SOFT_LIMIT"]
    assert result.rules_fired[0].description == (
        "Total 1500.01 EUR is over the 1500.00 EUR auto-approval limit."
    )


@pytest.mark.parametrize(
    ("total", "outcome", "rules", "status"),
    [
        (100.0, "AUTO", [], "within"),
        (100.01, "NEEDS_APPROVAL", ["BUDGET_EXCEEDED"], "over"),
        (115.0, "NEEDS_APPROVAL", ["BUDGET_EXCEEDED"], "over"),
        (115.01, "BLOCK", ["BUDGET_HARD_STOP", "BUDGET_EXCEEDED"], "hard_stop"),
    ],
)
def test_budget_boundaries(total: float, outcome: str, rules: list[str], status: str) -> None:
    result = decision(offer(line_total=total), budget=100.0)
    assert result.outcome == outcome
    assert rule_ids(result) == rules
    view = budget_status(total, 100.0)
    assert view is not None
    assert view.status == status
    assert view.limit == 100.0


def test_no_budget_returns_none() -> None:
    assert budget_status(100.0, None) is None
    assert "BUDGET_EXCEEDED" not in rule_ids(decision(offer()))


def test_zero_budget() -> None:
    assert decision(offer(line_total=0.0), budget=0.0).outcome == "AUTO"
    assert decision(offer(line_total=0.01), budget=0.0).outcome == "BLOCK"


def test_decimal_sum_at_budget_hard_stop_boundary() -> None:
    result = decision(
        offer(line_total=0.1),
        offer(item_id="it_second", line_total=0.13),
        budget=0.2,
    )
    assert rule_ids(result) == ["BUDGET_EXCEEDED"]
    result = decision(offer(line_total=0.1), offer(item_id="it_second", line_total=0.2), budget=0.3)
    assert result.outcome == "AUTO"


@pytest.mark.parametrize("source", ["valio", "agent", None])
def test_any_substitute_needs_approval(source: str | None) -> None:
    result = decision(offer(is_substitute=True, substitute_source=source, substitute_for="900302"))
    assert result.outcome == "NEEDS_APPROVAL"
    assert rule_ids(result) == ["SUBSTITUTION"]


@pytest.mark.parametrize("covered,expected", [(0.99, ["PARTIAL_QUANTITY"]), (1.0, []), (1.01, [])])
def test_partial_quantity_boundary(covered: float, expected: list[str]) -> None:
    result = decision(offer(quantity_covered={"value": covered, "unit": "kg"}))
    assert rule_ids(result) == expected
    assert result.outcome == ("NEEDS_APPROVAL" if expected else "AUTO")
    if expected:
        assert result.rules_fired[0].item_id == "it_pantry"
        assert result.rules_fired[0].product_id is None


@pytest.mark.parametrize("second_covered,partial", [(0.49, True), (0.5, False), (0.6, False)])
def test_split_coverage_is_aggregated(second_covered: float, partial: bool) -> None:
    result = decision(
        offer(quantity_covered={"value": 0.5, "unit": "kg"}, flags=["PARTIAL"]),
        offer(
            product_id="900302",
            quantity_covered={"value": second_covered, "unit": "kg"},
            is_split=True,
            flags=["PARTIAL"],
        ),
    )
    assert result.outcome == "NEEDS_APPROVAL"
    assert rule_ids(result) == (
        ["PARTIAL_QUANTITY", "SPLIT_DELIVERY"] if partial else ["SPLIT_DELIVERY"]
    )
    split = result.rules_fired[-1]
    assert split.item_id == "it_pantry"
    assert split.product_id == "900302"


def test_split_coverage_normalizes_compatible_units() -> None:
    result = decision(
        offer(quantity_covered={"value": 0.5, "unit": "kg"}),
        offer(
            product_id="900302",
            quantity_requested={"value": 1000.0, "unit": "g"},
            quantity_covered={"value": 500.0, "unit": "g"},
            is_split=True,
        ),
    )
    assert rule_ids(result) == ["SPLIT_DELIVERY"]


def test_split_does_not_combine_different_items() -> None:
    result = decision(
        offer(quantity_covered={"value": 0.5, "unit": "kg"}),
        offer(item_id="it_other", quantity_covered={"value": 0.5, "unit": "kg"}),
    )
    assert rule_ids(result) == ["PARTIAL_QUANTITY", "PARTIAL_QUANTITY"]
    assert {rule.item_id for rule in result.rules_fired} == {"it_pantry", "it_other"}


def test_decimal_split_coverage_has_no_rounding_gap() -> None:
    result = decision(
        *[
            offer(
                product_id=f"90030{i}",
                quantity_requested={"value": 0.3, "unit": "L"},
                quantity_covered={"value": amount, "unit": "L"},
            )
            for i, amount in enumerate([0.1, 0.2])
        ]
    )
    assert result.outcome == "AUTO"


def test_strictest_rule_wins_and_retains_soft_rules_in_plan_order() -> None:
    result = decision(
        offer(line_total=6000.0, flags=["LATE_DELIVERY", "PRICE_MISSING"]),
        agent="invented-agent",
        budget=100.0,
    )
    assert result.outcome == "BLOCK"
    assert rule_ids(result) == [
        "AGENT_NOT_PERMITTED",
        "PRICE_MISSING",
        "SPEND_HARD_CAP",
        "BUDGET_HARD_STOP",
        "SPEND_SOFT_LIMIT",
        "LINE_SOFT_LIMIT",
        "BUDGET_EXCEEDED",
        "LATE_DELIVERY",
    ]


def test_rule_deduplication_and_deterministic_sorting() -> None:
    lines = [
        offer(product_id=product, item_id=item, flags=["LATE_DELIVERY", "SALES_BLOCK"])
        for item, product in [("it_z", "900302"), ("it_a", "900302"), ("it_a", "900301")]
    ]
    context = ItemContext(
        constraints=["vegan", "lactose_free", "no_tree_nuts", "no_peanuts"],
        required_preferences=["vegan", "organic", "finnish_origin"],
    )
    # Multiple unknown dietary checks and missing sourcing preferences each produce one rule.
    result = decision(*lines, context=context)
    assert result == decision(*reversed(lines), context=context)
    identities = [(rule.rule_id, rule.item_id, rule.product_id) for rule in result.rules_fired]
    assert len(identities) == len(set(identities))
    assert identities[:3] == [
        ("SALES_BLOCK", "it_a", "900301"),
        ("SALES_BLOCK", "it_a", "900302"),
        ("SALES_BLOCK", "it_z", "900302"),
    ]
    assert rule_ids(result).count("DIETARY_UNKNOWN") == 3
    assert rule_ids(result).count("PREFERENCE_REQUIRED_UNMET") == 3


def test_context_is_per_item_and_inputs_are_not_mutated() -> None:
    lines = [offer(), offer(item_id="it_other", product_id="900302")]
    before = [line.model_dump() for line in lines]
    context = {"it_pantry": ItemContext(), "it_other": ItemContext(constraints=["vegan"])}
    result = evaluate(lines, context, get_mandate("demo-catering"), "limos-gemini")
    assert rule_ids(result) == ["DIETARY_UNKNOWN"]
    assert result.rules_fired[0].item_id == "it_other"
    assert [line.model_dump() for line in lines] == before


def test_missing_item_context_is_rejected() -> None:
    with pytest.raises(ValueError, match="Missing policy context for item"):
        evaluate([offer()], {}, get_mandate("demo-catering"), "limos-gemini")


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity_covered": {"value": 1.0, "unit": "L"}},
        {"quantity_requested": {"value": 2.0, "unit": "kg"}},
    ],
)
def test_inconsistent_split_quantities_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Inconsistent requested or covered quantities"):
        decision(offer(), offer(product_id="900302", **changes))


@pytest.mark.parametrize("budget", [-1.0, float("inf"), float("nan")])
def test_invalid_budget_rejected(budget: float) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        decision(offer(), budget=budget)
