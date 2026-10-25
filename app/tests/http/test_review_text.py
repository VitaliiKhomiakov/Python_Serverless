import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel


def test_review_sample_response(client: TestClient) -> None:
    response = client.post(
        "/review-text",
        json={"text": "Привет, мир! Привет.", "min_words": 4, "max_words": 8},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    payload: object = json.loads(response.content)
    assert payload == {
        "analysis": {
            "characters": 20,
            "words": 3,
            "top_words": [{"word": "привет", "count": 2}, {"word": "мир", "count": 1}],
        },
        "word_count_check": {"min_words": 4, "max_words": 8, "status": "too_short"},
    }


@pytest.mark.parametrize(
    ("text", "min_words", "max_words", "words", "status"),
    [
        ("123!?", 1, 2, 0, "too_short"),
        ("one", 1, 2, 1, "within_range"),
        ("one two", 1, 2, 2, "within_range"),
        ("one two three", 1, 2, 3, "too_long"),
        ("one", 1, 1, 1, "within_range"),
        ("one", 10000, 10000, 1, "too_short"),
    ],
)
def test_valid_reviews_return_200_for_each_assessment(
    client: TestClient,
    text: str,
    min_words: int,
    max_words: int,
    words: int,
    status: str,
) -> None:
    response = client.post(
        "/review-text",
        json={"text": text, "min_words": min_words, "max_words": max_words},
    )
    assert response.status_code == 200
    result = ReviewPayload.model_validate_json(response.content)
    assert result.analysis.words == words
    assert result.word_count_check == AssessmentPayload(
        min_words=min_words, max_words=max_words, status=status
    )


def test_review_preserves_unicode_and_assesses_total_with_top_k(
    client: TestClient,
) -> None:
    response = client.post(
        "/review-text",
        json={
            "text": "e\u0301 É Straße STRASSE",
            "min_words": 1,
            "max_words": 3,
            "top_k": 1,
        },
    )
    assert response.status_code == 200
    payload: object = json.loads(response.content)
    assert payload == {
        "analysis": {
            "characters": 19,
            "words": 4,
            "top_words": [{"word": "strasse", "count": 2}],
        },
        "word_count_check": {"min_words": 1, "max_words": 3, "status": "too_long"},
    }


@pytest.mark.parametrize(
    "body",
    [
        {"min_words": 1, "max_words": 8},
        {"text": "a", "max_words": 8},
        {"text": "a", "min_words": 1},
        {"text": "a", "min_words": 8, "max_words": 4},
        {"text": "a", "min_words": 0, "max_words": 8},
        {"text": "a", "min_words": 1, "max_words": 10001},
        {"text": "a", "min_words": True, "max_words": 8},
        {"text": "a", "min_words": 1, "max_words": True},
        {"text": "a", "min_words": "1", "max_words": 8},
        {"text": "a", "min_words": 1, "max_words": "8"},
        {"text": "a", "min_words": 1.0, "max_words": 8},
        {"text": "a", "min_words": 1, "max_words": 8.5},
        {"text": "a", "min_words": None, "max_words": 8},
        {"text": "a", "min_words": 1, "max_words": None},
        {"text": 123, "min_words": 1, "max_words": 8},
        {"text": " \t\n", "min_words": 1, "max_words": 8},
        {"text": "a" * 10001, "min_words": 1, "max_words": 8},
        {"text": "a", "min_words": 1, "max_words": 8, "top_k": True},
        {"text": "a", "min_words": 1, "max_words": 8, "top_k": "2"},
        {"text": "a", "min_words": 1, "max_words": 8, "top_k": 2.0},
        {"text": "a", "min_words": 1, "max_words": 8, "top_k": 11},
        {"text": "a", "min_words": 1, "max_words": 8, "unexpected": "value"},
    ],
)
def test_invalid_review_requests_use_owned_error(
    client: TestClient, body: object
) -> None:
    response = client.post(
        "/review-text",
        content=json.dumps(body),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    payload: object = json.loads(response.content)
    assert payload == {"detail": "Invalid request."}


class AnalysisPayload(BaseModel):
    words: int


class AssessmentPayload(BaseModel):
    min_words: int
    max_words: int
    status: str


class ReviewPayload(BaseModel):
    analysis: AnalysisPayload
    word_count_check: AssessmentPayload
