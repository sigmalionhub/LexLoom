from fastapi import FastAPI

from app.api.v1 import health
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="LexLoom API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.include_router(health.router)
    return app


app = create_app()
