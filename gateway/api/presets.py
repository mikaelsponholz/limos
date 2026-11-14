"""Committed demo presets, with explicit request fields taking precedence."""

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BeforeValidator

from gateway.schemas import Preset, RequestCreate

PRESETS_DIR = Path(__file__).resolve().parents[2] / "presets"
router = APIRouter()


def load_presets() -> list[Preset]:
    return [
        Preset.model_validate_json(path.read_bytes()) for path in sorted(PRESETS_DIR.glob("*.json"))
    ]


def parse_request(value: Any) -> RequestCreate:
    if isinstance(value, RequestCreate):
        return value
    if isinstance(value, dict) and value.get("preset_id"):
        preset = next((p for p in load_presets() if p.preset_id == value["preset_id"]), None)
        if preset is not None:
            value = {**preset.request.model_dump(mode="json"), **value}
    return RequestCreate.model_validate_json(json.dumps(value))


RequestBody = Annotated[RequestCreate, BeforeValidator(parse_request)]


@router.get("/presets", response_model=list[Preset])
def presets() -> list[Preset]:
    return load_presets()
