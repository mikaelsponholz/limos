from collections import deque

import pytest
from google.genai import types
from pydantic import BaseModel, Field

from agent import llm as llm_module
from agent.llm import (
    LLM,
    FakeLLM,
    GeminiLLM,
    LLMCall,
    LLMConfigurationError,
    LLMMaxStepsError,
    LLMOutputError,
    Tool,
    ToolRequest,
)
from gateway.settings import Settings


class Item(BaseModel):
    name: str
    quantity: int = Field(gt=0)


class AddArguments(BaseModel):
    left: int
    right: int


@pytest.fixture(autouse=True)
def forbid_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("A test tried to construct the real Gemini client.")

    monkeypatch.setattr(llm_module.genai, "Client", forbidden)


def settings(**overrides: str) -> Settings:
    values = {
        "gemini_api_key": "",
        "gemini_model": "invented-default",
        "gemini_expansion_model": "",
    }
    values.update(overrides)
    return Settings(**values)


def add_tool(executed: list[tuple[int, int]]) -> Tool:
    def add(left: int, right: int) -> int:
        executed.append((left, right))
        return left + right

    return Tool("add", "Add two integers.", AddArguments, add)


def test_fake_structured_success_and_call_recording() -> None:
    fake = FakeLLM(structured_outputs=[{"name": "Invented flour", "quantity": 2}])
    client: LLM = fake
    result = client.structured("Make an item.", Item, system="Use supplied facts.")
    assert isinstance(result.value, Item)
    assert result.value == Item(name="Invented flour", quantity=2)
    assert result.model_id == "fake"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.model_id == result.model_id
    assert call.config.system_instruction == "Use supplied facts."
    assert call.config.response_schema is Item
    assert call.config.response_mime_type == "application/json"
    assert "Make an item." in call.contents[0].parts[0].text


@pytest.mark.parametrize("invalid", ["not JSON", '{"name":"Invented flour","quantity":0}'])
def test_fake_repairs_once(invalid: str) -> None:
    fake = FakeLLM(
        structured_outputs=[invalid, '{"name":"Invented flour","quantity":3}'],
        settings=settings(),
    )
    result = fake.structured("Make an item.", Item)
    assert result.value.quantity == 3
    assert result.model_id == "invented-default"
    first, repair = fake.calls
    assert len(first.contents) == 1  # Request snapshots do not change during repair.
    assert [content.role for content in repair.contents] == ["user", "model", "user"]
    assert repair.contents[1].parts[0].text == invalid
    assert "Validation errors:" in repair.contents[2].parts[0].text
    assert "corrected JSON" in repair.contents[2].parts[0].text
    assert first.model_id == repair.model_id


def test_fake_two_invalid_outputs_fail_without_private_text() -> None:
    private_text = "invented-private-input"
    fake = FakeLLM(
        structured_outputs=["bad JSON", {"name": "Flour", "quantity": private_text}, {}],
        settings=settings(gemini_api_key="invented-secret"),
    )
    with pytest.raises(LLMOutputError) as caught:
        fake.structured(private_text, Item)
    assert caught.value.model_id == "invented-default"
    assert "quantity: int_parsing" in caught.value.reason
    assert len(fake.calls) == 2
    assert len(fake.structured_outputs) == 1
    assert private_text not in str(caught.value)
    assert "invented-secret" not in repr(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("batched", [False, True])
def test_fake_tool_calls_execute_in_order_then_final_answer(batched: bool) -> None:
    executed: list[tuple[int, int]] = []
    first = ToolRequest("add", {"left": 2, "right": 3}, id="call-1")
    second = ToolRequest("add", {"left": 5, "right": 7}, id="call-2")
    fake = FakeLLM(
        tool_responses=([[first, second]] if batched else [[first], [second]]) + ["Done."]
    )
    result = fake.run_tools("Add numbers.", [add_tool(executed)], max_steps=2)
    assert result.text == "Done."
    assert result.model_id == "fake"
    assert executed == [(2, 3), (5, 7)]
    assert [call.name for call in result.tool_calls] == ["add", "add"]
    assert [call.arguments for call in result.tool_calls] == [first.arguments, second.arguments]
    assert [call.result for call in result.tool_calls] == [5, 12]
    assert all(call.error is None for call in result.tool_calls)
    replies = [
        part.function_response
        for content in fake.calls[-1].contents
        for part in content.parts or []
        if part.function_response
    ]
    assert [reply.id for reply in replies] == ["call-1", "call-2"]
    assert [reply.response for reply in replies] == [{"output": 5}, {"output": 12}]


def test_unknown_tool_and_invalid_arguments_are_sent_back_without_execution() -> None:
    executed: list[tuple[int, int]] = []
    fake = FakeLLM(
        tool_responses=[
            [ToolRequest("unknown", {"left": 1})],
            [ToolRequest("add", {"left": "invalid", "right": 2})],
            [ToolRequest("add", {"left": 1, "right": 2, "unexpected": True})],
            "Unable to add.",
        ]
    )
    result = fake.run_tools("Add.", [add_tool(executed)], max_steps=3)
    assert executed == []
    assert len(result.tool_calls) == 3
    assert result.tool_calls[0].error == "Unknown tool name."
    assert "Invalid arguments" in result.tool_calls[1].error
    assert "extra_forbidden" in result.tool_calls[2].error
    for request in fake.calls[1:]:
        reply = request.contents[-1].parts[0].function_response
        assert "error" in reply.response
        assert "output" not in reply.response


@pytest.mark.parametrize("batched", [False, True])
def test_max_steps_includes_tool_errors_and_stops_execution(batched: bool) -> None:
    executed: list[tuple[int, int]] = []
    first = ToolRequest("unknown")
    second = ToolRequest("add", {"left": 1, "right": 2})
    fake = FakeLLM(tool_responses=[[first, second]] if batched else [[first], [second]])
    with pytest.raises(LLMMaxStepsError) as caught:
        fake.run_tools("Add.", [add_tool(executed)], max_steps=1)
    assert executed == []
    assert caught.value.model_id == "fake"
    assert len(caught.value.tool_calls) == 1
    assert caught.value.tool_calls[0].error == "Unknown tool name."
    assert "max_steps" in str(caught.value)


def test_tool_execution_exception_is_sanitized_and_returned() -> None:
    def fail(left: int, right: int) -> int:
        raise RuntimeError("invented-secret")

    fake = FakeLLM(tool_responses=[[ToolRequest("fail", {"left": 1, "right": 2})], "Failed."])
    result = fake.run_tools("Add.", [Tool("fail", "Fails.", AddArguments, fail)])
    assert result.tool_calls[0].error == "Tool execution failed."
    assert "invented-secret" not in repr(result)


def test_loop_configuration_errors_do_not_generate() -> None:
    fake = FakeLLM()
    tool = add_tool([])
    with pytest.raises(ValueError, match="positive"):
        fake.run_tools("Add.", [tool], max_steps=0)
    with pytest.raises(ValueError, match="unique"):
        fake.run_tools("Add.", [tool, tool])
    assert fake.calls == []


@pytest.mark.parametrize(
    ("configured", "requested", "expected"),
    [
        ("", None, "invented-default"),
        ("invented-expansion", "default", "invented-default"),
        ("invented-expansion", "expansion", "invented-expansion"),
        ("", "expansion", "invented-default"),
        ("invented-expansion", "explicit-model", "explicit-model"),
    ],
)
def test_gemini_model_resolution(configured: str, requested: str | None, expected: str) -> None:
    client = GeminiLLM(settings(gemini_expansion_model=configured))
    assert client.resolve_model(requested) == expected


def test_model_resolution_uses_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "environment-model")
    monkeypatch.delenv("GEMINI_EXPANSION_MODEL", raising=False)
    client = GeminiLLM()
    assert client.resolve_model() == "environment-model"
    assert client.resolve_model("expansion") == "environment-model"
    monkeypatch.setenv("GEMINI_EXPANSION_MODEL", "environment-expansion")
    assert GeminiLLM().resolve_model("expansion") == "environment-expansion"


def test_no_model_fallback_is_guessed() -> None:
    client = GeminiLLM(settings(gemini_model=""))
    with pytest.raises(LLMConfigurationError, match="GEMINI_MODEL"):
        client.structured("Make an item.", Item)


@pytest.mark.parametrize("operation", ["structured", "run_tools"])
def test_missing_key_fails_at_first_use(operation: str) -> None:
    client = GeminiLLM(settings())  # Construction must work without a key.
    with pytest.raises(LLMConfigurationError, match="GEMINI_API_KEY"):
        if operation == "structured":
            client.structured("Make an item.", Item)
        else:
            client.run_tools("Reply.", [])


class StubModels:
    def __init__(self, responses: list[types.GenerateContentResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[LLMCall] = []

    def generate_content(
        self, *, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.calls.append(
            LLMCall(model, [content.model_copy(deep=True) for content in contents], config)
        )
        return self.responses.popleft()


class StubClient:
    def __init__(self, responses: list[types.GenerateContentResponse]) -> None:
        self.models = StubModels(responses)


def response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part.from_text(text=text)])
            )
        ]
    )


def test_gemini_client_is_lazy_reused_and_translates_structured_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubClient([response("bad JSON"), response('{"name":"Flour","quantity":2}')])
    constructed: list[str] = []

    def factory(*, api_key: str) -> StubClient:
        constructed.append(api_key)
        return stub

    monkeypatch.setattr(llm_module.genai, "Client", factory)
    client = GeminiLLM(settings(gemini_api_key="invented-test-key"))
    assert constructed == []
    result = client.structured("Make an item.", Item, system="Supplied facts only.")
    assert constructed == ["invented-test-key"]
    assert result.value.quantity == 2
    assert result.model_id == "invented-default"
    first, repair = stub.models.calls
    assert first.config.response_schema is Item
    assert first.config.response_mime_type == "application/json"
    assert first.config.system_instruction == "Supplied facts only."
    assert "json_invalid" in repair.contents[-1].parts[0].text
    assert repair.model_id == first.model_id == result.model_id


def test_gemini_translation_preserves_signed_content_and_tool_call_ids() -> None:
    signed_content = types.Content(
        role="model",
        parts=[
            types.Part(
                thought_signature=b"invented-signature",
                function_call=types.FunctionCall(
                    id="call-1", name="add", args={"left": 3, "right": 4}
                ),
            )
        ],
    )
    stub = StubClient(
        [
            types.GenerateContentResponse(candidates=[types.Candidate(content=signed_content)]),
            response("The sum is 7."),
        ]
    )
    executed: list[tuple[int, int]] = []
    client: LLM = GeminiLLM(settings(gemini_expansion_model="invented-expansion"), client=stub)
    result = client.run_tools(
        "Add numbers.", [add_tool(executed)], system="Use tools.", model="expansion", max_steps=1
    )
    assert result.model_id == "invented-expansion"
    assert executed == [(3, 4)]
    first, following = stub.models.calls
    assert first.model_id == following.model_id == result.model_id
    assert first.config.system_instruction == "Use tools."
    assert first.config.automatic_function_calling.disable is True
    declaration = first.config.tools[0].function_declarations[0]
    assert declaration.name == "add"
    assert declaration.parameters_json_schema == AddArguments.model_json_schema()
    assert following.contents[1] == signed_content
    reply = following.contents[2].parts[0].function_response
    assert reply.name == "add"
    assert reply.id == "call-1"
    assert reply.response == {"output": 7}


def test_empty_tool_response_fails_clearly() -> None:
    stub = StubClient([types.GenerateContentResponse()])
    with pytest.raises(LLMOutputError, match="no final text"):
        GeminiLLM(settings(), client=stub).run_tools("Reply.", [])


@pytest.mark.parametrize("structured", [True, False])
def test_fake_exhaustion_is_explicit(structured: bool) -> None:
    fake = FakeLLM()
    with pytest.raises(AssertionError, match="queue exhausted"):
        if structured:
            fake.structured("Make an item.", Item)
        else:
            fake.run_tools("Reply.", [])
