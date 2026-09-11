"""Offline checks for the cohesive application entry points."""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def _load_server(monkeypatch, tmp_path, *, admin_key="", api_keys=""):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOLA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DOLA_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("DOLA_POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setenv("DOLA_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("DOLA_IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("DOLA_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("DOLA_PROXY", "")
    monkeypatch.setenv("DOLA_API_KEYS", api_keys)
    monkeypatch.setenv("DOLA_ADMIN_KEY", admin_key)

    import config
    import server

    importlib.reload(config)
    return importlib.reload(server)


def test_app_version_has_one_file_source():
    import app_version

    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert expected
    assert app_version.APP_VERSION == expected


def test_factory_and_application_routes(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    assert server.create_app() is server.app
    assert server.app.version == server.APP_VERSION

    with TestClient(server.app) as client:
        root = client.get("/")
        playground = client.get("/playground")
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert root.status_code == 200
    assert root.content == playground.content
    assert live.json() == {
        "ok": True,
        "status": "live",
        "version": server.APP_VERSION,
    }
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["setup_complete"] is False


def test_bootstrap_is_useful_and_does_not_expose_secrets(monkeypatch, tmp_path):
    server = _load_server(
        monkeypatch,
        tmp_path,
        admin_key="owner-secret",
        api_keys="client-secret",
    )

    with TestClient(server.app) as client:
        response = client.get("/api/app/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == server.APP_VERSION
    assert payload["auth_required"] is True
    assert payload["client_key_configured"] is True
    assert payload["setup_complete"] is False
    assert payload["docs_url"] == "/docs"
    assert "owner-secret" not in response.text
    assert "client-secret" not in response.text


def test_liveness_survives_readiness_failure(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)

    def broken_pending_count():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(server.store, "pending_task_count", broken_pending_count)
    with TestClient(server.app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "live"
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
