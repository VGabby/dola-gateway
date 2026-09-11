"""Tests for credential-free, manual Google account onboarding."""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from add_account import has_dola_session


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self, url):
        assert url == "https://www.dola.com"
        return self._cookies


def test_session_detector_requires_nonempty_sessionid():
    missing = FakeContext([{"name": "other", "value": "x"}])
    empty = FakeContext([{"name": "sessionid", "value": ""}])
    active = FakeContext([{"name": "sessionid", "value": "present"}])

    assert asyncio.run(has_dola_session(missing)) is False
    assert asyncio.run(has_dola_session(empty)) is False
    assert asyncio.run(has_dola_session(active)) is True


def _load_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOLA_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("DOLA_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("DOLA_PROXY", "")
    monkeypatch.setenv("DOLA_ADMIN_KEY", "")

    import config
    import server

    importlib.reload(config)
    return importlib.reload(server)


def test_account_api_schema_rejects_credentials(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    account = server.AccountAdd.model_validate({"name": "dola-01", "email": ""})
    assert account.name == "dola-01"

    schema = server.app.openapi()["components"]["schemas"]["AccountAdd"]
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {"name", "email"}

    for field in ("password", "totp", "cookies", "sessionid"):
        with pytest.raises(ValidationError):
            server.AccountAdd.model_validate({"name": "dola-01", field: "secret"})


def test_dashboard_uses_manual_google_fields_and_readable_api_errors():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    assert "Open Google Login" in html
    assert 'id="f_pass"' not in html
    assert 'id="f_totp"' not in html
    assert "apiErrorMessage" in html
    assert "knownAccountNames" in html
    assert "Cancel Login" in html
    assert "submitAddAccount(this)" in html


def test_add_job_calls_manual_flow_without_credentials(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    calls = []

    async def fake_manual_login(account):
        calls.append(account)
        return True

    monkeypatch.setattr(server, "add_account_flow", fake_manual_login)
    server.JOBS.clear()
    asyncio.run(server._run_add_job("dola-01", "label@example.com"))

    assert calls == ["dola-01"]
    assert server.JOBS["dola-01"]["status"] == "success"
    assert server.JOBS["dola-01"]["message"] == "Login complete"


def test_different_login_jobs_can_run_and_each_can_be_cancelled(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)

    async def wait_for_cancel(_account):
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "add_account_flow", wait_for_cancel)
    server.JOBS.clear()
    server.ADD_JOB_TASKS.clear()

    with TestClient(server.app) as client:
        first = client.post("/api/admin/accounts", json={"name": "acc1"})
        second = client.post("/api/admin/accounts", json={"name": "acc2"})
        duplicate = client.post("/api/admin/accounts", json={"name": "acc2"})

        assert first.status_code == 202
        assert second.status_code == 202
        assert duplicate.status_code == 409
        assert "already open" in duplicate.json()["detail"]
        assert set(client.get("/api/admin/jobs").json()["jobs"]) == {"acc1", "acc2"}

        for name in ("acc1", "acc2"):
            cancelled = client.delete(f"/api/admin/jobs/{name}")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"


def test_stale_running_marker_does_not_block_retry(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)

    async def wait_for_cancel(_account):
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "add_account_flow", wait_for_cancel)
    server.JOBS.clear()
    server.ADD_JOB_TASKS.clear()
    server.JOBS["acc3"] = {"kind": "add", "status": "running", "started_at": 1}

    with TestClient(server.app) as client:
        response = client.post("/api/admin/accounts", json={"name": "acc3"})
        assert response.status_code == 202
        cancelled = client.delete("/api/admin/jobs/acc3")
        assert cancelled.json()["status"] == "cancelled"


def test_remove_account_cancels_login_and_deletes_profile(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    login_closed = threading.Event()

    async def pending_login(account):
        profile = Path(server.config.ACCOUNTS_DIR) / account
        profile.mkdir(parents=True)
        (profile / "browser-state").write_text("local profile", encoding="utf-8")
        try:
            await asyncio.sleep(3600)
        finally:
            login_closed.set()

    monkeypatch.setattr(server, "add_account_flow", pending_login)
    server.JOBS.clear()
    server.ADD_JOB_TASKS.clear()

    with TestClient(server.app) as client:
        created = client.post("/api/admin/accounts", json={"name": "remove-me"})
        assert created.status_code == 202
        deadline = time.monotonic() + 2
        while "remove-me" not in server.pool.accounts and time.monotonic() < deadline:
            time.sleep(0.01)

        removed = client.delete("/api/admin/accounts/remove-me")

        assert removed.status_code == 200, removed.text
        assert removed.json() == {"ok": True}
        assert login_closed.wait(1)
        assert "remove-me" not in server.pool.accounts
        assert "remove-me" not in server.ADD_JOB_TASKS
        assert "remove-me" not in server.JOBS
        assert not (Path(server.config.ACCOUNTS_DIR) / "remove-me").exists()


def test_remove_account_clears_pool_usage_and_metadata(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    profile = Path(server.config.ACCOUNTS_DIR) / "retired"
    profile.mkdir(parents=True)
    server.pool.set_email("retired", "label@example.com")
    server.pool._claim("retired")

    with TestClient(server.app) as client:
        response = client.delete("/api/admin/accounts/retired")

    assert response.status_code == 200
    assert server.pool._meta("retired") is None
    assert server.pool.used_today("retired") == 0


def test_admin_shutdown_responds_before_requesting_process_stop(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(server, "_shutdown_process", lambda: calls.append("stop"))

    with TestClient(server.app) as client:
        response = client.post("/api/admin/shutdown")

    assert response.status_code == 200
    assert response.json()["message"] == "Gateway is stopping"
    assert calls == ["stop"]
