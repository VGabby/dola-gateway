"""Offline-first smoke checks. No Dola login or generation is performed."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def test_all_python_sources_compile():
    ignored = {
        ".venv",
        ".venv-moved-backup",
        "accounts",
        "dist",
        "downloads",
        "node_modules",
        "target",
    }
    sources = [p for p in ROOT.rglob("*.py") if not ignored.intersection(p.parts)]
    for source in sources:
        compile(source.read_text(encoding="utf-8"), str(source), "exec")


def test_config_defaults_to_local_bind(monkeypatch):
    monkeypatch.delenv("DOLA_HOST", raising=False)
    source = (ROOT / "src" / "dola_gateway" / "config.py").read_text(encoding="utf-8")
    assert 'os.getenv("DOLA_HOST", "127.0.0.1")' in source
    assert 'os.getenv("DOLA_VIDEO_TIMEOUT", "900")' in source


def test_browser_override_exists_when_configured():
    configured = os.getenv("DOLA_BROWSER_EXECUTABLE", "")
    if configured:
        assert Path(configured).is_file(), f"browser not found: {configured}"


def test_video_generation_waits_for_manual_verification():
    source = (ROOT / "src" / "dola_gateway" / "video_worker_ui.py").read_text(encoding="utf-8")
    active_flow = source[source.index("async def generate_video(") :]
    assert "complete it manually to continue" in active_flow
    assert "await solve_slider" not in active_flow


def test_video_default_duration_does_not_leave_menu_open():
    source = (ROOT / "src" / "dola_gateway" / "video_worker_ui.py").read_text(encoding="utf-8")
    active_flow = source[source.index("async def generate_video(") :]
    assert 'if (await current.inner_text()).strip() != duration_label:' in active_flow
    assert 'page.get_by_role("menuitem", name=duration_label, exact=True)' in active_flow
    assert 'await page.keyboard.press("Escape")' in active_flow


def test_video_duration_clarification_fails_fast():
    from dola_gateway.video_worker_ui import VIDEO_NOT_STARTED_PATTERN, _submission_prompt

    message = (
        "Video generation currently supports durations from 4 to 15 seconds. "
        "I can generate it at the nearest supported duration of 15 seconds."
    )
    assert VIDEO_NOT_STARTED_PATTERN.search(message)
    assert _submission_prompt("a dragon flying", 10) == (
        "Create a 10-second video: a dragon flying"
    )
    assert _submission_prompt("a dragon flying", 30) == "a dragon flying"

    source = (ROOT / "src" / "dola_gateway" / "video_worker_ui.py").read_text(encoding="utf-8")
    active_flow = source[source.index("async def generate_video(") :]
    assert "Duration selection did not stick" in active_flow
    assert "Failed to set requested duration" in active_flow


def test_video_control_lookup_waits_for_delayed_visible_match():
    from dola_gateway.video_worker_ui import _click_first_visible

    class Candidate:
        clicked = False

        async def is_visible(self):
            return True

        async def click(self, timeout):
            self.clicked = True

    class DelayedLocator:
        def __init__(self):
            self.count_calls = 0
            self.candidate = Candidate()

        async def count(self):
            self.count_calls += 1
            return 0 if self.count_calls < 3 else 1

        def nth(self, index):
            assert index == 0
            return self.candidate

    async def check():
        locator = DelayedLocator()
        result = await _click_first_visible(locator, "model button", timeout=1000)
        assert result is locator.candidate
        assert locator.candidate.clicked is True
        assert locator.count_calls >= 3

    asyncio.run(check())


def test_bundled_browser_can_launch_offline():
    async def check():
        from patchright.async_api import async_playwright

        async with async_playwright() as playwright:
            kwargs = {"headless": True}
            configured = os.getenv("DOLA_BROWSER_EXECUTABLE", "")
            if configured:
                kwargs["executable_path"] = configured
            browser = await playwright.chromium.launch(**kwargs)
            try:
                page = await browser.new_page()
                await page.set_content("<title>dola-smoke</title><h1>ok</h1>")
                assert await page.title() == "dola-smoke"
                assert await page.locator("h1").text_content() == "ok"
            finally:
                await browser.close()

    asyncio.run(check())


def test_health_endpoint_works_without_accounts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOLA_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("DOLA_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("DOLA_PROXY", "")

    import importlib
    from dola_gateway import config, server

    importlib.reload(config)
    server = importlib.reload(server)
    with TestClient(server.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["accounts"] == []
