"""Health endpoint: reports database and LLM configuration status."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.db import database_ok

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    database: bool
    llm_configured: bool
    model: str


@router.get("/health", response_model=Health)
def health() -> Health:
    settings = get_settings()
    db = database_ok()
    return Health(
        status="ok" if db else "degraded",
        database=db,
        llm_configured=bool(settings.openai_api_key),
        model=settings.llm_model,
    )
