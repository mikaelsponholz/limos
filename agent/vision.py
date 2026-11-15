"""Read fill levels from a stock photo with an NVIDIA Nemotron vision model.

The model only reports what it sees, as {"Milk": 25, "Spoons": 15} in percent. Quantities,
products and prices are decided later by code and the policy engine, never by this model.
Neither the image, the API key nor the raw model reply is ever logged.
"""

import json
import re

import httpx

from gateway.errors import GatewayError
from gateway.settings import Settings

PROMPT = (
    "You see a photo of a kitchen or storeroom. For each product you can see, estimate how "
    "full its shelf, box or container is, in percent: 0 is empty, 100 is full. "
    "Use these names when a product matches one of them: {names}. "
    'Reply with one JSON object only, for example {{"Milk": 25, "Spoons": 15}}, '
    "with whole numbers and no other text."
)

_OBJECT = re.compile(r"\{[^{}]*\}")


def parse_levels(text: str) -> dict[str, float]:
    """Take the last JSON object of plain names and 0-100 numbers from the model's reply."""
    for candidate in reversed(_OBJECT.findall(text)):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or not value:
            continue
        levels: dict[str, float] = {}
        for name, level in value.items():
            if (
                not isinstance(name, str)
                or not name.strip()
                or isinstance(level, bool)
                or not isinstance(level, int | float)
            ):
                break
            levels[name.strip()] = float(min(max(level, 0), 100))
        else:
            return levels
    raise GatewayError(502, "VISION_OUTPUT_INVALID", "The vision model gave no stock levels.")


async def read_stock_levels(
    image_base64: str,
    mime_type: str,
    names: list[str],
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, float]:
    if not settings.nvidia_api_key:
        raise GatewayError(503, "VISION_UNAVAILABLE", "Set NVIDIA_API_KEY to read stock photos.")
    body = {
        "model": settings.nvidia_vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT.format(names=", ".join(names) or "none")},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 1024,
    }
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=settings.nvidia_timeout_seconds
        ) as client:
            response = await client.post(
                settings.nvidia_base_url.rstrip("/") + "/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {settings.nvidia_api_key}"},
            )
    except httpx.HTTPError:
        raise GatewayError(502, "VISION_FAILED", "The vision model could not be reached.") from None
    if response.status_code != 200:
        # The status only; the body can echo the request.
        raise GatewayError(
            502, "VISION_FAILED", f"The vision model answered HTTP {response.status_code}."
        )
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise GatewayError(
            502, "VISION_OUTPUT_INVALID", "The vision model gave no stock levels."
        ) from None
    if not isinstance(content, str):
        raise GatewayError(502, "VISION_OUTPUT_INVALID", "The vision model gave no stock levels.")
    return parse_levels(content)
