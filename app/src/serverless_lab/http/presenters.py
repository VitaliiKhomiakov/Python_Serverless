from serverless_lab.application.text_operations import TextReviewResult
from serverless_lab.domain.analysis import AnalysisResult
from serverless_lab.http.schemas import (
    AnalyzeResponse,
    ReviewTextResponse,
    WordCountCheckResponse,
    WordCountResponse,
)


def to_analyze_response(result: AnalysisResult) -> AnalyzeResponse:
    return AnalyzeResponse(
        characters=result.characters,
        words=result.words,
        top_words=[
            WordCountResponse(word=item.word, count=item.count)
            for item in result.top_words
        ],
    )


def to_review_text_response(result: TextReviewResult) -> ReviewTextResponse:
    return ReviewTextResponse(
        analysis=to_analyze_response(result.analysis),
        word_count_check=WordCountCheckResponse(
            min_words=result.policy.min_words,
            max_words=result.policy.max_words,
            status=result.status,
        ),
    )
