"""Policy decision steps name the mandate and preserve the audit chain."""

import pytest

from gateway.audit import verify_chain
from gateway.schemas import Outcome
from tests.engine_support import AGENT, Engine, mandate


@pytest.mark.parametrize(
    ("outcome", "expected_title", "expected_rules"),
    [
        ("AUTO", "Within the {buyer_id} mandate, so the agent may order", []),
        (
            "NEEDS_APPROVAL",
            "Needs approval under the {buyer_id} mandate",
            ["SPEND_SOFT_LIMIT"],
        ),
        ("BLOCK", "Blocked under the {buyer_id} mandate", ["AGENT_NOT_PERMITTED"]),
    ],
)
async def test_policy_step_names_mandate_and_verifies_chain(
    outcome: Outcome, expected_title: str, expected_rules: list[str]
) -> None:
    engine = Engine()
    buyer_id = f"invented-{outcome.lower()}-buyer"
    quote = await engine.quote(
        buyer_id=buyer_id,
        agent_id="invented-unpermitted-agent" if outcome == "BLOCK" else AGENT,
        mandate=mandate(
            buyer_id=buyer_id,
            auto_limit=5.0 if outcome == "NEEDS_APPROVAL" else 1500.0,
        ),
    )

    assert quote.policy.outcome == outcome
    steps = engine.store.get_steps(quote.request_id, buyer_id)
    assert [step.type for step in steps] == ["QUOTE_CREATED", "POLICY_DECISION"]
    decision = steps[1]
    assert decision.actor == "policy"
    assert decision.title == expected_title.format(buyer_id=buyer_id)
    assert decision.detail == {
        "quote_id": quote.quote_id,
        "outcome": outcome,
        "rules": expected_rules,
        "mandate": quote.policy.mandate.buyer_id,
    }
    assert verify_chain(steps)
