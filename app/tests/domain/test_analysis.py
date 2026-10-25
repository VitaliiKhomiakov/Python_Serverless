import pytest

from serverless_lab.domain.analysis import (
    AnalysisInputError,
    AnalysisResult,
    WordCount,
    analyze_text,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Привет, мир! Привет.",
            AnalysisResult(20, 3, (WordCount("привет", 2), WordCount("мир", 1))),
        ),
        (
            "Hello, HELLO world!",
            AnalysisResult(19, 3, (WordCount("hello", 2), WordCount("world", 1))),
        ),
        ("e\u0301 É", AnalysisResult(4, 2, (WordCount("é", 2),))),
        ("Straße STRASSE", AnalysisResult(14, 2, (WordCount("strasse", 2),))),
        ("İx", AnalysisResult(2, 2, (WordCount("i", 1), WordCount("x", 1)))),
        ("123!?", AnalysisResult(5, 0, ())),
        ("👩‍💻", AnalysisResult(3, 0, ())),
        ("  A!  ", AnalysisResult(6, 1, (WordCount("a", 1),))),
    ],
)
def test_counts_original_characters_and_normalized_words(
    text: str, expected: AnalysisResult
) -> None:
    assert analyze_text(text) == expected


def test_digits_hyphens_and_underscores_separate_words() -> None:
    result = analyze_text("one-two 123 three_four")
    assert result.words == 4
    assert result.top_words == (
        WordCount("four", 1),
        WordCount("one", 1),
        WordCount("three", 1),
        WordCount("two", 1),
    )


def test_frequency_then_alphabetical_order_precedes_truncation() -> None:
    assert analyze_text("b a b a c", top_k=2) == AnalysisResult(
        9, 5, (WordCount("a", 2), WordCount("b", 2))
    )


def test_default_top_k_is_five_without_truncating_total_word_count() -> None:
    assert analyze_text("f e d c b a") == AnalysisResult(
        11,
        6,
        (
            WordCount("a", 1),
            WordCount("b", 1),
            WordCount("c", 1),
            WordCount("d", 1),
            WordCount("e", 1),
        ),
    )


@pytest.mark.parametrize("top_k", [1, 10])
def test_accepts_top_k_endpoints_and_returns_only_existing_words(top_k: int) -> None:
    assert analyze_text("a", top_k) == AnalysisResult(1, 1, (WordCount("a", 1),))


def test_accepts_maximum_original_length() -> None:
    assert analyze_text("a " * 5000) == AnalysisResult(
        10000, 5000, (WordCount("a", 5000),)
    )


@pytest.mark.parametrize("text", ["", " ", "\t\n", "\u2003", "a" * 10001])
def test_rejects_empty_blank_and_oversized_text(text: str) -> None:
    with pytest.raises(AnalysisInputError):
        analyze_text(text)


@pytest.mark.parametrize("top_k", [0, -1, 11, True, False])
def test_rejects_out_of_range_and_boolean_top_k(top_k: int) -> None:
    with pytest.raises(AnalysisInputError):
        analyze_text("private input", top_k)


def test_calls_do_not_accumulate_state() -> None:
    first = analyze_text("a a")
    assert analyze_text("b") == AnalysisResult(1, 1, (WordCount("b", 1),))
    assert analyze_text("a a") == first == AnalysisResult(3, 2, (WordCount("a", 2),))
