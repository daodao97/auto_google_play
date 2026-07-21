from fastapi.testclient import TestClient

from chatgpt_register.presentation.web import NO_STORE_HEADERS, app


def test_web_console_and_health_are_loopback_only(monkeypatch):
    monkeypatch.setenv("MAILCOM_APP_TOKEN", "test-secret")
    with TestClient(app) as client:
        page = client.get("/")
        health = client.get("/health")
    assert page.status_code == 200
    assert "ChatGPT 注册机" in page.text
    assert health.json() == {"ok": True, "mail_token_configured": True}
    assert page.headers["cache-control"] == NO_STORE_HEADERS["Cache-Control"]
    assert "test-secret" not in page.text
    assert "test-secret" not in health.text


def test_run_requires_server_side_mail_token(monkeypatch):
    monkeypatch.delenv("MAILCOM_APP_TOKEN", raising=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/run",
            json={"accounts": "user@example.com----mail-password"},
        )
    assert response.status_code == 503
    assert "mail-password" not in response.text
