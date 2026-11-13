"""Optionally run the database-backed tests against PostgreSQL (used by CI).

With ``DVL_TEST_POSTGRES_URL`` set, every engine the code under test creates
from a SQLite URL is redirected to that PostgreSQL database, and each test
starts from an empty ``public`` schema. Tests that depend on SQLite itself are
marked ``sqlite_only`` and skipped. Without the variable this file does nothing.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from dynamic_leverage.infrastructure import database

POSTGRES_URL = os.environ.get("DVL_TEST_POSTGRES_URL")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "sqlite_only: relies on SQLite behaviour; skipped against PostgreSQL"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not POSTGRES_URL:
        return
    skip = pytest.mark.skip(reason="SQLite-specific; DVL_TEST_POSTGRES_URL is set")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _postgres_instead_of_sqlite(monkeypatch: pytest.MonkeyPatch):
    if not POSTGRES_URL:
        yield
        return
    real_create_engine = database.create_engine
    admin = real_create_engine(POSTGRES_URL)
    with admin.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    admin.dispose()

    def redirected(url, **kwargs):
        if str(url).startswith("sqlite"):
            return real_create_engine(POSTGRES_URL, pool_pre_ping=True)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(database, "create_engine", redirected)
    yield
