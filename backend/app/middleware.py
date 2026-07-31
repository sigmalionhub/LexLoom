import uuid
from collections.abc import Awaitable, Callable

from app.logging import request_id_var

Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


class RequestContextMiddleware:
    """Raw ASGI middleware, deliberately not BaseHTTPMiddleware.

    BaseHTTPMiddleware wraps the receive channel, which breaks client-disconnect
    propagation into streaming responses. Since the whole point of this service
    is SSE with working cancellation, request context must not be added that way.
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

        async def send_with_header(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
