from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, validated at import time.

    A missing variable must stop the process at startup rather than surface
    as a 500 on a user's first request.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "ci", "production"] = "local"
    api_port: int = 8100

    database_url: str = Field(description="asyncpg DSN for the application role")
    redis_url: str = Field(description="Redis DSN used as Celery broker and cache")

    error_type_base_url: str = Field(
        default="http://localhost:8100",
        description="Base URL the RFC 9457 `type` field is built from",
    )

    auth_jwks_url: str = Field(
        default="http://localhost:8100/.well-known/jwks.json",
        description="JWKS endpoint; point at Clerk to use Clerk instead of the dev IdP",
    )
    auth_issuer: str = "https://lexloom.dev-idp"
    auth_audience: str = "lexloom-api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
