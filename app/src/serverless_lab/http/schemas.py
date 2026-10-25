from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from serverless_lab.domain.analysis import (
    DEFAULT_TOP_K,
    MAX_TEXT_LENGTH,
    MAX_TOP_K,
    MIN_TEXT_LENGTH,
    MIN_TOP_K,
)
from serverless_lab.domain.word_count import (
    MAX_WORD_LIMIT,
    MIN_WORD_LIMIT,
    WordCountStatus,
)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[
        str, Field(strict=True, min_length=MIN_TEXT_LENGTH, max_length=MAX_TEXT_LENGTH)
    ]
    top_k: Annotated[int, Field(strict=True, ge=MIN_TOP_K, le=MAX_TOP_K)] = (
        DEFAULT_TOP_K
    )


class ReviewTextRequest(AnalyzeRequest):
    min_words: Annotated[int, Field(strict=True, ge=MIN_WORD_LIMIT, le=MAX_WORD_LIMIT)]
    max_words: Annotated[int, Field(strict=True, ge=MIN_WORD_LIMIT, le=MAX_WORD_LIMIT)]


class WordCountResponse(BaseModel):
    word: str
    count: int


class AnalyzeResponse(BaseModel):
    characters: int
    words: int
    top_words: list[WordCountResponse]


class WordCountCheckResponse(BaseModel):
    min_words: int
    max_words: int
    status: WordCountStatus


class ReviewTextResponse(BaseModel):
    analysis: AnalyzeResponse
    word_count_check: WordCountCheckResponse


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class InvalidRequestResponse(BaseModel):
    detail: Literal["Invalid request."] = "Invalid request."
