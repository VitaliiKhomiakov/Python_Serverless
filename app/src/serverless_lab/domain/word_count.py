from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MIN_WORD_LIMIT: Final = 1
MAX_WORD_LIMIT: Final = 10_000


class WordCountInputError(ValueError):
    """A word-count policy or count violates the supported value bounds."""


@dataclass(frozen=True)
class WordCountPolicy:
    min_words: int
    max_words: int

    def __post_init__(self) -> None:
        if isinstance(self.min_words, bool) or isinstance(self.max_words, bool):
            raise WordCountInputError("Word-count bounds must not be booleans.")
        if not MIN_WORD_LIMIT <= self.min_words <= self.max_words <= MAX_WORD_LIMIT:
            raise WordCountInputError(
                "Word-count bounds must satisfy 1 <= min_words <= max_words <= 10000."
            )


class WordCountStatus(StrEnum):
    TOO_SHORT = "too_short"
    WITHIN_RANGE = "within_range"
    TOO_LONG = "too_long"


def assess_word_count(words: int, policy: WordCountPolicy) -> WordCountStatus:
    """Assess a nonnegative total word count against an inclusive policy."""
    if isinstance(words, bool) or words < 0:
        raise WordCountInputError("Word count must be nonnegative and not a boolean.")
    if words < policy.min_words:
        return WordCountStatus.TOO_SHORT
    if words > policy.max_words:
        return WordCountStatus.TOO_LONG
    return WordCountStatus.WITHIN_RANGE
