from typing import Annotated

from fastapi import APIRouter, Depends

from gateway.settings import Settings, get_settings

router = APIRouter()


@router.get("/health")
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    return {
        "status": "ok",
        "valio_api": "ok" if settings.valio_api_key else "unconfigured",
        "database": "ok" if settings.store in {"memory", "file"} else "unconfigured",
    }
