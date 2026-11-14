"""Typed Gemini calls and an offline fake with the same validation and tool loop."""

import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from google import genai
from google.genai import types
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from gateway.settings import Settings


class LLMConfigurationError(Exception):
    """Required Gemini configuration is missing."""


class LLMOutputError(Exception):
    def __init__(self, model_id: str, reason: str) -> None:
        self.model_id = model_id
        self.reason = reason
        super().__init__(f"LLM output from {model_id}: {reason}")


class StructuredResult[T: BaseModel](BaseModel):
    """The validated schema instance plus the model used for the decision log."""

    value: T
    model_id: str
    repaired: bool = False


@dataclass(frozen=True)
class Tool:
    """A synchronous callable; validated field names become keyword arguments."""

    name: str
    description: str
    arguments: type[BaseModel]
    function: Callable[..., object] = field(repr=False)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, JsonValue]
    result: JsonValue = None
    error: str | None = None

    def response(self) -> dict[str, JsonValue]:
        return {"error": self.error} if self.error is not None else {"output": self.result}


@dataclass(frozen=True)
class ToolResult:
    text: str
    tool_calls: list[ToolCall]
    model_id: str


class LLMMaxStepsError(LLMOutputError):
    def __init__(self, model_id: str, tool_calls: list[ToolCall]) -> None:
        self.tool_calls = list(tool_calls)
        super().__init__(model_id, "max_steps reached before a final answer")


class LLM(Protocol):
    def structured[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        model: str | None = None,
    ) -> StructuredResult[T]: ...

    def run_tools(
        self,
        prompt: str,
        tools: Sequence[Tool],
        *,
        system: str | None = None,
        model: str | None = None,
        max_steps: int = 10,
        on_call: Callable[[str, str], None] | None = None,
    ) -> ToolResult: ...


def _user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def _validation_reason(error: ValidationError) -> str:
    # Error types and field paths are enough for repair; omit input values,
    # custom error messages and context which could contain private text.
    return "; ".join(
        f"{'.'.join(map(str, detail['loc'])) or '$'}: {detail['type']}"
        for detail in error.errors(include_input=False, include_context=False, include_url=False)[
            :5
        ]
    )[:300]


class _ValidatedLLM(ABC):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def resolve_model(self, model: str | None = None) -> str:
        """`default` and `expansion` select settings; other values are explicit IDs."""
        if model == "expansion":
            model_id = self.settings.gemini_expansion_model or self.settings.gemini_model
        elif model is None or model == "default":
            model_id = self.settings.gemini_model
        else:
            model_id = model
        if not model_id.strip():
            raise LLMConfigurationError(
                "Set GEMINI_MODEL after confirming an ID with python -m scripts.list_models."
            )
        return model_id

    @abstractmethod
    def _generate(
        self, model_id: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse: ...

    def structured[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        model: str | None = None,
    ) -> StructuredResult[T]:
        model_id = self.resolve_model(model)
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
        )
        contents = [_user(prompt + "\nReturn JSON matching the supplied response schema.")]
        for attempt in range(2):
            response = self._generate(model_id, contents, config)
            raw = response.text or ""
            try:
                value = schema.model_validate_json(raw)
            except ValidationError as error:
                reason = _validation_reason(error)
                if attempt == 1:
                    raise LLMOutputError(model_id, reason) from None
                contents.extend(
                    [
                        types.Content(role="model", parts=[types.Part.from_text(text=raw)]),
                        _user(
                            f"Validation errors: {reason}\n"
                            "Return corrected JSON matching the supplied schema."
                        ),
                    ]
                )
            else:
                return StructuredResult[T](value=value, model_id=model_id, repaired=attempt == 1)
        raise AssertionError("The repair loop must return or raise.")

    def run_tools(
        self,
        prompt: str,
        tools: Sequence[Tool],
        *,
        system: str | None = None,
        model: str | None = None,
        max_steps: int = 10,
        on_call: Callable[[str, str], None] | None = None,
    ) -> ToolResult:
        """Bound attempted tool calls, including errors and calls in a batch.

        A final text turn is allowed after the last permitted tool call.
        Exceeding the budget raises with the ordered calls already attempted.
        """
        if max_steps < 1:
            raise ValueError("max_steps must be positive.")
        by_name = {tool.name: tool for tool in tools}
        if len(by_name) != len(tools):
            raise ValueError("Tool names must be unique.")
        model_id = self.resolve_model(model)
        declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters_json_schema=tool.arguments.model_json_schema(),
            )
            for tool in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = [_user(prompt)]
        calls: list[ToolCall] = []
        while True:
            response = self._generate(model_id, contents, config)
            requested = response.function_calls or []
            if not requested:
                if not response.text or not response.text.strip():
                    raise LLMOutputError(model_id, "no final text or tool calls returned")
                return ToolResult(response.text, calls, model_id)
            # Keep the original parts, including thought signatures required by Gemini.
            assert response.candidates and response.candidates[0].content
            contents.append(response.candidates[0].content)
            replies: list[types.Part] = []
            for request in requested:
                if len(calls) >= max_steps:
                    raise LLMMaxStepsError(model_id, calls)
                if on_call is not None:
                    on_call(request.name or "", model_id)
                call = _execute_tool(request, by_name)
                calls.append(call)
                replies.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=request.id, name=call.name, response=call.response()
                        )
                    )
                )
            contents.append(types.Content(role="user", parts=replies))


def _execute_tool(request: types.FunctionCall, tools: dict[str, Tool]) -> ToolCall:
    name = request.name or ""
    arguments = request.args or {}
    tool = tools.get(name)
    if tool is None:
        return ToolCall(name, arguments, error="Unknown tool name.")
    try:
        # Forbid undeclared arguments even when the caller's schema ignores extras.
        validated = tool.arguments.model_validate(arguments, extra="forbid")
    except ValidationError as error:
        return ToolCall(name, arguments, error="Invalid arguments: " + _validation_reason(error))
    try:
        result = tool.function(
            **{name: getattr(validated, name) for name in tool.arguments.model_fields}
        )
        if isinstance(result, BaseModel):
            result = result.model_dump(mode="json")
        output = TypeAdapter(JsonValue).validate_python(result)
    except Exception:
        # Tool exceptions may include credentials or remote response bodies.
        return ToolCall(name, arguments, error="Tool execution failed.")
    return ToolCall(name, arguments, result=output)


class SDKModels(Protocol):
    def generate_content(
        self, *, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse: ...


class SDKClient(Protocol):
    @property
    def models(self) -> SDKModels: ...


class GeminiLLM(_ValidatedLLM):
    def __init__(
        self, settings: Settings | None = None, *, client: SDKClient | None = None
    ) -> None:
        super().__init__(settings if settings is not None else Settings())
        self._client = client

    def _generate(
        self, model_id: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        if self._client is None:
            if not self.settings.gemini_api_key.strip():
                raise LLMConfigurationError("Set GEMINI_API_KEY before calling Gemini.")
            self._client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._client.models.generate_content(
            model=model_id, contents=contents, config=config
        )


@dataclass(frozen=True)
class ToolRequest:
    """One model-requested tool call in a FakeLLM scripted turn."""

    name: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)
    id: str | None = None


@dataclass(frozen=True)
class LLMCall:
    """A snapshot of each generation, including structured repair requests."""

    model_id: str
    contents: list[types.Content]
    config: types.GenerateContentConfig


class FakeLLM(_ValidatedLLM):
    def __init__(
        self,
        *,
        structured_outputs: Iterable[str | dict[str, JsonValue]] = (),
        tool_responses: Iterable[str | Sequence[ToolRequest]] = (),
        settings: Settings | None = None,
    ) -> None:
        # This invented identifier is only for offline tests, never a Gemini fallback.
        super().__init__(settings or Settings(gemini_model="fake", gemini_expansion_model=""))
        self.structured_outputs = deque(structured_outputs)
        self.tool_responses = deque(tool_responses)
        self.calls: list[LLMCall] = []

    def _generate(
        self, model_id: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.calls.append(
            LLMCall(
                model_id,
                [content.model_copy(deep=True) for content in contents],
                config.model_copy(deep=True),
            )
        )
        if config.response_mime_type == "application/json":
            if not self.structured_outputs:
                raise AssertionError("FakeLLM structured output queue exhausted.")
            output = self.structured_outputs.popleft()
            parts = [
                types.Part.from_text(text=output if isinstance(output, str) else json.dumps(output))
            ]
        else:
            if not self.tool_responses:
                raise AssertionError("FakeLLM tool response queue exhausted.")
            turn = self.tool_responses.popleft()
            parts = (
                [types.Part.from_text(text=turn)]
                if isinstance(turn, str)
                else [
                    types.Part(
                        function_call=types.FunctionCall(
                            name=request.name, args=request.arguments, id=request.id
                        )
                    )
                    for request in turn
                ]
            )
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=parts))]
        )
