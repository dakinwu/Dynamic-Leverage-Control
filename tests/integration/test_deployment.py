"""Deployment defaults: the system probe and Compose secret mounts must fail safe."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dynamic_leverage.api.app import create_app
from dynamic_leverage.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = {"base_url": "http://localhost", "headers": {"X-DVL-Client": "console"}}


@pytest.mark.sqlite_only  # Simulates the outage with an unreachable SQLite path.
def test_system_probe_returns_503_when_the_database_is_down(tmp_path: Path) -> None:
    unreachable = tmp_path / "missing-dir" / "db.sqlite"
    app = create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite+pysqlite:///{unreachable.as_posix()}",
            auto_create_schema=False,
        )
    )
    with TestClient(app, raise_server_exceptions=False, **CONSOLE) as client:
        response = client.get("/api/v1/system")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
    assert response.json()["production_order_submission"] == "disabled"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_without_env_mounts_the_existing_empty_secret_template(tmp_path: Path) -> None:
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "DVL_SECRET_FILE"}
    resolved = subprocess.run(
        ["docker", "compose", "--env-file", str(empty_env), "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(resolved.stdout)["services"]
    mounts = [
        volume
        for service in services.values()
        for volume in service.get("volumes", [])
        if volume.get("target") == "/run/secrets/runtime.env"
    ]

    assert mounts
    for mount in mounts:
        source = Path(mount["source"])
        assert source.resolve() == (ROOT / "config" / "empty-secrets.env").resolve()
        assert source.is_file()
        # Older Compose omits a false create_host_path from `config` output, so
        # absent is accepted here; the declaration itself is checked below.
        assert mount.get("bind", {}).get("create_host_path", False) is False


def test_compose_secret_mounts_declare_no_host_path_creation() -> None:
    lines = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    targets = [i for i, line in enumerate(lines) if "target: /run/secrets/runtime.env" in line]

    assert targets
    for i in targets:
        assert any("create_host_path: false" in line for line in lines[i + 1 : i + 5])
