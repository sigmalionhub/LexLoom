import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.logging import request_id_var

logger = structlog.get_logger(__name__)


class ProblemError(Exception):
    """An error that serialises to RFC 9457 application/problem+json."""

    def __init__(self, *, code: str, status: int, title: str, detail: str) -> None:
        self.code = code
        self.status = status
        self.title = title
        self.detail = detail
        super().__init__(detail)

    def to_dict(self, instance: str) -> dict[str, object]:
        anchor = self.code.replace("_", "-")
        return {
            "type": f"{get_settings().error_type_base_url}/docs/errors#{anchor}",
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "instance": instance,
            "request_id": request_id_var.get(),
        }


def install_error_handlers(app: FastAPI) -> None:
    """Every error leaves this service as problem+json, without exception.

    Registering only the ProblemError handler is not enough: a Pydantic
    validation failure would still return FastAPI's `{"detail": [...]}`, and an
    unhandled exception would return a bare text/plain 500 with no `code` for
    the frontend to switch on. A contract with holes in it is not a contract.
    """

    @app.exception_handler(ProblemError)
    async def _handle(request: Request, exc: ProblemError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=exc.to_dict(request.url.path),
            media_type="application/problem+json",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """404 and 405 come from the router, not from our code.

        Without this they leave as FastAPI's `{"detail": "Not Found"}` with no
        `code` — and 404 is not an incidental error here: cross-organisation
        access deliberately answers 404 rather than 403, so it is a
        load-bearing part of the contract the frontend switches on.
        """
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, f"http_{exc.status_code}"
        )
        problem = ProblemError(
            code=code,
            status=exc.status_code,
            title=str(exc.detail),
            detail=str(exc.detail),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=problem.to_dict(request.url.path),
            media_type="application/problem+json",
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problem = ProblemError(
            code="validation_error",
            status=422,
            title="Invalid request",
            detail="The request body or parameters failed validation.",
        )
        body = problem.to_dict(request.url.path)
        body["errors"] = jsonable_encoder(exc.errors())
        return JSONResponse(status_code=422, content=body, media_type="application/problem+json")

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # FastAPI/Starlette install the bare-`Exception` handler on
        # ServerErrorMiddleware, which sits OUTSIDE RequestContextMiddleware
        # (above it in the stack, see Starlette's build_middleware_stack). An
        # unhandled exception therefore unwinds all the way past our
        # middleware's `finally` — which has already reset `request_id_var` to
        # its empty default — before this handler ever runs. `request.state`,
        # set once by the middleware from the same `scope` and never reset,
        # is the only value that reliably survives that unwind, so it — not
        # the contextvar — is the source of truth here for both the log line
        # and the response. The response also gets its own X-Request-Id
        # header: ServerErrorMiddleware sends it via the raw `send` it
        # received, bypassing the header our middleware injects into
        # everything else.
        request_id = getattr(request.state, "request_id", "") or request_id_var.get()
        # The detail is deliberately generic — the request_id is how the caller
        # points us at the log line that has the real cause.
        logger.exception("unhandled_exception", path=request.url.path, request_id=request_id)
        problem = ProblemError(
            code="internal_error",
            status=500,
            title="Internal error",
            detail="An unexpected error occurred. Quote the request_id when reporting it.",
        )
        body = problem.to_dict(request.url.path)
        body["request_id"] = request_id
        return JSONResponse(
            status_code=500,
            content=body,
            media_type="application/problem+json",
            headers={"X-Request-Id": request_id},
        )
