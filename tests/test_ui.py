from fastapi.testclient import TestClient

from converter.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_root_serves_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Format Converter" in r.text


def test_static_assets():
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_existing_routes_unaffected():
    assert client.get("/docs").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/convert/json-to-yaml", content='{"a": 1}').status_code == 200
    assert client.get("/nope").status_code == 404
