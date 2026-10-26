"""Installed entry point for the configured Uvicorn process."""

import argparse
import os
from collections.abc import Sequence

import uvicorn

from serverless_lab.app_factory import create_app
from serverless_lab.logging_config import configure_logging
from serverless_lab.runtime import RuntimeSettings


class ServerArguments(argparse.Namespace):
    host: str
    port: int


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "port must be an integer from 1 to 65535"
        ) from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the text-analysis service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=os.environ.get("PORT", "8000"))
    arguments = parser.parse_args(argv, namespace=ServerArguments())
    settings = RuntimeSettings.from_environment(os.environ)
    configure_logging()
    uvicorn.run(
        create_app(settings),
        host=arguments.host,
        port=arguments.port,
        log_config=None,
        access_log=True,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
