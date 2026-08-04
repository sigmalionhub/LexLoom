import time
import uuid
from collections.abc import Awaitable, Callable

from app.logging import request_id_var
from app.observability import _STREAM_SUFFIX, record_response_start, record_stream_duration

Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


class RequestContextMiddleware:
    """Raw ASGI middleware, deliberately not BaseHTTPMiddleware.

    BaseHTTPMiddleware wraps the receive channel, which breaks client-disconnect
    propagation into streaming responses. Since the whole point of this service
    is SSE with working cancellation, request context must not be added that way.

    This is also why the timer below has to stop at `http.response.start`
    rather than after `await self.app(...)` returns: raw ASGI is what makes
    streaming work, and streaming is exactly the case where that return
    happens 12 to 45 seconds late. See `app/observability.py`.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        measured_as_stream = scope["path"].endswith(_STREAM_SUFFIX)
        started_responding = False

        async def send_with_header(message: dict) -> None:
            nonlocal started_responding
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode()))
                # Here, not after `await self.app(...)`. This middleware is raw
                # ASGI precisely so that streaming works, which means the call
                # below returns when the SSE generator is exhausted — 12 to 45
                # seconds for a real provider. A histogram fed from there
                # reports how long a visitor watched a stream and calls it
                # latency.
                started_responding = True
                if not measured_as_stream:
                    record_response_start(
                        scope["path"], message["status"], (time.perf_counter() - started) * 1000
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            # `started_responding` guards the case where no response was ever
            # begun: a client that walked away before headers, or an ASGI-level
            # crash. Recording those as fast streams would be an invention; an
            # application error is a 500 the app itself sends, so it does
            # reach the counter above with the status it deserves.
            if measured_as_stream and started_responding:
                record_stream_duration((time.perf_counter() - started) * 1000)
            request_id_var.reset(token)
