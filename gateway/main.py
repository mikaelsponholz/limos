from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.api.health import router as health_router
from gateway.errors import register_error_handlers
from gateway.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    application = FastAPI(title="Limos")
    application.state.settings = config
    register_error_handlers(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.web_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    return application


app = create_app()
