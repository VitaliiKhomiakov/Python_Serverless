import pytest

from serverless_lab.application.text_operations import TextReviewResult, review_text
from serverless_lab.domain.analysis import AnalysisInputError, AnalysisResult, WordCount
from serverless_lab.domain.word_count import WordCountPolicy, WordCountStatus


def test_review_returns_analysis_policy_and_assessment() -> None:
    assert review_text("Привет, мир! Привет.", WordCountPolicy(4, 8)) == (
        TextReviewResult(
            AnalysisResult(20, 3, (WordCount("привет", 2), WordCount("мир", 1))),
            WordCountPolicy(4, 8),
            WordCountStatus.TOO_SHORT,
        )
    )


def test_review_reuses_unicode_analysis_without_changing_original_length() -> None:
    result = review_text("e\u0301 É Straße STRASSE", WordCountPolicy(4, 4))
    assert result.analysis == AnalysisResult(
        19, 4, (WordCount("strasse", 2), WordCount("é", 2))
    )
    assert result.status == WordCountStatus.WITHIN_RANGE


def test_review_assesses_total_independently_of_top_word_truncation() -> None:
    result = review_text("b a b a c", WordCountPolicy(1, 4), top_k=1)
    assert result.analysis == AnalysisResult(9, 5, (WordCount("a", 2),))
    assert result.status == WordCountStatus.TOO_LONG


def test_review_accepts_valid_text_without_words() -> None:
    result = review_text("123!?", WordCountPolicy(1, 1))
    assert result.analysis == AnalysisResult(5, 0, ())
    assert result.status == WordCountStatus.TOO_SHORT


@pytest.mark.parametrize(("text", "top_k"), [(" \t", 5), ("a", True), ("a", 11)])
def test_review_preserves_analysis_input_errors(text: str, top_k: int) -> None:
    with pytest.raises(AnalysisInputError):
        review_text(text, WordCountPolicy(1, 8), top_k)
