"""A Valio substitute promoted because its original has incomplete data stays a substitute."""

from tests.test_offers import product, stock
from tests.test_pipeline import EXPLANATION, environment, item, start
from tests.test_relevance_fix import choice, picks


def test_substitute_for_incomplete_original_still_requires_approval(tmp_path):
    records = [
        product(
            "990001",
            names={"en": "Invented sugar paste"},
            substitutions=[{"product": "990001", "substituteProduct": "990002"}],
        ),
        product("990002", names={"en": "Invented icing"}),
    ]
    outputs = [picks(choice("it_1", "990002", [])), EXPLANATION]
    # The original has no logistic model, so its offer is skipped as incomplete data.
    availability = [stock("990001", logisticModel=None), stock("990002")]
    with environment(tmp_path, records, outputs, availability=availability) as client:
        detail = start(client, items=[item("sugar paste")])
        assert detail.error is None
        assert detail.quote is not None
        line = detail.quote.lines[0]
        assert (line.product_id, line.substitute_for, line.substitute_source) == (
            "990002",
            "990001",
            "valio",
        )
        assert "SUBSTITUTION" in {rule.rule_id for rule in detail.quote.policy.rules_fired}
        assert detail.status == "WAITING_APPROVAL" and detail.order is None
