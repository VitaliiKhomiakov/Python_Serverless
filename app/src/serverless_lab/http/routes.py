from fastapi import APIRouter

from serverless_lab.application.text_operations import analyze_text, review_text
from serverless_lab.domain.word_count import WordCountPolicy
from serverless_lab.http.presenters import to_analyze_response, to_review_text_response
from serverless_lab.http.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    HealthResponse,
    InvalidRequestResponse,
    ReviewTextRequest,
    ReviewTextResponse,
)

router = APIRouter()


@router.post("/analyze", responses={422: {"model": InvalidRequestResponse}})
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    result = analyze_text(request.text, request.top_k)
    return to_analyze_response(result)


@router.post("/review-text", responses={422: {"model": InvalidRequestResponse}})
def review(request: ReviewTextRequest) -> ReviewTextResponse:
    policy = WordCountPolicy(request.min_words, request.max_words)
    result = review_text(request.text, policy, request.top_k)
    return to_review_text_response(result)


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse()
