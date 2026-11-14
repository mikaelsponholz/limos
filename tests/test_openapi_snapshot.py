import inspect
import json
from typing import Any

from pydantic import BaseModel

from gateway import schemas
from scripts.export_openapi import SNAPSHOT_PATH, build_openapi, render_openapi


def test_openapi_snapshot() -> None:
    assert SNAPSHOT_PATH.is_file(), "Missing OpenAPI snapshot; run scripts/export_openapi.py"
    assert SNAPSHOT_PATH.read_text(encoding="utf-8") == render_openapi(build_openapi()), (
        "OpenAPI snapshot is stale; run scripts/export_openapi.py"
    )


def test_every_contract_model_is_exported() -> None:
    document = build_openapi()
    components = document["components"]["schemas"]
    models = {
        name
        for name, model in inspect.getmembers(schemas, inspect.isclass)
        # Only declared names, excluding Pydantic's generated RootModel[list[...]] bases.
        if name.isidentifier()
        and issubclass(model, BaseModel)
        and model.__module__ == schemas.__name__
        and model is not schemas.ContractModel
    }
    assert models <= components.keys(), f"Missing contract models: {models - components.keys()}"
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == schemas.CONTRACT_VERSION == "0.6"
    health_schema = document["paths"]["/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert health_schema == {"$ref": "#/components/schemas/Health"}


def test_component_references_resolve() -> None:
    document = build_openapi()

    def check(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                assert ref.startswith("#/components/schemas/"), ref
                assert (
                    ref.removeprefix("#/components/schemas/") in document["components"]["schemas"]
                )
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(document)
    assert "$defs" not in json.dumps(document)
