"""FastAPI entry point. All endpoints live under /v1."""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api import health
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")
    app.include_router(health.router, prefix="/v1")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    return app


app = create_app()
