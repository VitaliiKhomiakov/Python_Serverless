import re

import pytest
from fastapi.testclient import TestClient

from serverless_lab.app_factory import create_app


def test_removed_delay_configuration_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_DELAY_MS", "invalid")
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-instance-id"]
    assert response.headers["x-revision"]


def test_factory_captures_environment_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTNAME", "pod-one")
    monkeypatch.setenv("K_REVISION", "revision-one")
    app = create_app()
    monkeypatch.setenv("HOSTNAME", "pod-two")
    monkeypatch.setenv("K_REVISION", "revision-two")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.headers["x-instance-id"] == "pod-one"
    assert response.headers["x-revision"] == "revision-one"


@pytest.mark.parametrize("value", [None, "", "unsafe\r\nheader", "юникод", "x" * 200])
def test_runtime_identity_fallbacks_are_safe_and_stable(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    for key in ("HOSTNAME", "K_REVISION"):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    with TestClient(create_app()) as client:
        first = client.get("/health")
        second = client.get("/health")
    for header in ("x-instance-id", "x-revision"):
        assert re.fullmatch(r"[A-Za-z0-9._-]{1,128}", first.headers[header])
        assert first.headers[header] == second.headers[header]
