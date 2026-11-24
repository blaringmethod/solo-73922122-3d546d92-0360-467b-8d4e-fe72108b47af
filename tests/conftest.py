"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "strat.db"


@pytest.fixture()
def settings(db_path: Path) -> Settings:
    return Settings(db_path=db_path, busy_timeout_ms=5_000)


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded(client: TestClient) -> dict:
    response = client.post("/demo/seed")
    assert response.status_code == 200, response.text
    return response.json()
