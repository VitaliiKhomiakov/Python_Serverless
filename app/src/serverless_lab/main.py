from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from serverless_lab.domain.analysis import AnalysisInputError
from serverless_lab.domain.word_count import WordCountInputError
from serverless_lab.http.errors import http_error, invalid_request
from serverless_lab.http.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Serverless Text Analysis", version="0.1.0")
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(AnalysisInputError, invalid_request)
    app.add_exception_handler(WordCountInputError, invalid_request)
    app.add_exception_handler(HTTPException, http_error)
    app.include_router(router)
    return app
