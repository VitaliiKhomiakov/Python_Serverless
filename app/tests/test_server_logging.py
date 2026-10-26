"""Real pinned Uvicorn, its error timing, and the installed module launcher."""

import asyncio
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse, StreamingResponse

from serverless_lab.http.observability import ObservabilityMiddleware
from serverless_lab.http.routes import router
from serverless_lab.logging_config import configure_logging
from serverless_lab.runtime import RuntimeSettings


def serve_fixture() -> None:
    """Process-local fixture preserves the real Starlette error middleware."""
    app = FastAPI()
    app.include_router(router)

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("PRIVATE_EXCEPTION_MARKER")

    @app.get("/after")
    async def after() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            yield b"first"
            await asyncio.sleep(0)
            raise RuntimeError("PRIVATE_STREAM_MARKER")

        return StreamingResponse(stream())

    @app.get("/background")
    async def background() -> PlainTextResponse:
        async def fail() -> None:
            raise RuntimeError("PRIVATE_BACKGROUND_MARKER")

        return PlainTextResponse("ok", background=BackgroundTask(fail))

    settings = RuntimeSettings.from_environment(os.environ)
    configure_logging()
    uvicorn.run(
        ObservabilityMiddleware(app, settings),
        host="127.0.0.1",
        port=int(os.environ["PORT"]),
        log_config=None,
        access_log=True,
        timeout_graceful_shutdown=2,
    )


@dataclass(frozen=True)
class Server:
    port: int
    log: Path

    def request(
        self,
        method: str,
        path: str,
        identifier: str,
        body: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(
                method,
                path,
                body,
                headers={
                    "X-Request-Id": identifier,
                    "Content-Type": "application/json",
                    "Authorization": "PRIVATE_HEADER_MARKER",
                },
            )
            response = connection.getresponse()
            status, headers = response.status, dict(response.getheaders())
            try:
                data = response.read()
            except http.client.IncompleteRead as error:
                data = error.partial
            return status, {key.lower(): value for key, value in headers.items()}, data
        finally:
            connection.close()


@contextmanager
def running_server(tmp_path: Path, *, launcher: bool = False) -> Iterator[Server]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = dict(os.environ, PORT=str(port), K_REVISION="revision-process")
    environment.pop("HOSTNAME", None)
    command = (
        [sys.executable, "-m", "serverless_lab"]
        if launcher
        else [
            sys.executable,
            "-c",
            "from test_server_logging import serve_fixture; serve_fixture()",
        ]
    )
    environment["PYTHONPATH"] = str(Path(__file__).parent)
    log = tmp_path / ("launcher.log" if launcher else "fixture-server.log")
    with log.open("w") as output:
        process = subprocess.Popen(
            command, env=environment, stdout=output, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + 8
            while True:
                if process.poll() is not None:
                    pytest.fail(f"Server exited before startup: {log.read_text()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail(f"Server startup timed out: {log.read_text()}")
                    time.sleep(0.02)
            yield Server(port, log)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                pytest.fail("Server required forced shutdown")
    assert process.returncode in {0, -signal.SIGTERM}, log.read_text()


def events(log: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for line in log.read_text().splitlines():
        if not line.startswith("{"):
            continue
        value: object = json.loads(line)
        assert isinstance(value, dict)
        result.append(value)
    return result


def correlated(
    records: list[dict[str, object]], identifier: str
) -> list[dict[str, object]]:
    return [record for record in records if record["request_id"] == identifier]


def test_real_uvicorn_preserves_access_errors_completion_and_private_data_boundary(
    tmp_path: Path,
) -> None:
    with running_server(tmp_path) as server:
        for method, path, identifier, body, expected in [
            ("GET", "/health?PRIVATE_QUERY_MARKER", "req-healthy", None, 200),
            (
                "POST",
                "/analyze?PRIVATE_QUERY_MARKER",
                "req-invalid",
                '{"text":"PRIVATE_BODY_MARKER","top_k":"req-invalid"}',
                422,
            ),
            (
                "GET",
                "/PRIVATE_PATH_MARKER?PRIVATE_QUERY_MARKER",
                "req-missing",
                None,
                404,
            ),
            ("GET", "/explode?PRIVATE_QUERY_MARKER", "req-failed", None, 500),
            ("GET", "/after", "req-after-start", None, 200),
            ("GET", "/background", "req-background", None, 200),
        ]:
            status, headers, _ = server.request(method, path, identifier, body)
            assert status == expected
            assert headers["x-request-id"] == identifier
            assert headers["x-revision"] == "revision-process"
            assert headers["x-instance-id"]
    output = server.log.read_text()
    assert "PRIVATE_" not in output
    records = events(server.log)
    for identifier, status in (
        ("req-healthy", 200),
        ("req-invalid", 422),
        ("req-missing", 404),
        ("req-failed", 500),
        ("req-after-start", 200),
        ("req-background", 200),
    ):
        group = correlated(records, identifier)
        [completion] = group
        assert completion["event"] == "request.completed"
        assert completion["logger"] == "serverless_lab.requests"
        assert completion["status"] == status
        assert completion["revision"] == "revision-process"
        assert isinstance(completion["duration_ms"], (int, float))
        expected_outcome = (
            "failed"
            if identifier in {"req-failed", "req-after-start", "req-background"}
            else "completed"
        )
        assert completion["outcome"] == expected_outcome
    server_lines = [line for line in output.splitlines() if not line.startswith("{")]
    assert sum("HTTP/1.1 200" in line for line in server_lines) == 3
    assert all(
        any(f"HTTP/1.1 {status}" in line for line in server_lines)
        for status in (422, 404, 500)
    )
    assert sum("Exception in ASGI application" in line for line in server_lines) == 3
    assert sum(line == "RuntimeError" for line in server_lines) == 3
    assert any("Started server process" in line for line in server_lines)
    assert any("Finished server process" in line for line in server_lines)
    assert not any(
        identifier in line
        for identifier in (
            "req-healthy",
            "req-invalid",
            "req-missing",
            "req-failed",
            "req-after-start",
            "req-background",
        )
        for line in server_lines
    )


def test_actual_launcher_configures_once_and_captures_process_settings_once(
    tmp_path: Path,
) -> None:
    with running_server(tmp_path, launcher=True) as server:
        first = server.request("GET", "/health", "first")[1]
        second = server.request("GET", "/health", "second")[1]
        assert first["x-instance-id"] == second["x-instance-id"]
        assert first["x-instance-id"].startswith("instance-")
    records = events(server.log)
    for identifier in ("first", "second"):
        group = correlated(records, identifier)
        assert len(group) == 1
        assert group[0]["event"] == "request.completed"
        assert all(item["instance_id"] == first["x-instance-id"] for item in group)


@pytest.mark.parametrize("value", ["0", "65536", "bad"])
@pytest.mark.parametrize("source", ["cli", "environment"])
def test_actual_launcher_rejects_invalid_ports_before_startup(
    value: str, source: str
) -> None:
    environment = dict(os.environ, PORT=value if source == "environment" else "8000")
    command = [sys.executable, "-m", "serverless_lab"]
    if source == "cli":
        command.extend(["--port", value])
    result = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 2
    assert "port must be an integer from 1 to 65535" in result.stderr
    assert not result.stdout
