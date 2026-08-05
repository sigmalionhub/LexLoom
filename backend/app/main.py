import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.v1 import auth, conversations, evals, generators, health, ingest, status
from app.auth.dev_idp import DevIdp
from app.auth.jwks import JwksCache
from app.chat.embedding import MODEL_NAME, get_embedder
from app.config import PUBLIC_ENVS, get_settings
from app.db.engine import assert_unprivileged_role, engine
from app.errors import install_error_handlers
from app.limits import report_configuration
from app.logging import configure_logging
from app.middleware import RequestContextMiddleware
from app.redis_auth import assert_redis_requires_a_password

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await assert_unprivileged_role(conn)

    # Warm the embedder before the port starts serving. The first inference
    # compiles the ONNX graph and takes noticeably longer than every one after
    # it; without this, that cost is paid by the first visitor, on the stream
    # path, where the entire design promise is that tokens start arriving
    # quickly.
    #
    # `to_thread` because loading holds the GIL for the whole of that time and
    # uvicorn is already running this loop.
    #
    # Deliberately not wrapped in try/except. If the weights are missing this
    # raises and the container fails to start, which is the loud failure. The
    # quiet one is a container that comes up, answers /health/live, and then
    # stalls on every search while it tries to reach the network. With
    # `unless-stopped` the loud version shows up as a restart loop, which
    # `docker compose ps` reports and the runbook already reads.
    await asyncio.to_thread(get_embedder()._ensure)
    logger.info("embedding_model_ready", model=MODEL_NAME)

    yield
    await engine.dispose()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    # Before anything is constructed and long before a port opens. The stream
    # ceilings, the demo-login limiter and the ingest progress hash all live in
    # this Redis, and on the shared box it is published on host loopback where
    # every local account can reach it — see app/redis_auth.py for what an
    # anonymous one hands a neighbour.
    assert_redis_requires_a_password(settings)
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
    app.include_router(status.router)
    app.include_router(generators.router)
    app.include_router(ingest.router)
    app.include_router(evals.router)
    return app


app = create_app()
