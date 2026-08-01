from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import auth, conversations, health
from app.auth.dev_idp import DevIdp
from app.auth.jwks import JwksCache
from app.config import PUBLIC_ENVS, get_settings
from app.db.engine import assert_unprivileged_role, engine
from app.errors import install_error_handlers
from app.limits import report_configuration
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
    # `settings.env`, not `os.getenv("ENV")`. The two disagree whenever the
    # value comes from `.env` rather than from the process environment, and the
    # disagreement was in the direction that starts the dev IdP on a box
    # documented as running a real identity provider.
    app.state.dev_idp = DevIdp(settings.auth_issuer, settings.auth_audience, env=settings.env)
    # PUBLIC_ENVS, not `production` alone. A plaintext JWKS fetch is an
    # on-path opening — a spoofed key set makes every subsequent token verify —
    # and that is true of any box the internet can reach, including the demo,
    # which cannot call itself `production` because it deliberately runs the
    # dev IdP that refuses to start there.
    app.state.jwks_cache = JwksCache(
        settings.auth_jwks_url, require_https=settings.env in PUBLIC_ENVS
    )
    report_configuration(settings)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(conversations.router)
    return app


app = create_app()
