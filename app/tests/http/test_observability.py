import asyncio
import io
import json
import logging
import re
from collections.abc import Iterator
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from serverless_lab.app_factory import create_app
from serverless_lab.http.observability import ObservabilityMiddleware
from serverless_lab.runtime import RuntimeSettings


@pytest.fixture(autouse=True)
def request_log() -> Iterator[io.StringIO]:
    logger = logging.getLogger("serverless_lab.requests")
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    try:
        yield output
    finally:
        logger.handlers, logger.propagate = handlers, propagate
        logger.setLevel(level)
        handler.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    # No lifespan resources here; build middleware on the first captured request.
    test_client = TestClient(create_app())
    try:
        yield test_client
    finally:
        test_client.close()


class RequestEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event: Literal["request.completed"]
    timestamp: str
    level: str
    logger: str
    method: str
    request_id: str
    instance_id: str
    revision: str
    route: str
    duration_ms: float
    status: int | None
    outcome: Literal["completed", "failed", "cancelled"]


def request_events(request_log: io.StringIO) -> list[RequestEvent]:
    return [
        RequestEvent.model_validate_json(line)
        for line in request_log.getvalue().splitlines()
    ]


@pytest.mark.parametrize("request_id", ["safe-id_1.2", "a" * 64])
def test_valid_request_id_is_echoed_and_logged(
    client: TestClient, request_log: io.StringIO, request_id: str
) -> None:
    response = client.get("/health", headers={"X-Request-Id": request_id})
    assert response.headers["x-request-id"] == request_id
    [event] = request_events(request_log)
    assert event.request_id == request_id
    assert event.status == 200
    assert event.outcome == "completed"
    assert event.duration_ms >= 0


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("x-request-id", "")],
        [("x-request-id", "with spaces")],
        [("x-request-id", "x" * 65)],
        [("x-request-id", "one"), ("x-request-id", "two")],
    ],
)
def test_missing_invalid_or_duplicate_request_ids_are_replaced(
    client: TestClient,
    request_log: io.StringIO,
    headers: list[tuple[str, str]],
) -> None:
    response = client.get("/health", headers=headers)
    identifier = response.headers["x-request-id"]
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", identifier)
    assert identifier not in {value for _, value in headers}
    [event] = request_events(request_log)
    assert event.request_id == identifier


def test_observation_headers_are_available_by_default(
    request_log: io.StringIO,
) -> None:
    with TestClient(create_app()) as client:
        response = client.post("/analyze", json={"text": "a"})
    assert response.status_code == 200
    assert response.headers["x-instance-id"]
    assert response.headers["x-revision"]
    assert len(request_events(request_log)) == 1


@pytest.mark.parametrize("endpoint", ["/analyze", "/review-text"])
def test_observation_preserves_json_contract(endpoint: str) -> None:
    body: dict[str, str | int] = {"text": "a b a", "top_k": 1}
    if endpoint == "/review-text":
        body.update(min_words=2, max_words=3)
    with TestClient(create_app()) as client:
        response = client.post(endpoint, json=body)
    analysis = {
        "characters": 5,
        "words": 3,
        "top_words": [{"word": "a", "count": 2}],
    }
    expected: object = analysis
    if endpoint == "/review-text":
        expected = {
            "analysis": analysis,
            "word_count_check": {
                "min_words": 2,
                "max_words": 3,
                "status": "within_range",
            },
        }
    assert response.status_code == 200
    assert json.loads(response.content) == expected
    assert response.headers["x-instance-id"]
    assert response.headers["x-revision"]


def test_logs_exclude_request_data_and_untrusted_paths(
    client: TestClient, request_log: io.StringIO
) -> None:
    response = client.post(
        "/analyze?private-query",
        json={"text": "private-body", "top_k": "invalid"},
        headers={"authorization": "private-header", "x-request-id": "bad id"},
    )
    assert response.status_code == 422
    assert response.headers["x-request-id"]
    missing = client.get("/private-path?private-query")
    assert missing.status_code == 404
    output = request_log.getvalue()
    assert "private-" not in output
    assert "bad id" not in output
    first, second = [
        RequestEvent.model_validate_json(line) for line in output.splitlines()
    ]
    assert (first.route, first.status, first.outcome) == ("/analyze", 422, "completed")
    assert (second.route, second.status) == ("unmatched", 404)


def test_unexpected_exception_is_logged_safely_and_propagates(
    request_log: io.StringIO,
) -> None:
    inner = FastAPI()
    app = ObservabilityMiddleware(
        inner, RuntimeSettings("test-instance", "test-revision")
    )

    @inner.get("/explode")
    async def explode() -> None:
        raise RuntimeError("private-exception")

    with (
        TestClient(app) as client,
        pytest.raises(RuntimeError, match="private-exception"),
    ):
        client.get("/explode")
    [event] = request_events(request_log)
    assert (event.status, event.outcome, event.route) == (500, "failed", "unmatched")


def test_factories_do_not_change_global_logging_or_duplicate_events(
    request_log: io.StringIO,
) -> None:
    loggers = [
        logging.getLogger(name)
        for name in (
            "",
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "serverless_lab",
            "serverless_lab.requests",
        )
    ]
    original_configuration = [
        (logger.handlers[:], logger.filters[:], logger.level, logger.propagate)
        for logger in loggers
    ]
    for _ in range(2):
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
    assert [
        (logger.handlers, logger.filters, logger.level, logger.propagate)
        for logger in loggers
    ] == original_configuration
    assert len(request_events(request_log)) == 2


def request_scope(path: str, request_id: bytes = b"safe") -> Scope:
    return {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": [(b"x-request-id", request_id)],
    }


async def receive_unused() -> Message:
    raise AssertionError("This response-only ASGI fixture must not read the body")


async def respond(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def run_request(app: ASGIApp, scope: Scope) -> list[Message]:
    messages: list[Message] = []

    async def collect(message: Message) -> None:
        messages.append(message)

    await app(scope, receive_unused, collect)
    return messages


def test_overlapping_requests_preserve_ids_and_leave_health_responsive(
    request_log: io.StringIO,
) -> None:
    async def scenario() -> None:
        both_waiting = asyncio.Event()
        release = asyncio.Event()
        waiting = 0

        async def controlled_app(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal waiting
            if scope["path"] == "/health":
                await respond(scope, receive, send)
                return
            waiting += 1
            if waiting == 2:
                both_waiting.set()
            await release.wait()
            await respond(scope, receive, send)

        app = ObservabilityMiddleware(
            controlled_app, RuntimeSettings.from_environment({})
        )
        async with asyncio.timeout(2), asyncio.TaskGroup() as group:
            first = group.create_task(
                run_request(app, request_scope("/analyze", b"first"))
            )
            second = group.create_task(
                run_request(app, request_scope("/review-text", b"second"))
            )
            await both_waiting.wait()
            health = await run_request(app, request_scope("/health", b"probe"))
            assert Headers(raw=health[0]["headers"])["x-request-id"] == "probe"
            assert not first.done()
            assert not second.done()
            release.set()
        for task, expected in ((first, "first"), (second, "second")):
            headers = Headers(raw=task.result()[0]["headers"])
            assert headers["x-request-id"] == expected

    asyncio.run(scenario())
    events = request_events(request_log)
    assert {event.request_id for event in events} == {"first", "second", "probe"}
    assert len(events) == 3
    assert all(event.outcome == "completed" for event in events)


def test_cancellation_is_logged_and_propagated_without_a_response(
    request_log: io.StringIO,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        never_released = asyncio.Event()

        async def blocked_app(scope: Scope, receive: Receive, send: Send) -> None:
            entered.set()
            await never_released.wait()

        app = ObservabilityMiddleware(blocked_app, RuntimeSettings.from_environment({}))
        async with asyncio.timeout(2):
            task = asyncio.create_task(run_request(app, request_scope("/analyze")))
            try:
                await entered.wait()
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())
    [event] = request_events(request_log)
    assert (event.request_id, event.status, event.outcome) == (
        "safe",
        None,
        "cancelled",
    )


@pytest.mark.parametrize("identifier", [b"unsafe\xff", b"unsafe\r\nheader"])
def test_non_ascii_or_control_character_request_ids_are_replaced(
    request_log: io.StringIO, identifier: bytes
) -> None:
    app = ObservabilityMiddleware(respond, RuntimeSettings.from_environment({}))
    messages = asyncio.run(run_request(app, request_scope("/health", identifier)))
    request_id = Headers(raw=messages[0]["headers"])["x-request-id"]
    assert request_id.isascii()
    assert request_id.isalnum()
    [event] = request_events(request_log)
    assert event.request_id == request_id


def test_failure_after_response_start_preserves_observed_status(
    request_log: io.StringIO,
) -> None:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def fail_after_start(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("private-body-failure")

    middleware = ObservabilityMiddleware(
        fail_after_start, RuntimeSettings.from_environment({})
    )
    with pytest.raises(RuntimeError, match="private-body-failure"):
        asyncio.run(middleware(request_scope("/analyze"), receive_unused, send))
    [event] = request_events(request_log)
    assert sent[0]["status"] == 200
    assert (event.status, event.outcome) == (200, "failed")
