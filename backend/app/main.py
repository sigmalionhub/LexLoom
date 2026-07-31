from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import health
from app.config import get_settings
from app.db.engine import assert_unprivileged_role, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await assert_unprivileged_role(conn)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="LexLoom API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(health.router)
    return app


app = create_app()
