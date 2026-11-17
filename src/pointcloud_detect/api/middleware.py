"""Pure-ASGI middleware for the API: a request-body cap and ``Cache-Control``.

Both are raw ASGI rather than Starlette's ``BaseHTTPMiddleware``, which buffers and
re-wraps the request stream: the body cap must see each chunk as it arrives so it can
stop before the whole body is in memory.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyTooLarge(HTTPException):
    """Raised from ``receive`` once a streamed body passes the cap.

    An ``HTTPException`` on purpose. FastAPI turns any other exception raised while it
    reads a body into a 400 ("There was an error parsing the body") but re-raises this
    one, so the route answers 413. If nothing catches it on the way out, the
    middleware below answers 413 itself.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413, detail=f"Request body exceeds the {limit}-byte limit."
        )


class BodySizeLimitMiddleware:
    """Refuse request bodies larger than ``max_body_bytes`` with 413 (T44).

    Two checks, because a client can send a body either way:

    - a declared ``Content-Length`` over the cap is refused before the app runs, so
      nothing reads or parses a byte of it;
    - a body without one (chunked), or one that sends more than it declared, is
      counted as it streams, and the request is aborted as soon as the count passes
      the cap.

    Uploads do not pass through here: the browser sends file parts straight to S3 on
    presigned URLs, so every legitimate body is JSON of a few hundred KiB at most.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive number of bytes")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length < 0:
                await _reply(scope, receive, send, 400, "Invalid Content-Length.")
                return
            if length > self.max_body_bytes:
                await _reply(
                    scope,
                    receive,
                    send,
                    413,
                    f"Request body exceeds the {self.max_body_bytes}-byte limit.",
                )
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestBodyTooLarge(self.max_body_bytes)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge as exc:
            # Reached only when nothing inside turned it into a response. Once a
            # response has started there is nothing left to say; the request is
            # abandoned either way.
            if not response_started:
                await _reply(scope, receive, send, 413, str(exc.detail))


async def _reply(
    scope: Scope, receive: Receive, send: Send, status: int, detail: str
) -> None:
    response = JSONResponse({"detail": detail}, status_code=status)
    await response(scope, receive, send)


class NoStoreMiddleware:
    """Mark every ``/api`` response ``Cache-Control: no-store`` (M35).

    Responses carry scan-derived data. In public mode the ALB forwards ``/api``
    straight to the api pods, so nginx never sees these responses and cannot add the
    header there. A route that sets its own ``Cache-Control`` keeps it.
    """

    PREFIX = "/api"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == self.PREFIX or path.startswith(self.PREFIX + "/")):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                # ASGI allows a start message without headers.
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_no_store)
