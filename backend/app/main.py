from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import auth, health
from app.auth.dev_idp import DevIdp
from app.config import get_settings
from app.db.engine import assert_unprivileged_role, engine
from app.errors import install_error_handlers
from app.logging import configure_logging
from app.middleware import RequestContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await assert_unprivileged_role(conn)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="LexLoom API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.dev_idp = DevIdp(settings.auth_issuer, settings.auth_audience)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    return app


app = create_app()
