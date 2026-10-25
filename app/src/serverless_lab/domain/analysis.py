from collections import Counter
from dataclasses import dataclass
from typing import Final
from unicodedata import normalize

MIN_TEXT_LENGTH: Final = 1
MAX_TEXT_LENGTH: Final = 10_000
MIN_TOP_K: Final = 1
MAX_TOP_K: Final = 10
DEFAULT_TOP_K: Final = 5


class AnalysisInputError(ValueError):
    """A typed analysis argument violates the supported value bounds."""


@dataclass(frozen=True)
class WordCount:
    word: str
    count: int


@dataclass(frozen=True)
class AnalysisResult:
    characters: int
    words: int
    top_words: tuple[WordCount, ...]


def analyze_text(text: str, top_k: int = DEFAULT_TOP_K) -> AnalysisResult:
    """Count original code points and words after NFC normalization and casefold().

    Accept non-blank text of length 1-10000 and top_k 1-10, excluding bool.
    Raise AnalysisInputError for values outside these bounds.
    """
    if not MIN_TEXT_LENGTH <= len(text) <= MAX_TEXT_LENGTH or text.isspace():
        raise AnalysisInputError("Text must have length 1-10000 and must not be blank.")
    if isinstance(top_k, bool) or not MIN_TOP_K <= top_k <= MAX_TOP_K:
        raise AnalysisInputError("top_k must be an integer between 1 and 10.")

    normalized = normalize("NFC", text).casefold()
    words = "".join(char if char.isalpha() else " " for char in normalized).split()
    counts = Counter(words)
    ranked = sorted(counts.items(), key=_ranking_key)
    return AnalysisResult(
        characters=len(text),
        words=len(words),
        top_words=tuple(WordCount(word, count) for word, count in ranked[:top_k]),
    )


def _ranking_key(item: tuple[str, int]) -> tuple[int, str]:
    word, count = item
    return -count, word
