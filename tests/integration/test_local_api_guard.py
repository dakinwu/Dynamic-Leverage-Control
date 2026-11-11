"""A web page on another origin must not be able to drive the local API."""

from fastapi.testclient import TestClient

from dynamic_leverage.api.app import create_app
from dynamic_leverage.config import Settings

CONSOLE_HEADER = {"X-DVL-Client": "console"}


def _app():
    return create_app(
        Settings(
            app_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            auto_create_schema=True,
        )
    )


def _halt_active(client: TestClient) -> bool:
    return client.get("/api/v1/operations/summary").json()["manual_halt"]["active"]


def test_post_without_console_header_is_forbidden_and_has_no_effect() -> None:
    with TestClient(_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/api/v1/risk/manual-halt",
            json={"actor": "attacker", "reason": "cross-site"},
        )
        assert response.status_code == 403
        assert _halt_active(client) is False


def test_cross_site_form_post_is_forbidden() -> None:
    with TestClient(_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/api/v1/risk/manual-halt/release",
            content=b'{"actor": "attacker", "reason": "x"}',
            headers={"Content-Type": "text/plain"},
        )
    assert response.status_code == 403


def test_request_with_foreign_host_is_rejected() -> None:
    # DNS rebinding: a hostile name resolving to 127.0.0.1 keeps its own Host header.
    with TestClient(_app(), base_url="http://attacker.example:8000") as client:
        response = client.get("/api/v1/system")
    assert response.status_code == 400


def test_non_json_body_is_unsupported_even_with_console_header() -> None:
    with TestClient(_app(), base_url="http://localhost:8000", headers=CONSOLE_HEADER) as client:
        plain = client.post(
            "/api/v1/risk/manual-halt",
            content=b'{"actor": "ops", "reason": "x"}',
            headers={"Content-Type": "text/plain"},
        )
        untyped = client.post(
            "/api/v1/risk/manual-halt",
            content=b'{"actor": "ops", "reason": "x"}',
        )
        assert plain.status_code == 415
        assert untyped.status_code == 415
        assert _halt_active(client) is False


def test_console_json_post_is_accepted() -> None:
    with TestClient(_app(), base_url="http://localhost:8000", headers=CONSOLE_HEADER) as client:
        response = client.post(
            "/api/v1/risk/manual-halt",
            json={"actor": "ops", "reason": "volatility review"},
        )
        assert response.status_code == 200
        assert _halt_active(client) is True


def test_console_post_without_body_is_accepted() -> None:
    with TestClient(_app(), base_url="http://localhost:8000", headers=CONSOLE_HEADER) as client:
        response = client.post("/api/v1/account/sync")
    # Reaches the endpoint, which refuses for its own reason (no account binding).
    assert response.status_code == 409


def test_cors_preflight_allows_the_console_header_from_the_web_origin() -> None:
    with TestClient(_app(), base_url="http://localhost:8000") as client:
        response = client.options(
            "/api/v1/risk/manual-halt",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-dvl-client",
            },
        )
    assert response.status_code == 200
    assert "x-dvl-client" in response.headers["access-control-allow-headers"].lower()


def test_openapi_lets_swagger_ui_send_the_console_header() -> None:
    with TestClient(_app(), base_url="http://localhost:8000") as client:
        schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"].values()
    assert {"type": "apiKey", "in": "header", "name": "X-DVL-Client"}.items() <= next(
        iter(schemes)
    ).items()
