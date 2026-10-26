"""Request-scoped correlation, timing and structured HTTP logs."""

import json
import logging
import re
from asyncio import CancelledError
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal, TypedDict
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from serverless_lab.runtime import RuntimeSettings

LOGGED_ROUTES = frozenset(
    {"/analyze", "/review-text", "/health", "/openapi.json", "/docs", "/redoc"}
)
LOGGED_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
)


class RequestCompletion(TypedDict):
    event: Literal["request.completed"]
    timestamp: str
    level: str
    logger: str
    request_id: str
    instance_id: str
    revision: str
    route: str
    method: str
    duration_ms: float
    status: int | None
    outcome: Literal["completed", "failed", "cancelled"]


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, settings: RuntimeSettings) -> None:
        self.app = app
        self.settings = settings
        self.logger = logging.getLogger("serverless_lab.requests")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path, method = scope["path"], scope["method"]
        request_id = _request_id(Headers(scope=scope))
        started = perf_counter()
        status: int | None = None
        response_complete = False
        outcome: Literal["completed", "failed", "cancelled"] = "failed"
        escaping: BaseException | None = None

        async def send_observed(message: Message) -> None:
            nonlocal status, response_complete
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-Id"] = request_id
                headers["X-Instance-Id"] = self.settings.instance_id
                headers["X-Revision"] = self.settings.revision
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete = True

        try:
            await self.app(scope, receive, send_observed)
            if response_complete:
                outcome = "completed"
        except BaseException as error:
            escaping = error
            outcome = "cancelled" if isinstance(error, CancelledError) else "failed"
            raise
        finally:
            try:
                completion: RequestCompletion = {
                    "event": "request.completed",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": "INFO",
                    "logger": self.logger.name,
                    "request_id": request_id,
                    "instance_id": self.settings.instance_id,
                    "revision": self.settings.revision,
                    "route": path if path in LOGGED_ROUTES else "unmatched",
                    "method": method if method in LOGGED_METHODS else "OTHER",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "status": status,
                    "outcome": outcome,
                }
                self.logger.info(json.dumps(completion, separators=(",", ":")))
            except Exception:
                # Output failure must not replace an application error/cancellation.
                if escaping is None:
                    raise


def _request_id(headers: Headers) -> str:
    values = headers.getlist("x-request-id")
    if len(values) == 1 and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", values[0]):
        return values[0]
    return uuid4().hex
