"""Buyer configuration validation, using only invented configuration in error cases."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gateway.errors import GatewayError
from gateway.policy import UnknownBuyerError, get_mandate, load_mandates
from gateway.schemas import Mandate


@pytest.mark.parametrize("buyer_id", ["demo-catering", "ab-oy", "bistro-example"])
def test_configured_buyer_and_contract_view(buyer_id: str) -> None:
    entry = get_mandate(buyer_id)
    assert entry.buyer_id == buyer_id
    assert entry.contract == Mandate(
        buyer_id=buyer_id,
        auto_limit=1500.0,
        line_limit=250.0,
        hard_cap=5000.0,
        max_overbuy_percent=25.0,
        waste_percent=5.0,
    )
    assert type(entry.contract) is Mandate
    assert entry.allowed_agents == ("limos-gemini", "limos-runner", "limos-mcp", "limos-script")
    assert entry.terminal_reliability_threshold == 0.9
    assert "allowed_agents" not in entry.contract.model_dump()
    assert "terminal_reliability_threshold" not in entry.contract.model_dump()


def test_all_buyer_entries_load() -> None:
    assert set(load_mandates()) == {"demo-catering", "ab-oy", "bistro-example"}


def test_unknown_buyer_domain_error() -> None:
    with pytest.raises(
        UnknownBuyerError, match="No mandate configured for buyer: invented-buyer"
    ) as e:
        get_mandate("invented-buyer")
    assert isinstance(e.value, GatewayError)
    assert e.value.status_code == 404
    assert e.value.code == "BUYER_NOT_FOUND"
    assert e.value.buyer_id == "invented-buyer"


@pytest.mark.parametrize(
    "changes",
    [
        {"auto_limit": -1},
        {"line_limit": -1},
        {"hard_cap": -1},
        {"auto_limit": 5001},
        {"max_overbuy_percent": -1},
        {"waste_percent": -1},
        {"terminal_reliability_threshold": -0.01},
        {"terminal_reliability_threshold": 1.01},
        {"allowed_agents": []},
        {"allowed_agents": [" "]},
        {"allowed_agents": ["invented-agent", "invented-agent"]},
        {"buyer_id": ""},
        {"line_limit": "250"},
        {"hard_cap": float("inf")},
        {"waste_percent": float("nan")},
        {"unexpected": True},
    ],
)
def test_invalid_configuration_rejected(tmp_path: Path, changes: dict[str, Any]) -> None:
    entry = get_mandate("demo-catering").model_dump(mode="json")
    path = tmp_path / "invented-mandates.json"
    path.write_text(json.dumps([{**entry, **changes}]), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_mandates(path)


def test_missing_required_configuration_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invented-mandates.json"
    path.write_text('[{"buyer_id": "invented-buyer"}]', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_mandates(path)


def test_duplicate_buyers_rejected(tmp_path: Path) -> None:
    entry = get_mandate("demo-catering").model_dump(mode="json")
    path = tmp_path / "invented-mandates.json"
    path.write_text(json.dumps([entry, entry]), encoding="utf-8")
    with pytest.raises(ValueError, match="buyer ids must be unique"):
        load_mandates(path)
