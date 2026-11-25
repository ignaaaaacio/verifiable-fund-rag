"""Application settings, read from environment variables (Twelve-Factor style)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "FundRAG"
    environment: str = "development"

    # Supabase Postgres connection string, e.g.
    # postgresql+psycopg://postgres:<password>@db.<project>.supabase.co:5432/postgres
    database_url: str = "sqlite:///./local.db"

    # OpenAI
    openai_api_key: str = ""
    llm_model: str = "gpt-6-luna"

    # Embeddings / NLI (run locally on the CPU)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    nli_model: str = "cross-encoder/nli-deberta-v3-base"

    # Limits
    rate_limit_per_minute: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
