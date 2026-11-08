from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service_block(compose_text: str, service: str) -> str:
    lines = compose_text.splitlines()
    start = lines.index(f"  {service}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            end = index
            break
    return "\n".join(lines[start:end])


def test_every_runtime_service_recovers_after_docker_engine_restart() -> None:
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for service in ("postgres", "api", "web", "scheduler"):
        block = _service_block(compose_text, service)
        assert "restart: unless-stopped" in block, f"{service} has no restart policy"


def test_nginx_resolves_api_through_docker_dns_at_request_time() -> None:
    nginx_config = (PROJECT_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "resolver 127.0.0.11" in nginx_config
    assert "set $api_upstream api:8000;" in nginx_config
    assert "proxy_pass http://$api_upstream;" in nginx_config
