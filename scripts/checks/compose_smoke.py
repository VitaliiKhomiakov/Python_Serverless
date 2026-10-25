"""Check an existing image in a disposable Compose project, then remove that project."""

import argparse
import http.client
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = "Привет, мир! Привет."
ANALYSIS = {
    "characters": 20,
    "words": 3,
    "top_words": [{"word": "привет", "count": 2}, {"word": "мир", "count": 1}],
}


class Options(argparse.Namespace):
    image: str
    container_port: int


def run(
    *command: str,
    timeout: int = 60,
    env: dict[str, str] | None = None,
    combined: bool = False,
) -> str:
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if combined else subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.stdout.strip()


def inspect(container: str, template: str) -> str:
    return run("docker", "inspect", "--format", template, container)


def request(port: int, path: str, body: object | None = None) -> tuple[int, object]:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = None if body is None else json.dumps(body).encode()
        client.request(
            "GET" if body is None else "POST",
            path,
            payload,
            {"Content-Type": "application/json"},
        )
        response = client.getresponse()
        decoded: object = json.loads(response.read())
        return response.status, decoded
    finally:
        client.close()


def check_http(port: int) -> None:
    assert request(port, "/health") == (200, {"status": "ok"})
    assert request(port, "/analyze", {"text": SAMPLE}) == (200, ANALYSIS)
    assert request(
        port, "/review-text", {"text": SAMPLE, "min_words": 4, "max_words": 8}
    ) == (
        200,
        {
            "analysis": ANALYSIS,
            "word_count_check": {"min_words": 4, "max_words": 8, "status": "too_short"},
        },
    )
    assert request(port, "/analyze", {"text": "PRIVATE_CHECK", "top_k": True}) == (
        422,
        {"detail": "Invalid request."},
    )


def check_runtime(container: str, expected_port: int) -> None:
    properties = {
        "{{.Config.User}}": "10001:10001",
        "{{.HostConfig.ReadonlyRootfs}}": "true",
        "{{.HostConfig.CapDrop}}": "[ALL]",
        "{{.HostConfig.SecurityOpt}}": "[no-new-privileges:true]",
        "{{.HostConfig.Memory}}": str(256 * 1024 * 1024),
        "{{.HostConfig.NanoCpus}}": "1000000000",
        "{{.HostConfig.PidsLimit}}": "64",
        "{{range .Mounts}}{{.Type}}:{{.Destination}}{{end}}": "",
        '{{index .HostConfig.Tmpfs "/tmp"}}': "rw,noexec,nosuid,size=16m,mode=1777",
    }
    for template, expected in properties.items():
        assert inspect(container, template) == expected, template
    run(
        "docker",
        "exec",
        container,
        "python",
        "-c",
        "\n".join(
            [
                "import importlib.util, os, pathlib, shutil",
                "import serverless_lab.http.presenters as presenters",
                "assert os.getuid() == os.getgid() == 10001",
                f"assert os.environ['PORT'] == '{expected_port}'",
                "assert '/opt/venv/' in presenters.__file__",
                "assert not pathlib.Path('/build').exists()",
                "assert not pathlib.Path('/app/src').exists()",
                "assert ' /tmp tmpfs ' in pathlib.Path('/proc/mounts').read_text()",
                "assert all(importlib.util.find_spec(name) is None for name in "
                "('pytest', 'ruff', 'mypy', 'uv_build', 'httpx2'))",
                "assert shutil.which('uv') is None",
                "assert shutil.which('pip') is None",
                "assert not "
                "pathlib.Path('/usr/local/lib/python3.14/ensurepip').exists()",
                "p = pathlib.Path('/tmp/write-check'); p.write_text('ok'); p.unlink()",
            ]
        ),
    )


def check_shutdown(container: str, port: int) -> None:
    body = json.dumps({"text": SAMPLE}).encode()
    headers = (
        "POST /analyze HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Type: application/json\r\nExpect: 100-continue\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(headers)
        interim = b""
        while b"\r\n\r\n" not in interim:
            chunk = connection.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed before accepting the request")
            interim += chunk
        assert interim.startswith(b"HTTP/1.1 100 Continue"), interim
        # The body read is active; stop must drain this request before process exit.
        with subprocess.Popen(
            ["docker", "stop", "--time", "20", container],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as stop:
            try:
                deadline = time.monotonic() + 5
                while "Waiting for connections to close" not in run(
                    "docker", "logs", container, timeout=3, combined=True
                ):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Server did not enter graceful draining")
                    time.sleep(0.1)
                connection.sendall(body)
                response = http.client.HTTPResponse(connection)
                response.begin()
                decoded: object = json.loads(response.read())
                assert (response.status, decoded) == (200, ANALYSIS)
                _, error = stop.communicate(timeout=25)
                assert stop.returncode == 0, error
            except BaseException:
                stop.kill()
                stop.communicate(timeout=5)
                raise
    assert inspect(container, "{{.State.OOMKilled}}") == "false"
    assert inspect(container, "{{.State.ExitCode}}") in {"0", "143"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python-serverless-lab:0.1.0")
    parser.add_argument("--container-port", type=int, default=8080)
    arguments = parser.parse_args(namespace=Options())
    if not 1 <= arguments.container_port <= 65535:
        parser.error("--container-port must be between 1 and 65535")
    project = "serverless-lab-check-" + uuid4().hex[:12]
    environment = dict(os.environ)
    environment.update(
        APP_IMAGE=arguments.image,
        APP_PORT=str(arguments.container_port),
        APP_HOST_PORT="0",
    )
    compose = ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "-p", project]
    assert not run(
        "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"
    )
    print(f"Checking disposable Compose project: {project}", flush=True)
    try:
        run(
            *compose,
            "up",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "30",
            env=environment,
        )
        container = run(*compose, "ps", "-q", "app", env=environment)
        address = run(
            *compose, "port", "app", str(arguments.container_port), env=environment
        )
        host, _, port = address.rpartition(":")
        assert host == "127.0.0.1", address
        check_http(int(port))
        check_runtime(container, arguments.container_port)
        image_id = inspect(container, "{{.Image}}")
        check_shutdown(container, int(port))
        logs = run("docker", "logs", container, combined=True)
        assert "Application shutdown complete" in logs
        assert "PRIVATE_CHECK" not in logs and SAMPLE not in logs
        print(f"PASS: HTTP, runtime isolation and in-flight shutdown; image {image_id}")
    finally:
        run(*compose, "down", "--timeout", "20", env=environment)


if __name__ == "__main__":
    main()
