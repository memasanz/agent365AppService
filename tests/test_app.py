from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import PROJECT


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENABLE_AGENT365_TELEMETRY", "false")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", PROJECT)
    monkeypatch.setenv("FOUNDRY_MODEL", "test-model")
    from src import app as module

    agent = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="Hello")))
    monkeypatch.setattr(module, "Agent", Mock(return_value=agent))
    monkeypatch.setattr(
        module, "FoundryChatClient", Mock(return_value=SimpleNamespace(close=AsyncMock()))
    )
    monkeypatch.setattr(
        module, "AzureCliCredential", Mock(return_value=SimpleNamespace(close=AsyncMock()))
    )
    monkeypatch.setattr(module, "telemetry", None)
    with TestClient(module.app) as client:
        yield client, agent


def test_baseline_health_and_chat_without_telemetry_credentials(client):
    web, agent = client
    assert web.get("/health").json() == {"status": "ok"}
    response = web.post("/chat", json={"message": "Hello", "conversation_id": "test-001"})
    assert response.status_code == 200
    assert response.json() == {"reply": "Hello", "conversation_id": "test-001", "trace_id": None}
    agent.run.assert_awaited_once_with("Hello")
    assert web.post("/chat", json={"message": "Hi"}).json()["conversation_id"]


@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": " "},
        {"message": 1},
        {"message": ["x"]},
        {"message": "x" * 8001},
        {"message": "ok", "conversation_id": 3},
        {"message": "ok", "conversation_id": "with spaces"},
        {"message": "ok", "unexpected": "PRIVATE"},
    ],
)
def test_input_limits_and_sanitized_validation(client, body):
    web, agent = client
    response = web.post("/chat", json=body)
    assert response.status_code == 422
    assert "PRIVATE" not in response.text
    agent.run.assert_not_awaited()


def test_oversized_request_body_is_rejected(client):
    response = client[0].post("/chat", content=b"x" * 40001)
    assert response.status_code == 413


def test_model_errors_are_sanitized(client, caplog):
    web, agent = client
    agent.run.side_effect = RuntimeError("SECRET MODEL RESPONSE")
    response = web.post("/chat", json={"message": "PRIVATE"})
    assert response.status_code == 502
    assert "SECRET" not in response.text + caplog.text


def test_hosted_model_explicitly_selects_system_assigned_identity(client, monkeypatch):
    from src import app as module

    credential = Mock(return_value=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "test-host")
    monkeypatch.setenv("AZURE_CLIENT_ID", "not-the-model-identity")
    monkeypatch.setattr(module, "ManagedIdentityCredential", credential)
    with TestClient(module.app):
        credential.assert_called_once_with()
