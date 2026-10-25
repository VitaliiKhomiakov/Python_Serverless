from fastapi import Request, Response
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException

from serverless_lab.http.schemas import InvalidRequestResponse


async def invalid_request(_request: Request, _exception: Exception) -> Response:
    return Response(
        content=InvalidRequestResponse().model_dump_json(),
        status_code=422,
        media_type="application/json",
    )


async def http_error(request: Request, exception: Exception) -> Response:
    if not isinstance(exception, HTTPException):
        raise exception
    # FastAPI wraps JSON encoding failures in HTTP 400, retaining the decoding cause.
    if exception.status_code == 400 and isinstance(
        exception.__cause__, UnicodeDecodeError
    ):
        return await invalid_request(request, exception)
    return await http_exception_handler(request, exception)
