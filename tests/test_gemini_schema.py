"""Gemini wire schemas stay small while local validation stays strict."""

from collections.abc import Iterable, Sequence
from copy import deepcopy
from datetime import date
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent.expansion import EventExpansion
from agent.intent import TextFacts
from agent.llm import FakeLLM, LLMOutputError, Tool, ToolRequest, gemini_schema
from agent.loop import OffersArguments, QuoteArguments, SearchArguments
from agent.question import QuestionText
from gateway.settings import Settings


@pytest.fixture(autouse=True)
def forbid_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("A schema test tried to construct the real Gemini client.")

    monkeypatch.setattr("agent.llm.genai.Client", forbidden)


def fake_llm(
    *,
    structured_outputs: Iterable[str | dict[str, JsonValue]] = (),
    tool_responses: Iterable[str | Sequence[ToolRequest]] = (),
) -> FakeLLM:
    return FakeLLM(
        settings=Settings(gemini_api_key="", gemini_model="fake", gemini_expansion_model=""),
        structured_outputs=structured_outputs,
        tool_responses=tool_responses,
    )


def assert_no_unsupported_keys(value: JsonValue) -> None:
    if isinstance(value, dict):
        assert not {"$ref", "$defs", "additionalProperties", "maxItems"}.intersection(value)
        for child in value.values():
            assert_no_unsupported_keys(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_unsupported_keys(child)


@pytest.mark.parametrize(
    "model",
    [TextFacts, QuestionText, EventExpansion, SearchArguments, OffersArguments, QuoteArguments],
)
def test_phase_three_schemas_are_reduced(model: type[BaseModel]) -> None:
    reduced = gemini_schema(model)
    assert_no_unsupported_keys(reduced)
    assert reduced["type"] == "object"
    assert reduced["properties"]


def test_nested_definitions_keep_required_and_description_fields() -> None:
    reduced = gemini_schema(EventExpansion)
    assert reduced == {
        "type": "object",
        "required": ["dishes"],
        "properties": {
            "dishes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "ingredients"],
                    "properties": {
                        "name": {"type": "string", "maxLength": 160},
                        "ingredients": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": [
                                    "description",
                                    "description_fi",
                                    "quantity_per_serving",
                                    "unit",
                                ],
                                "properties": {
                                    "description": {"type": "string", "maxLength": 120},
                                    "description_fi": {"type": "string", "maxLength": 120},
                                    "quantity_per_serving": {
                                        "type": "number",
                                        "description": "Positive quantity for one serving",
                                    },
                                    "unit": {
                                        "type": "string",
                                        "enum": ["g", "kg", "ml", "L", "pcs"],
                                    },
                                },
                            },
                        },
                    },
                },
            }
        },
    }


class KeywordFields(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"nullable": True, "propertyOrdering": ["description", "items"]},
    )

    description: str = Field(min_length=1, max_length=20, pattern="^[a-z]+$")
    items: list[QuestionText] = Field(min_length=1, max_length=2)
    title: QuestionText
    additionalProperties: str
    minimum: int = Field(ge=0, le=10)
    maximum: float = Field(gt=0, lt=10)
    kind: Literal["first", "second"] = "first"
    fixed: Literal["one"] = "one"
    day: date | None = None


def test_whitelist_preserves_data_and_does_not_mutate_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = KeywordFields.model_json_schema()
    original = deepcopy(source)
    monkeypatch.setattr(KeywordFields, "model_json_schema", lambda: source)
    reduced = gemini_schema(KeywordFields)
    assert source == original
    assert reduced == {
        "type": "object",
        "nullable": True,
        "propertyOrdering": ["description", "items"],
        "required": ["description", "items", "title", "additionalProperties", "minimum", "maximum"],
        "properties": {
            "description": {"type": "string", "minLength": 1, "maxLength": 20},
            "items": {"type": "array", "items": gemini_schema(QuestionText)},
            "title": gemini_schema(QuestionText),
            "additionalProperties": {"type": "string"},
            "minimum": {"type": "integer"},
            "maximum": {"type": "number"},
            "kind": {"type": "string", "enum": ["first", "second"]},
            "fixed": {"type": "string"},
            "day": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
        },
    }


class RecursiveNode(BaseModel):
    children: list["RecursiveNode"]


def test_recursive_references_fail_clearly() -> None:
    with pytest.raises(
        ValueError, match=r"Recursive Gemini schema reference: #/\$defs/RecursiveNode"
    ):
        gemini_schema(RecursiveNode)


def test_fake_records_json_schema_and_repairs_dropped_list_limit() -> None:
    invalid: dict[str, JsonValue] = {
        "dishes": [{"name": "Invented dish", "ingredients": []} for _ in range(25)]
    }
    valid: dict[str, JsonValue] = {"dishes": [{"name": "Invented dish", "ingredients": []}]}
    fake = fake_llm(structured_outputs=[invalid, valid])
    result = fake.structured("Expand an invented event.", EventExpansion)
    assert result.repaired
    assert len(result.value.dishes) == 1
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert call.config.response_schema is None
        assert call.config.response_json_schema == gemini_schema(EventExpansion)
    repair_text = fake.calls[1].contents[-1].parts[0].text
    assert "dishes: too_long" in repair_text


def test_dropped_limit_still_fails_after_one_repair() -> None:
    invalid: dict[str, JsonValue] = {
        "dishes": [{"name": "Invented dish", "ingredients": []} for _ in range(25)]
    }
    fake = fake_llm(structured_outputs=[invalid, invalid])
    with pytest.raises(LLMOutputError, match="dishes: too_long"):
        fake.structured("Expand an invented event.", EventExpansion)
    assert len(fake.calls) == 2


def test_tool_parameters_are_reduced_but_arguments_still_validate() -> None:
    executed: list[list[str]] = []

    def offers(item_id: str, product_ids: list[str]) -> str:
        executed.append(product_ids)
        return "Invented offers"

    fake = fake_llm(
        tool_responses=[
            [ToolRequest("offers", {"item_id": "it_invented", "product_ids": ["900701"] * 6})],
            [ToolRequest("offers", {"item_id": "it_invented", "product_ids": ["900701"]})],
            "Done.",
        ]
    )
    result = fake.run_tools(
        "Check invented offers.", [Tool("offers", "Check.", OffersArguments, offers)]
    )
    assert executed == [["900701"]]
    assert "product_ids: too_long" in result.tool_calls[0].error
    assert result.tool_calls[1].error is None
    declaration = fake.calls[0].config.tools[0].function_declarations[0]
    assert declaration.parameters_json_schema == gemini_schema(OffersArguments)
