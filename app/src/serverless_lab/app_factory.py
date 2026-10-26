import os

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp

from serverless_lab.domain.analysis import AnalysisInputError
from serverless_lab.domain.word_count import WordCountInputError
from serverless_lab.http.errors import http_error, invalid_request
from serverless_lab.http.observability import ObservabilityMiddleware
from serverless_lab.http.routes import router
from serverless_lab.runtime import RuntimeSettings


def create_app(settings: RuntimeSettings | None = None) -> ASGIApp:
    if settings is None:
        settings = RuntimeSettings.from_environment(os.environ)
    app = FastAPI(title="Serverless Text Analysis", version="0.1.0")
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(AnalysisInputError, invalid_request)
    app.add_exception_handler(WordCountInputError, invalid_request)
    app.add_exception_handler(HTTPException, http_error)
    app.include_router(router)
    return ObservabilityMiddleware(app, settings)
