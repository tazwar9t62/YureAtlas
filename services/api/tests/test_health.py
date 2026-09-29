from fastapi.testclient import TestClient
from yureatlas.api import app


def test_local_api_boundary():
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/experiments", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/experiments").status_code == 403
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 400
