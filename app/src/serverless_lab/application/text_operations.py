from dataclasses import dataclass

from serverless_lab.domain.analysis import DEFAULT_TOP_K, AnalysisResult
from serverless_lab.domain.analysis import analyze_text as analyze_domain_text
from serverless_lab.domain.word_count import (
    WordCountPolicy,
    WordCountStatus,
    assess_word_count,
)


@dataclass(frozen=True)
class TextReviewResult:
    analysis: AnalysisResult
    policy: WordCountPolicy
    status: WordCountStatus


def analyze_text(text: str, top_k: int = DEFAULT_TOP_K) -> AnalysisResult:
    return analyze_domain_text(text, top_k)


def review_text(
    text: str, policy: WordCountPolicy, top_k: int = DEFAULT_TOP_K
) -> TextReviewResult:
    analysis = analyze_text(text, top_k)
    status = assess_word_count(analysis.words, policy)
    return TextReviewResult(analysis=analysis, policy=policy, status=status)
