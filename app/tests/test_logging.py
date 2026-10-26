"""Safe conventional server output, completion lifetime and logger ownership."""

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from starlette.types import Message, Receive, Scope, Send

from serverless_lab.http.observability import ObservabilityMiddleware
from serverless_lab.logging_config import (
    SafeServerFormatter,
    configure_logging,
)
from serverless_lab.runtime import RuntimeSettings

SETTINGS = RuntimeSettings("instance-one", "revision-one")


@contextmanager
def log_output() -> Iterator[io.StringIO]:
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


def scope(identifier: bytes = b"request-one") -> Scope:
    return {
        "type": "http",
        "path": "/health",
        "method": "GET",
        "headers": [(b"x-request-id", identifier)],
    }


async def receive() -> Message:
    raise AssertionError("Response fixture does not read requests")


async def discard(message: Message) -> None:
    pass


async def respond(request: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 201, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def record(
    logger: str, message: str, args: tuple[object, ...] = ()
) -> logging.LogRecord:
    return logging.LogRecord(logger, logging.INFO, __file__, 1, message, args, None)


def test_access_never_interpolates_private_url_or_fallback_message() -> None:
    for args, expected in [
        (("client", "GET", "/private-path?private-query", "1.1", 200), "HTTP/1.1 200"),
        (("private-fallback",), "HTTP/? ?"),
        (("client", "GET", "/private", "1.1", True), "HTTP/1.1 ?"),
    ]:
        entry = record("uvicorn.access", "private-message %s", args)
        output = SafeServerFormatter().format(entry)
        assert "private" not in output
        assert output == f"INFO:     {expected}"


def test_lifecycle_remains_readable_standard_output() -> None:
    entry = record("uvicorn.error", "Started server process [%d]", (123,))
    assert (
        SafeServerFormatter().format(entry) == "INFO:     Started server process [123]"
    )


def test_original_exception_is_unchanged_and_diagnostics_are_private() -> None:
    failure = RuntimeError("private-exception")
    failure.add_note("private-note")
    attributes = failure.__dict__.copy()

    async def fail(request: Scope, receive: Receive, send: Send) -> None:
        raise failure

    with log_output() as output, pytest.raises(RuntimeError) as caught:
        asyncio.run(ObservabilityMiddleware(fail, SETTINGS)(scope(), receive, discard))
    assert caught.value is failure
    assert failure.__dict__ == attributes
    completion = json.loads(output.getvalue())
    assert (completion["status"], completion["outcome"]) == (None, "failed")
    entry = record("uvicorn.error", "Exception in ASGI application")
    entry.exc_info = (type(failure), failure, failure.__traceback__)
    diagnostic = SafeServerFormatter().format(entry)
    assert "private-" not in diagnostic
    assert "RuntimeError" in diagnostic
    assert "in fail" in diagnostic
    assert "request-one" not in diagnostic


@pytest.mark.parametrize(
    "failure", [None, RuntimeError("failure"), asyncio.CancelledError()]
)
def test_logging_failure_preserves_original_error_or_cancellation(
    failure: BaseException | None,
) -> None:
    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise OSError("logging output failed")

    async def app(request: Scope, receive: Receive, send: Send) -> None:
        if failure is not None:
            raise failure
        await respond(request, receive, send)

    async def scenario() -> None:
        with pytest.raises(OSError if failure is None else type(failure)) as caught:
            await ObservabilityMiddleware(app, SETTINGS)(scope(), receive, discard)
        if failure is not None:
            assert caught.value is failure

    with log_output():
        logging.getLogger("serverless_lab.requests").handlers = [FailingHandler()]
        asyncio.run(scenario())


def test_incomplete_response_is_failed_and_method_is_bounded() -> None:
    async def incomplete(request: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 202, "headers": []})

    request = scope()
    request["method"] = "private-method" * 50
    with log_output() as output:
        asyncio.run(
            ObservabilityMiddleware(incomplete, SETTINGS)(request, receive, discard)
        )
    event = json.loads(output.getvalue())
    assert (event["status"], event["outcome"], event["method"]) == (
        202,
        "failed",
        "OTHER",
    )


def test_logging_setup_repeatable_root_and_unrelated_logger_untouched() -> None:
    loggers = [
        logging.getLogger(name)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "serverless_lab")
    ]
    originals = [(item.handlers[:], item.level, item.propagate) for item in loggers]
    root, unrelated = logging.getLogger(), logging.getLogger("unrelated.library")
    root_before = (root.handlers[:], root.level, root.propagate)
    other_before = (unrelated.handlers[:], unrelated.level, unrelated.propagate)
    try:
        configure_logging()
        configure_logging()
        for logger in loggers:
            assert len(logger.handlers) == 1
            assert not logger.propagate
        assert (root.handlers, root.level, root.propagate) == root_before
        assert (
            unrelated.handlers,
            unrelated.level,
            unrelated.propagate,
        ) == other_before
    finally:
        for logger, (handlers, level, propagate) in zip(
            loggers, originals, strict=True
        ):
            for handler in logger.handlers:
                handler.close()
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)


def test_completion_waits_for_awaited_work_after_final_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(
        "serverless_lab.http.observability.perf_counter", lambda: next(ticks)
    )

    async def scenario(output: io.StringIO) -> None:
        final_body_sent = asyncio.Event()
        release_work = asyncio.Event()

        async def app(request: Scope, receive: Receive, send: Send) -> None:
            await respond(request, receive, send)
            final_body_sent.set()
            await release_work.wait()

        middleware = ObservabilityMiddleware(app, SETTINGS)
        async with asyncio.timeout(2):
            task = asyncio.create_task(middleware(scope(), receive, discard))
            try:
                await final_body_sent.wait()
                assert not output.getvalue()
            finally:
                release_work.set()
                await task

    with log_output() as output:
        asyncio.run(scenario(output))
    event = json.loads(output.getvalue())
    assert (event["duration_ms"], event["outcome"]) == (2000.0, "completed")


def test_server_errors_use_stderr_and_access_uses_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "serverless_lab")
    loggers = [logging.getLogger(name) for name in names]
    originals = [
        (logger.handlers[:], logger.level, logger.propagate) for logger in loggers
    ]
    try:
        configure_logging()
        logging.getLogger("uvicorn.error").info("Application startup complete.")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "client",
            "GET",
            "/private",
            "1.1",
            200,
        )
        assert stdout.getvalue() == "INFO:     HTTP/1.1 200\n"
        assert stderr.getvalue() == "INFO:     Application startup complete.\n"
    finally:
        for logger, (handlers, level, propagate) in zip(
            loggers, originals, strict=True
        ):
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)


@pytest.mark.parametrize("failure", ["write", "flush"])
@pytest.mark.parametrize("fallback_broken", [False, True])
def test_owned_handler_output_failure_never_exposes_raw_access_record(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    fallback_broken: bool,
) -> None:
    class BrokenOutput(io.StringIO):
        def write(self, value: str) -> int:
            if failure == "write":
                raise OSError("PRIVATE_WRITE_FAILURE")
            return super().write(value)

        def flush(self) -> None:
            if failure == "flush":
                raise OSError("PRIVATE_FLUSH_FAILURE")
            super().flush()

    stdout = BrokenOutput()
    stderr = BrokenOutput() if fallback_broken else io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "serverless_lab")
    loggers = [logging.getLogger(name) for name in names]
    originals = [
        (logger.handlers[:], logger.level, logger.propagate) for logger in loggers
    ]
    raise_exceptions = logging.raiseExceptions
    root = logging.getLogger()
    root_before = (root.handlers[:], root.level, root.propagate)
    try:
        configure_logging()
        logging.getLogger("uvicorn.access").info(
            "PRIVATE_RAW_MESSAGE %s %s %s %s %d",
            "PRIVATE_CLIENT",
            "GET",
            "/private?PRIVATE_QUERY_SENTINEL",
            "1.1",
            200,
        )
        output = stdout.getvalue() + stderr.getvalue()
        assert "PRIVATE_" not in output
        if not fallback_broken:
            event = json.loads(stderr.getvalue())
            assert event["event"] == "logging.output_failed"
        assert logging.raiseExceptions is raise_exceptions
        assert (root.handlers, root.level, root.propagate) == root_before
    finally:
        for logger, (handlers, level, propagate) in zip(
            loggers, originals, strict=True
        ):
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)
