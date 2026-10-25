import json

from fastapi.testclient import TestClient


def test_health_reports_process_responsiveness(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload: object = json.loads(response.content)
    assert payload == {"status": "ok"}
