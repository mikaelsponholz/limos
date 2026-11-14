"""Safe, stable decision-log codes, without remote messages or responses."""

from agent.llm import LLMConfigurationError, LLMOutputError
from gateway.errors import GatewayError
from gateway.valio.client import (
    ValioError,
    ValioHTTPError,
    ValioResponseError,
    ValioTransportError,
)


def error_code(error: Exception) -> str:
    if isinstance(error, GatewayError):
        return error.code
    if isinstance(error, LLMOutputError):
        return "LLM_OUTPUT_ERROR"
    if isinstance(error, LLMConfigurationError):
        return "LLM_CONFIGURATION_ERROR"
    if isinstance(error, ValioHTTPError):
        return "VALIO_HTTP_ERROR"
    if isinstance(error, ValioTransportError):
        return "VALIO_TRANSPORT_ERROR"
    if isinstance(error, ValioResponseError):
        return "VALIO_RESPONSE_ERROR"
    if isinstance(error, ValioError):
        return "VALIO_ERROR"
    return "INTERNAL_ERROR"
