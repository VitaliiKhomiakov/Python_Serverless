import pytest

from serverless_lab.domain.word_count import (
    WordCountInputError,
    WordCountPolicy,
    WordCountStatus,
    assess_word_count,
)


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (0, WordCountStatus.TOO_SHORT),
        (3, WordCountStatus.TOO_SHORT),
        (4, WordCountStatus.WITHIN_RANGE),
        (6, WordCountStatus.WITHIN_RANGE),
        (8, WordCountStatus.WITHIN_RANGE),
        (9, WordCountStatus.TOO_LONG),
    ],
)
def test_assesses_total_words_against_inclusive_range(
    words: int, expected: WordCountStatus
) -> None:
    assert assess_word_count(words, WordCountPolicy(4, 8)) == expected


@pytest.mark.parametrize("bound", [1, 10000])
def test_equal_bounds_and_policy_endpoints_are_valid(bound: int) -> None:
    assert assess_word_count(bound, WordCountPolicy(bound, bound)) == (
        WordCountStatus.WITHIN_RANGE
    )


@pytest.mark.parametrize(
    ("min_words", "max_words"),
    [(0, 8), (-1, 8), (1, 10001), (10001, 10001), (8, 4), (True, 8), (1, True)],
)
def test_rejects_invalid_policy_bounds(min_words: int, max_words: int) -> None:
    with pytest.raises(WordCountInputError):
        WordCountPolicy(min_words, max_words)


@pytest.mark.parametrize("words", [-1, True, False])
def test_rejects_negative_and_boolean_word_counts(words: int) -> None:
    with pytest.raises(WordCountInputError):
        assess_word_count(words, WordCountPolicy(1, 8))
