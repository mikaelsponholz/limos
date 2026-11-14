"""Read a stock photo into fill levels the buyer can check before ordering.

Reading and ordering are separate on purpose: the buyer sees what the model read, can fix a
number, and then sends the levels to POST /requests as stock_levels.
"""

import binascii
from base64 import b64decode

from fastapi import APIRouter

from agent.stock import match_stock
from agent.vision import read_stock_levels
from gateway import customers
from gateway.api.gateway_tools import Env
from gateway.errors import GatewayError
from gateway.schemas import StockReadBody, StockReading

router = APIRouter()

MAX_IMAGE_BYTES = 8 * 1024 * 1024


@router.post("/stock/read", response_model=StockReading)
async def read_stock(body: StockReadBody, env: Env) -> StockReading:
    customer = customers.get_customer(body.customer_id, env.store, **env.customer_paths)
    try:
        size = len(b64decode(body.image_base64, validate=True))
    except (binascii.Error, ValueError):
        raise GatewayError(422, "IMAGE_INVALID", "The image is not valid base64.") from None
    if size > MAX_IMAGE_BYTES:
        raise GatewayError(413, "IMAGE_TOO_LARGE", "The image is larger than 8 MB.")
    levels = await read_stock_levels(
        body.image_base64,
        body.mime_type,
        [item.name for item in customer.stock],
        env.settings,
        getattr(env.state, "vision_transport", None),
    )
    # Known products are returned under their stock-list name, so the request matches them.
    matched: dict[str, float] = {}
    unmatched: list[str] = []
    for name, level in levels.items():
        stocked = match_stock(customer, name)
        if stocked is None:
            unmatched.append(name)
        else:
            matched[stocked.name] = level
    return StockReading(levels=matched, unmatched=unmatched, model=env.settings.nvidia_vision_model)
