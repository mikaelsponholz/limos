"""Date refreshes retain substitute provenance and require human approval."""

from datetime import date

import pytest

from tests.test_e2e import poll
from tests.test_offers import product, schedule, stock
from tests.test_pipeline import EXPLANATION, environment, item, start
from tests.test_relevance_fix import choice, picks


@pytest.mark.parametrize("source", ["agent", "valio"])
@pytest.mark.parametrize("move_before_planning", [False, True])
def test_substitute_with_moved_default_date_requires_approval(
    tmp_path, source, move_before_planning
):
    records = [
        product(
            "990001",
            names={"en": "Invented sugar paste"},
            substitutions=(
                [{"product": "990001", "substituteProduct": "990002"}] if source == "valio" else []
            ),
        ),
        product("990002", names={"en": "Invented icing"}),
        product("990003", names={"en": "Invented carrots"}),
    ]
    outputs = [
        {"text": "What date do you need the delivery?"},
        picks(
            choice("it_1", "990001", ["990002"] if source == "valio" else []),
            choice("it_2", "990003"),
        ),
    ]
    if source == "agent":
        outputs.extend([{"query": "icing"}, picks(choice("it_1", "990002"))])
    outputs.append(EXPLANATION)
    availability = [
        stock("990001", scheduleLines=[]),
        stock("990002", scheduleLines=[schedule(availableDate="2026-11-19")]),
        stock(
            "990003",
            scheduleLines=[
                schedule(availableDate="2026-11-19" if move_before_planning else "2026-11-14")
            ],
        ),
    ]
    with environment(tmp_path, records, outputs, availability=availability) as client:
        detail = start(client, requested_date=None, items=[item("sugar paste"), item("carrots")])
        assert detail.status == "NEEDS_INPUT"
        response = client.post(
            f"/requests/{detail.request_id}/answer",
            json={"question_id": detail.question.question_id, "answer": None},
        )
        assert response.status_code == 202
        detail = poll(client, detail.request_id)
        assert detail.error is None
        assert detail.intent.deliver_by == date(2026, 11, 19)
        assert detail.quote is not None
        assert detail.status == "WAITING_APPROVAL"
        assert detail.quote.policy.outcome == "NEEDS_APPROVAL"
        assert {rule.rule_id for rule in detail.quote.policy.rules_fired} == {"SUBSTITUTION"}
        line = next(line for line in detail.quote.lines if line.item_id == "it_1")
        assert (line.product_id, line.substitute_for, line.substitute_source) == (
            "990002",
            "990001",
            source,
        )
        assert line.requested_date == date(2026, 11, 19)
        assert detail.approval_id is not None and detail.order is None
        assert any(
            call[0] == "price" and "990002" in call[1] and call[2] == date(2026, 11, 19)
            for call in client.app.state.valio.calls
        )
