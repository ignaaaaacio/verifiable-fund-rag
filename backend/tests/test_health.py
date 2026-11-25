from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_status_and_model():
    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["database"], bool)
    assert body["model"] == "gpt-6-luna"


def test_openapi_is_generated():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/v1/health" in response.json()["paths"]
