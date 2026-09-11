"""Offline coverage for the desktop process and loopback security boundary."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


PORT = 18765
BASE_URL = f"http://127.0.0.1:{PORT}"


def _load_desktop(
    monkeypatch,
    tmp_path,
    *,
    token="desktop-owner-token",
    instance_id="desktop-instance-one",
    local_api=False,
    api_keys="",
    admin_key="",
):
    state = tmp_path / "state"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOLA_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    monkeypatch.setenv("DOLA_STATE_DIR", str(state))
    monkeypatch.setenv("DOLA_HOST", "127.0.0.1")
    monkeypatch.setenv("DOLA_PORT", str(PORT))
    monkeypatch.setenv("DOLA_PUBLIC_BASE", BASE_URL)
    monkeypatch.setenv("DOLA_DESKTOP_TOKEN", token)
    monkeypatch.setenv("DOLA_INSTANCE_ID", instance_id)
    monkeypatch.setenv("DOLA_LOCAL_API_ENABLED", "1" if local_api else "0")
    monkeypatch.setenv("DOLA_API_KEYS", api_keys)
    monkeypatch.setenv("DOLA_ADMIN_KEY", admin_key)
    monkeypatch.setenv("DOLA_PROXY", "")

    import config
    import server

    importlib.reload(config)
    return importlib.reload(server), state


def _client(server):
    return TestClient(server.app, base_url=BASE_URL)


def _owner_headers(token="desktop-owner-token"):
    return {"Authorization": f"Bearer {token}"}


def test_desktop_health_and_owner_routes_fail_closed(monkeypatch, tmp_path):
    server, _state = _load_desktop(monkeypatch, tmp_path)

    with _client(server) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        private_health = client.get("/health")
        owner_health = client.get("/health", headers=_owner_headers())
        disabled_api = client.get("/v1/images")
        invalid_api = client.get(
            "/v1/images", headers={"Authorization": "Bearer wrong-token"}
        )
        owner_api = client.get("/v1/images", headers=_owner_headers())
        denied_admin = client.get("/api/admin/accounts")
        owner_admin = client.get(
            "/api/admin/accounts", headers={"X-Admin-Key": "desktop-owner-token"}
        )

    assert live.json() == {
        "ok": True,
        "status": "live",
        "version": server.APP_VERSION,
        "instance_id": "desktop-instance-one",
    }
    assert ready.json() == {
        "ok": True,
        "status": "ready",
        "version": server.APP_VERSION,
        "instance_id": "desktop-instance-one",
    }
    assert private_health.status_code == 401
    assert owner_health.status_code == 200
    assert disabled_api.status_code == 403
    assert invalid_api.status_code == 403
    assert owner_api.status_code == 200
    assert denied_admin.status_code == 401
    assert owner_admin.status_code == 200


def test_optional_local_api_still_requires_a_client_key(monkeypatch, tmp_path):
    server, _state = _load_desktop(
        monkeypatch,
        tmp_path,
        local_api=True,
        api_keys="client-integration-key",
    )

    with _client(server) as client:
        missing = client.get("/v1/images")
        invalid = client.get(
            "/v1/images", headers={"Authorization": "Bearer not-the-key"}
        )
        allowed = client.get(
            "/v1/images",
            headers={"Authorization": "Bearer client-integration-key"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert allowed.status_code == 200


def test_desktop_rejects_untrusted_hosts_and_origins(monkeypatch, tmp_path):
    server, _state = _load_desktop(monkeypatch, tmp_path)

    with _client(server) as client:
        bad_host = client.get(
            "/health/live", headers={"Host": "attacker.example"}
        )
        bad_origin = client.get(
            "/health/live", headers={"Origin": "https://attacker.example"}
        )
        good_origin = client.get(
            "/health/live", headers={"Origin": BASE_URL}
        )

    assert bad_host.status_code == 400
    assert bad_origin.status_code == 403
    assert good_origin.status_code == 200


def test_public_video_mount_is_disabled_in_desktop_mode(monkeypatch, tmp_path):
    server, state = _load_desktop(monkeypatch, tmp_path)
    media = state / "downloads" / "private.mp4"
    media.write_bytes(b"private-media")

    with _client(server) as client:
        response = client.get("/videos/private.mp4", headers=_owner_headers())

    assert response.status_code == 404
    assert response.content != b"private-media"


def test_desktop_owner_history_survives_launch_token_rotation(monkeypatch, tmp_path):
    first, _state = _load_desktop(monkeypatch, tmp_path, token="first-launch-token")
    with _client(first) as client:
        first.store.create(
            "image_owner_history",
            "dola-image",
            "owner history",
            "1:1",
            0,
            api_key_hash=first.DESKTOP_OWNER_HASH,
            api_key_name="Desktop Owner",
            media_type="image",
        )
        initial = client.get(
            "/v1/images", headers=_owner_headers("first-launch-token")
        )
        assert [row["id"] for row in initial.json()] == ["image_owner_history"]

    second, _state = _load_desktop(monkeypatch, tmp_path, token="second-launch-token")
    with _client(second) as client:
        rotated = client.get(
            "/v1/images", headers=_owner_headers("second-launch-token")
        )

    assert [row["id"] for row in rotated.json()] == ["image_owner_history"]


def test_support_bundle_excludes_private_payloads_and_scrubs_logs(
    monkeypatch, tmp_path
):
    server, state = _load_desktop(
        monkeypatch,
        tmp_path,
        token="known-desktop-secret-token",
        api_keys="known-client-secret-key",
        admin_key="known-admin-secret-key",
    )
    (state / ".env.local").write_text(
        "DOLA_ADMIN_KEY=known-admin-secret-key\n", encoding="utf-8"
    )
    profile = state / "accounts" / "friend"
    profile.mkdir()
    (profile / "Cookies").write_text("profile-cookie-secret", encoding="utf-8")
    (state / "downloads" / "private.png").write_bytes(b"private-media-secret")
    log = state / "logs" / "server.err.log"
    log.write_text(
        "server ready\n"
        "Authorization: Bearer known-client-secret-key\n"
        "owner=known-desktop-secret-token user=friend@example.com\n"
        f"state={state}\n",
        encoding="utf-8",
    )

    with _client(server) as client:
        response = client.post(
            "/api/admin/support-bundle",
            headers={"X-Admin-Key": "known-desktop-secret-token"},
        )
        assert response.status_code == 200, response.text
        archive = Path(response.json()["path"])

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        combined = "\n".join(
            bundle.read(name).decode("utf-8", errors="replace") for name in names
        )

    assert "diagnostics.json" in names
    assert "inventory.json" in names
    assert any(name.startswith("log-tails/") for name in names)
    assert all("Cookies" not in name for name in names)
    assert all("tasks.db" not in name and "pool_usage.db" not in name for name in names)
    for private in (
        "known-desktop-secret-token",
        "known-client-secret-key",
        "known-admin-secret-key",
        "profile-cookie-secret",
        "private-media-secret",
        "friend@example.com",
        str(state),
    ):
        assert private not in combined
    assert "server ready" in combined


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_desktop_state_and_database_permissions_are_owner_only(monkeypatch, tmp_path):
    server, state = _load_desktop(monkeypatch, tmp_path)
    with _client(server):
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        for directory in ("accounts", "downloads", "logs", "support"):
            assert stat.S_IMODE((state / directory).stat().st_mode) == 0o700
        assert stat.S_IMODE(Path(server.config.DB_PATH).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(server.config.POOL_DB_PATH).stat().st_mode) == 0o600


def test_desktop_shutdown_closes_both_sqlite_connections(monkeypatch, tmp_path):
    server, _state = _load_desktop(monkeypatch, tmp_path)
    task_connection = server.store._conn
    pool_connection = server.pool._conn

    with _client(server):
        assert task_connection.execute("SELECT 1").fetchone()[0] == 1
        assert pool_connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        task_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        pool_connection.execute("SELECT 1")


def test_desktop_refuses_non_loopback_bind(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOLA_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("DOLA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DOLA_DESKTOP_TOKEN", "desktop-owner-token")
    monkeypatch.setenv("DOLA_HOST", "0.0.0.0")

    import config

    with pytest.raises(ValueError, match="loopback"):
        importlib.reload(config)


def test_desktop_ui_uses_launch_capability_without_persistent_browser_storage():
    source = (Path(__file__).resolve().parents[1] / "web" / "playground.html").read_text(
        encoding="utf-8"
    )
    assert "desktop_token" in source
    assert "window.name='dola-desktop:'" in source
    assert "history.replaceState" in source
    assert "let adminKey=desktopToken" in source
    assert "desktopToken||$('apiKey').value.trim()" in source
    assert "sessionSet(KEY_STORAGE,desktopToken)" not in source
    assert "localStorage" not in source
    assert "/api/app/support-bundle" in source
