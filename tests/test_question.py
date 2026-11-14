"""One-question decisions and resumable answers, using invented offline inputs."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent.intent import resolve_request
from agent.llm import FakeLLM, LLMConfigurationError
from agent.question import FALLBACK_TEXT, choices_and_default, missing_field
from gateway.audit import verify_chain
from tests.agent_loop_support import EXPLANATION, purchase
from tests.test_e2e import poll
from tests.test_intent import TODAY, client_for, facts, gap_sources, post_text, profile, request

ITEMS: list[JsonValue] = [{"description": "flour", "quantity": 2.0, "unit": "kg"}]


@pytest.mark.parametrize("with_history", [False, True])
def test_vague_lunch_asks_only_without_a_guest_source(tmp_path: Path, with_history: bool) -> None:
    outputs: list[dict[str, JsonValue]] = [{"event_type": "company_meeting"}]
    if not with_history:
        outputs.append({"text": "How many people is the lunch for?"})
    llm = FakeLLM(structured_outputs=outputs)
    with client_for(tmp_path, llm) as client:
        values: dict[str, JsonValue] = {"customer_id": "invented-customer"} if with_history else {}
        detail = post_text(client, text="Lunch for the team", requested_date="2026-11-20", **values)
        if with_history:
            assert detail.question is None
            assert gap_sources(detail)["event.guests"] == "history"
            assert not any(step.type == "CLARIFICATION_REQUESTED" for step in detail.steps)
        else:
            assert detail.status == "NEEDS_INPUT" and detail.question is not None
            assert detail.question.field == "guests" and detail.question.default == "30"
            assert not any(step.type == "TOOL_CALL" for step in detail.steps)
            assert detail.candidates == [] and detail.quote is None


@pytest.mark.parametrize("with_history", [False, True])
def test_empty_intent_asks_items_first_with_past_titles(tmp_path: Path, with_history: bool) -> None:
    llm = FakeLLM(structured_outputs=[{}, {"text": "What would you like to order?"}])
    with client_for(tmp_path, llm) as client:
        values: dict[str, JsonValue] = {"customer_id": "invented-customer"} if with_history else {}
        detail = post_text(client, text="Please help", **values)
        assert detail.status == "NEEDS_INPUT" and detail.question is not None
        assert detail.question.field == "items"
        expected = ["Invented team coffee", "Invented board meeting"] if with_history else []
        assert detail.question.choices == expected
        assert detail.question.default == (expected[0] if expected else None)
        assert detail.question.asked_at == detail.created_at
        assert detail.question.question_id.startswith("qn_")
        prompt = llm.calls[1].contents[0].parts[0].text
        assert prompt is not None
        payload = json.loads(prompt.split("\nReturn JSON")[0])
        assert set(payload) == {"field", "event_type", "choices"}
        assert payload["choices"] == expected
        for private in (
            "Invented Buyer",
            "Invented Kitchen",
            "900701",
            "2026-10-01",
            "invented-board",
        ):
            assert private not in prompt


@pytest.mark.parametrize("answer,source,guests", [("60", "answer", 60), (None, "default", 30)])
def test_guest_answer_resumes_to_quote_without_a_second_question(
    tmp_path: Path, answer: str | None, source: str, guests: int
) -> None:
    llm = FakeLLM(
        structured_outputs=[
            {"event_type": "company_lunch", "items": ITEMS, "dishes": [{"name": "Lunch"}]},
            {"text": "How many people is the lunch for?"},
            {
                "dishes": [
                    {
                        "name": "Lunch",
                        "ingredients": [
                            {
                                "description": "flour",
                                "description_fi": "jauho",
                                "quantity_per_serving": 1.0,
                                "unit": "g",
                            }
                        ],
                    }
                ]
            },
            EXPLANATION,
        ],
        tool_responses=purchase(),
    )
    with client_for(tmp_path, llm) as client:
        detail = post_text(client, text="Lunch for the team, including 2 kg flour")
        assert detail.question is not None and detail.question.field == "guests"
        assert len(llm.calls) == 2  # Intent parsing and question wording precede expansion.
        response = client.post(
            f"/requests/{detail.request_id}/answer",
            json={"question_id": detail.question.question_id, "answer": answer},
        )
        assert response.status_code == 202, response.text
        assert response.json() == {"request_id": detail.request_id, "status": "RUNNING"}
        result = poll(client, detail.request_id)
        assert result.status == "WAITING_APPROVAL" and result.quote is not None
        assert result.question is None and result.intent.event is not None
        assert result.intent.event.guests == result.intent.dishes[0].servings == guests
        assert gap_sources(result)["event.guests"] == source
        assert gap_sources(result)["deliver_by"] == "default"
        assert sum(step.type == "CLARIFICATION_REQUESTED" for step in result.steps) == 1
        assert sum(step.type == "CLARIFICATION_ANSWERED" for step in result.steps) == 1
        assert sum(step.type == "REQUEST_RECEIVED" for step in result.steps) == 1
        assert len(llm.calls) == 8  # Expansion, four tool turns and the explanation.
        assert sum(step.type == "EVENT_EXPANDED" for step in result.steps) == 1
        assert verify_chain(result.steps)
        replay = client.post(
            f"/requests/{detail.request_id}/answer",
            json={"question_id": detail.question.question_id, "answer": "60"},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "NO_OPEN_QUESTION"


@pytest.mark.parametrize("answer", ["0", "-1", "1.5", "many", "", "1e3"])
def test_invalid_guests_and_wrong_question_leave_question_open(tmp_path: Path, answer: str) -> None:
    llm = FakeLLM(structured_outputs=[{"event_type": "lunch"}, {"text": "How many people?"}])
    with client_for(tmp_path, llm) as client:
        detail = post_text(client)
        assert detail.question is not None
        for question_id, status in [("wrong-question", 409), (detail.question.question_id, 422)]:
            response = client.post(
                f"/requests/{detail.request_id}/answer",
                json={"question_id": question_id, "answer": answer},
            )
            assert response.status_code == status
            if status == 409:
                assert response.json()["error"]["code"] == "NO_OPEN_QUESTION"
        unchanged = poll(client, detail.request_id)
        assert unchanged.status == "NEEDS_INPUT" and unchanged.question == detail.question
        assert unchanged.steps == detail.steps


def test_missing_delivery_date_is_not_asked(tmp_path: Path) -> None:
    # Busy buyers get the next delivery day, and the filled gap says so.
    llm = FakeLLM(structured_outputs=[{"items": ITEMS}, EXPLANATION], tool_responses=purchase())
    with client_for(tmp_path, llm) as client:
        result = post_text(client)
    assert result.question is None and result.quote is not None
    assert result.intent.deliver_by.isoformat() == "2026-11-15"
    assert gap_sources(result)["deliver_by"] == "default"


@pytest.mark.parametrize("answer", ["Invented board meeting", None])
def test_items_answer_maps_title_to_customer_reorder(tmp_path: Path, answer: str | None) -> None:
    llm = FakeLLM(structured_outputs=[{}, {"text": "What would you like to order?"}])
    with client_for(tmp_path, llm) as client:
        detail = post_text(client, customer_id="invented-customer")
        assert detail.question is not None
        response = client.post(
            f"/requests/{detail.request_id}/answer",
            json={"question_id": detail.question.question_id, "answer": answer},
        )
        assert response.status_code == 202
        result = poll(client, detail.request_id)
        assert result.quote is not None and result.intent.based_on is not None
        assert result.intent.based_on.title == (answer or "Invented team coffee")
        assert gap_sources(result)["items"] == ("default" if answer is None else "answer")
        assert len(llm.calls) == 2 and verify_chain(result.steps)


@pytest.mark.parametrize("valid", [False, True])
def test_items_free_text_is_parsed_and_invalid_text_keeps_question(
    tmp_path: Path, valid: bool
) -> None:
    llm = FakeLLM(
        structured_outputs=[
            {},
            {"text": "What would you like to order?"},
            {"items": ITEMS} if valid else {},
            EXPLANATION,
        ],
        tool_responses=purchase(),
    )
    with client_for(tmp_path, llm) as client:
        detail = post_text(client)
        assert detail.question is not None
        response = client.post(
            f"/requests/{detail.request_id}/answer",
            json={
                "question_id": detail.question.question_id,
                "answer": "2 kg flour" if valid else "unclear",
            },
        )
        result = poll(client, detail.request_id)
        if valid:
            assert response.status_code == 202 and result.quote is not None
            assert result.intent.items[0].description == "flour"
            assert result.intent.event is None
            assert not any(gap.field.startswith("event.") for gap in result.intent.filled_gaps)
            assert gap_sources(result)["items"] == "answer"
            assert gap_sources(result)["deliver_by"] == "default"
            assert result.steps[-1].model is None  # Quote and policy stay deterministic.
            assert any(
                step.type == "INTENT_PARSED" and step.model == "fake" and "answer" in step.title
                for step in result.steps
            )
        else:
            assert response.status_code == 422
            assert result.status == "NEEDS_INPUT" and result.question == detail.question
        assert len(llm.calls) == (8 if valid else 3)


def test_items_null_without_history_leaves_system_default(tmp_path: Path) -> None:
    llm = FakeLLM(structured_outputs=[{}, {"text": "What would you like to order?"}])
    with client_for(tmp_path, llm) as client:
        detail = post_text(client)
        assert detail.question is not None and detail.question.default is None
        response = client.post(
            f"/requests/{detail.request_id}/answer",
            json={"question_id": detail.question.question_id, "answer": None},
        )
        assert response.status_code == 202
        result = poll(client, detail.request_id)
        assert result.intent.items == [] and result.question is None
        assert gap_sources(result)["items"] == "default"
        assert result.status == "FAILED" and result.error is not None
        assert result.error.code == "AGENT_NOT_AVAILABLE"


@pytest.mark.parametrize(
    "bad_text", ["", "First? Second?", "x" * 141, "No question", "A sentence. Question?"]
)
def test_invalid_wording_falls_back_and_logs_model(tmp_path: Path, bad_text: str) -> None:
    llm = FakeLLM(structured_outputs=[{}, {"text": bad_text}, {"text": bad_text}])
    with client_for(tmp_path, llm) as client:
        detail = post_text(client)
        assert detail.status == "NEEDS_INPUT" and detail.question is not None
        assert detail.question.text == FALLBACK_TEXT["items"]
        step = next(step for step in detail.steps if step.type == "CLARIFICATION_REQUESTED")
        assert step.detail["fallback_used"] is True and step.model == "fake"
        assert verify_chain(detail.steps)


@pytest.mark.parametrize("from_form", [False, True])
def test_event_date_avoids_question_and_delivery_keeps_system_default(from_form: bool) -> None:
    customer = profile()
    customer.order_history.clear()
    body = (
        request(event={"type": "lunch", "date": "2026-11-20", "guests": 50, "dishes": []})
        if from_form
        else request()
    )
    resolved = resolve_request(
        body, customer, facts(event_type="lunch", event_date="2026-11-20"), TODAY
    )
    assert missing_field(resolved.intent, resolved.reorder_target) is None
    assert resolved.intent.deliver_by.isoformat() == "2026-11-15"
    assert (
        next(g for g in resolved.intent.filled_gaps if g.field == "deliver_by").source == "default"
    )
    assert resolved.intent.event is not None
    assert resolved.intent.event.date.isoformat() == "2026-11-20"
    if not from_form:
        assert (
            next(g for g in resolved.intent.filled_gaps if g.field == "event.guests").source
            == "profile"
        )
    choices, default = choices_and_default("guests", resolved.intent, customer, TODAY)
    assert choices == ["50"] and default == "50"


def test_wording_configuration_error_uses_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(structured_outputs=[{}])

    def resolve_model(model: str | None = None) -> str:
        if llm.calls:
            raise LLMConfigurationError("Wording model is unavailable.")
        return "fake"

    monkeypatch.setattr(llm, "resolve_model", resolve_model)
    with client_for(tmp_path, llm) as client:
        detail = post_text(client)
        assert detail.status == "NEEDS_INPUT" and detail.question is not None
        assert detail.question.text == FALLBACK_TEXT["items"]
        assert detail.error is None
        step = next(step for step in detail.steps if step.type == "CLARIFICATION_REQUESTED")
        assert step.detail["fallback_used"] is True and step.model is None
        assert verify_chain(detail.steps)


def test_choices_are_bounded_and_unique() -> None:
    customer = profile()
    customer.order_history *= 4
    intent = resolve_request(request(), customer, facts(), TODAY).intent
    choices, default = choices_and_default("items", intent, customer, TODAY)
    assert choices == ["Invented team coffee", "Invented board meeting"]
    assert default == choices[0]


def test_invalid_answer_parser_output_fails_request_clearly(tmp_path: Path) -> None:
    llm = FakeLLM(
        structured_outputs=[
            {},
            {"text": "What would you like to order?"},
            {"guests": "many"},
            {"guests": "many"},
        ]
    )
    with client_for(tmp_path, llm) as client:
        detail = post_text(client)
        assert detail.question is not None
        response = client.post(
            f"/requests/{detail.request_id}/answer",
            json={"question_id": detail.question.question_id, "answer": "Some food"},
        )
        assert response.status_code == 422
        result = poll(client, detail.request_id)
        assert result.status == "FAILED" and result.question is None
        assert result.error is not None and result.error.code == "LLM_OUTPUT_ERROR"
        assert verify_chain(result.steps)
