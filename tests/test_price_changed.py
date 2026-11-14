"""Locked unit prices are optional, deterministic policy inputs."""

import pytest

from gateway.policy import ItemContext, evaluate
from tests.engine_support import AGENT, mandate
from tests.test_offers import offer


@pytest.mark.parametrize("previous", [3.35, 3.0, 4.0, 0.0])
async def test_changed_unit_price_requires_approval(previous: float) -> None:
    line = await offer()
    decision = evaluate(
        [line],
        {line.item_id: ItemContext()},
        mandate(),
        AGENT,
        previous_prices={(line.item_id, line.product_id): previous},
    )
    rules = [rule for rule in decision.rules_fired if rule.rule_id == "PRICE_CHANGED"]
    assert bool(rules) == (previous != line.unit_price)
    if rules:
        assert decision.outcome == rules[0].outcome == "NEEDS_APPROVAL"
        assert rules[0].product_id == line.product_id
        assert rules[0].item_id == line.item_id
        assert line.name in rules[0].description
        assert f"{previous:.2f} EUR" in rules[0].description
        assert "3.35 EUR" in rules[0].description
    else:
        assert decision.outcome == "AUTO"


async def test_no_input_or_unrelated_item_product_has_no_effect() -> None:
    line = await offer()
    context = {line.item_id: ItemContext()}
    baseline = evaluate([line], context, mandate(), AGENT)
    for key in (("other_item", line.product_id), (line.item_id, "900999")):
        assert evaluate([line], context, mandate(), AGENT, previous_prices={key: 1}) == baseline
