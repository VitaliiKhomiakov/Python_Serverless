import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field


def test_sample_response(client: TestClient) -> None:
    response = client.post("/analyze", json={"text": "Привет, мир! Привет."})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    payload: object = json.loads(response.content)
    assert payload == {
        "characters": 20,
        "words": 3,
        "top_words": [{"word": "привет", "count": 2}, {"word": "мир", "count": 1}],
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"text": "b a b a c", "top_k": 1},
            {"characters": 9, "words": 5, "top_words": [{"word": "a", "count": 2}]},
        ),
        (
            {"text": "e\u0301 É", "top_k": 10},
            {"characters": 4, "words": 2, "top_words": [{"word": "é", "count": 2}]},
        ),
        ({"text": "123!?"}, {"characters": 5, "words": 0, "top_words": []}),
        (
            {"text": "a" * 10000},
            {
                "characters": 10000,
                "words": 1,
                "top_words": [{"word": "a" * 10000, "count": 1}],
            },
        ),
    ],
)
def test_successful_boundaries(
    client: TestClient, body: dict[str, str | int], expected: dict[str, object]
) -> None:
    response = client.post("/analyze", json=body)
    assert response.status_code == 200
    payload: object = json.loads(response.content)
    assert payload == expected


def test_default_top_k_over_http(client: TestClient) -> None:
    response = client.post("/analyze", json={"text": "f e d c b a"})
    assert response.status_code == 200
    payload: object = json.loads(response.content)
    assert payload == {
        "characters": 11,
        "words": 6,
        "top_words": [
            {"word": "a", "count": 1},
            {"word": "b", "count": 1},
            {"word": "c", "count": 1},
            {"word": "d", "count": 1},
            {"word": "e", "count": 1},
        ],
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": None},
        {"text": 123},
        {"text": True},
        {"text": []},
        {"text": ""},
        {"text": " \t\n\u2003"},
        {"text": "x" * 10001},
        {"text": "a", "top_k": None},
        {"text": "a", "top_k": True},
        {"text": "a", "top_k": False},
        {"text": "a", "top_k": "2"},
        {"text": "a", "top_k": 2.0},
        {"text": "a", "top_k": 1.5},
        {"text": "a", "top_k": 0},
        {"text": "a", "top_k": 11},
        {"text": "a", "unexpected": "value"},
        [],
        "a",
        None,
    ],
)
def test_invalid_json_values_have_owned_error(client: TestClient, body: object) -> None:
    response = client.post(
        "/analyze",
        content=json.dumps(body),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    payload: object = json.loads(response.content)
    assert payload == {"detail": "Invalid request."}


@pytest.mark.parametrize(
    "body",
    ['{"text": "private-marker",', '{"text":"private-marker","top_k":"2"}'],
)
@pytest.mark.parametrize("endpoint", ["/analyze", "/review-text"])
def test_errors_do_not_echo_or_log_input(
    client: TestClient, caplog: pytest.LogCaptureFixture, body: str, endpoint: str
) -> None:
    response = client.post(
        endpoint, content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    payload: object = json.loads(response.content)
    assert payload == {"detail": "Invalid request."}
    assert "private-marker" not in response.text
    assert "private-marker" not in caplog.text


@pytest.mark.parametrize("endpoint", ["/analyze", "/review-text"])
def test_invalid_json_encoding_uses_owned_error(
    client: TestClient, endpoint: str
) -> None:
    response = client.post(
        endpoint,
        content=b'{"text":"\xff"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    payload: object = json.loads(response.content)
    assert payload == {"detail": "Invalid request."}


def test_unrelated_http_errors_keep_their_status(client: TestClient) -> None:
    assert client.get("/missing").status_code == 404
    assert client.get("/analyze").status_code == 405
    assert client.get("/review-text").status_code == 405


# Validate application-owned OpenAPI fields; ignore unrelated upstream extensions.
class PropertySchema(BaseModel):
    type: str | None = None
    ref: str | None = Field(default=None, alias="$ref")
    min_length: int | None = Field(default=None, alias="minLength")
    max_length: int | None = Field(default=None, alias="maxLength")
    minimum: int | None = None
    maximum: int | None = None
    default: int | str | None = None


class RequestSchema(BaseModel):
    required: list[str]
    properties: dict[str, PropertySchema]
    additional_properties: bool = Field(alias="additionalProperties")


class Components(BaseModel):
    schemas: dict[str, object]


class SchemaReference(BaseModel):
    ref: str = Field(alias="$ref")


class MediaType(BaseModel):
    schema_: SchemaReference = Field(alias="schema")


class ResponseDescription(BaseModel):
    content: dict[str, MediaType]


class Operation(BaseModel):
    responses: dict[str, ResponseDescription]


class OpenAPIContract(BaseModel):
    components: Components
    paths: dict[str, dict[str, Operation]]


def test_openapi_matches_request_and_error_contract(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    document = OpenAPIContract.model_validate_json(response.content)
    request = RequestSchema.model_validate(
        document.components.schemas["AnalyzeRequest"]
    )
    assert request.required == ["text"]
    assert request.additional_properties is False
    assert request.properties["text"].type == "string"
    assert request.properties["text"].min_length == 1
    assert request.properties["text"].max_length == 10000
    assert request.properties["top_k"].type == "integer"
    assert request.properties["top_k"].minimum == 1
    assert request.properties["top_k"].maximum == 10
    assert request.properties["top_k"].default == 5
    error = document.paths["/analyze"]["post"].responses["422"]
    assert error.content["application/json"].schema_.ref.endswith(
        "/InvalidRequestResponse"
    )


class StatusSchema(BaseModel):
    enum: list[str]


class AssessmentSchema(BaseModel):
    properties: dict[str, PropertySchema]


def test_review_openapi_exposes_bounds_required_fields_status_and_errors(
    client: TestClient,
) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    document = OpenAPIContract.model_validate_json(response.content)
    schemas = document.components.schemas
    request = RequestSchema.model_validate(schemas["ReviewTextRequest"])
    assert set(request.required) == {"text", "min_words", "max_words"}
    assert request.additional_properties is False
    for field in ("min_words", "max_words"):
        assert request.properties[field].type == "integer"
        assert request.properties[field].minimum == 1
        assert request.properties[field].maximum == 10000
    assert request.properties["top_k"].minimum == 1
    assert request.properties["top_k"].maximum == 10
    assert request.properties["top_k"].default == 5
    status = StatusSchema.model_validate(schemas["WordCountStatus"])
    assert status.enum == ["too_short", "within_range", "too_long"]
    assessment = AssessmentSchema.model_validate(schemas["WordCountCheckResponse"])
    assert assessment.properties["status"].ref == (
        "#/components/schemas/WordCountStatus"
    )
    responses = document.paths["/review-text"]["post"].responses
    assert (
        responses["200"]
        .content["application/json"]
        .schema_.ref.endswith("/ReviewTextResponse")
    )
    assert (
        responses["422"]
        .content["application/json"]
        .schema_.ref.endswith("/InvalidRequestResponse")
    )
