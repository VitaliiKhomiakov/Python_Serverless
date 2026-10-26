"""Startup-owned log handlers and safe conventional Uvicorn output."""

import logging
import sys
from typing import TextIO


class SafeStreamHandler(logging.StreamHandler[TextIO]):
    def handleError(self, record: logging.LogRecord) -> None:
        # The standard fallback exposes raw msg/args and exception text.
        try:
            sys.stderr.write(
                '{"level":"ERROR","logger":"serverless_lab.logging",'
                '"event":"logging.output_failed",'
                '"message":"Unable to write log record"}\n'
            )
            sys.stderr.flush()
        except Exception:
            # A broken emergency sink must not affect request processing.
            pass


class SafeServerFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = (
            _access_message(record.args)
            if record.name == "uvicorn.access"
            else record.getMessage().rstrip()
        )
        lines = [f"{record.levelname}:     {message}"]
        if record.exc_info is not None and record.exc_info[1] is not None:
            # Keep diagnostic locations, excluding source, locals and error text.
            traceback = record.exc_info[2]
            while traceback is not None:
                code = traceback.tb_frame.f_code
                lines.append(
                    f'  File "{code.co_filename}", line {traceback.tb_lineno}, '
                    f"in {code.co_name}"
                )
                traceback = traceback.tb_next
            lines.append(type(record.exc_info[1]).__name__)
        return "\n".join(lines)


def _access_message(args: object) -> str:
    # Uvicorn's tuple includes an untrusted client/target: never interpolate it.
    if not isinstance(args, tuple) or len(args) != 5:
        return "HTTP/? ?"
    version, status = args[3], args[4]
    safe_version = version if version in ("1.0", "1.1", "2", "3") else "?"
    safe_status = status if type(status) is int and 100 <= status <= 599 else "?"
    return f"HTTP/{safe_version} {safe_status}"


def configure_logging() -> None:
    """Configure owned names once at startup; leave root/other loggers alone."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "serverless_lab"):
        stream = sys.stderr if name in {"uvicorn", "uvicorn.error"} else sys.stdout
        handler = SafeStreamHandler(stream)
        handler.setFormatter(
            logging.Formatter("%(message)s")
            if name == "serverless_lab"
            else SafeServerFormatter()
        )
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
