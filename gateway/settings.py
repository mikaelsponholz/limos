import os
from dataclasses import dataclass, field
from typing import cast

from starlette.requests import Request


@dataclass(frozen=True)
class Settings:
    agent_mode: str = field(default_factory=lambda: os.getenv("AGENT_MODE", "pipeline"))
    valio_api_key: str = field(default_factory=lambda: os.getenv("VALIO_API_KEY", ""), repr=False)
    valio_base_url: str = field(default_factory=lambda: os.getenv("VALIO_BASE_URL", ""))
    valio_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("VALIO_TIMEOUT_SECONDS", "5"))
    )
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""), repr=False)
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", ""))
    limos_gateway_url: str = field(
        default_factory=lambda: os.getenv("LIMOS_GATEWAY_URL", "http://127.0.0.1:8000")
    )
    limos_request_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("LIMOS_REQUEST_TIMEOUT_SECONDS", "10"))
    )
    gemini_expansion_model: str = field(
        default_factory=lambda: (
            os.getenv("GEMINI_EXPANSION_MODEL", "") or os.getenv("GEMINI_MODEL", "")
        )
    )
    store: str = field(default_factory=lambda: os.getenv("STORE", "file"))
    catalog_path: str = field(
        default_factory=lambda: os.getenv("CATALOG_PATH", "data/valio/catalog.slim.json")
    )
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""), repr=False)
    migrate_on_start: bool = field(
        default_factory=lambda: os.getenv("MIGRATE_ON_START", "").lower() in {"1", "true", "yes"}
    )
    web_origin: str = field(
        default_factory=lambda: os.getenv("WEB_ORIGIN", "http://localhost:3000")
    )

    def __post_init__(self) -> None:
        if self.agent_mode not in {"pipeline", "tools"}:
            raise ValueError("AGENT_MODE must be pipeline or tools.")

    @property
    def web_origins(self) -> list[str]:
        return [origin.strip() for origin in self.web_origin.split(",") if origin.strip()]


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)
