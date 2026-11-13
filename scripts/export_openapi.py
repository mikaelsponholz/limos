"""Export all public contract shapes: python scripts/export_openapi.py."""

import json
import sys
from pathlib import Path
from typing import Any

# Direct execution places scripts/, rather than the repository root, on sys.path.
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic.json_schema import models_json_schema

from gateway.main import app
from gateway.schemas import CONTRACT_VERSION, contract_models

SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "contract" / "openapi.json"


def build_openapi(application: FastAPI = app) -> dict[str, Any]:
    """Build fresh route schemas and merge unused contract models with component refs."""
    document = get_openapi(
        title=application.title,
        version=CONTRACT_VERSION,
        openapi_version="3.1.0",
        description=application.description,
        routes=application.routes,
    )
    _, contract = models_json_schema(
        [(model, "validation") for model in contract_models()],
        ref_template="#/components/schemas/{model}",
    )
    document.setdefault("components", {}).setdefault("schemas", {}).update(contract["$defs"])
    return document


def render_openapi(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def main() -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(render_openapi(build_openapi()), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
