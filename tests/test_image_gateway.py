"""Offline tests for Dola image generation and gateway orchestration."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from browser_pool import BrowserPool
from dola_client import DolaClient
from image_worker import (
    build_cookie_header,
    localized_style_label,
    select_generated_image,
    select_generated_images,
)
from store import TaskStore


def test_cookie_header_is_deterministic_and_skips_empty_values():
    cookies = [
        {"name": "sessionid", "value": "session"},
        {"name": "empty", "value": ""},
        {"name": "msToken", "value": "token"},
    ]
    assert build_cookie_header(cookies) == "msToken=token; sessionid=session"


def test_existing_dola_image_payload_uses_image_ability():
    client = DolaClient("sessionid=test")
    body = client._build_image_body(
        "smoke-test lighthouse", "16:9", "watercolor"
    )
    assert body["chat_ability"]["ability_type"] == 16
    text = body["messages"][0]["content_block"][0]["content"]["text_block"]["text"]
    assert "smoke-test lighthouse" in text
    assert "16:9" in text
    assert "watercolor" in text


def test_current_ui_style_mapping_and_generated_image_selection():
    assert localized_style_label("watercolor") == "水彩"
    assert localized_style_label("minimal") is None
    baseline = {"https://cdn.example/avatar.png"}
    candidates = [
        {"src": "https://cdn.example/avatar.png", "width": 1024, "height": 1024},
        {"src": "https://cdn.example/icon.png", "width": 64, "height": 64},
        {
            "src": "https://cdn.example/rc_gen_image/result.png",
            "width": 1024,
            "height": 1024,
        },
    ]
    assert select_generated_image(candidates, baseline).endswith("/result.png")
    assert select_generated_images(candidates, baseline) == [
        "https://cdn.example/rc_gen_image/result.png"
    ]


def test_generated_image_selection_returns_every_unique_full_size_result():
    candidates = [
        {"src": "https://cdn.example/rc_gen_image/one.png", "width": 1024, "height": 1024},
        {"src": "https://cdn.example/rc_gen_image/two.png", "width": 1536, "height": 1024},
        {"src": "https://cdn.example/rc_gen_image/one.png", "width": 1024, "height": 1024},
        {"src": "https://cdn.example/rc_gen_image/icon.png", "width": 100, "height": 100},
    ]
    assert select_generated_images(candidates, set()) == [
        "https://cdn.example/rc_gen_image/one.png",
        "https://cdn.example/rc_gen_image/two.png",
    ]


def test_application_displays_every_generated_image_result():
    html = (
        Path(__file__).resolve().parents[1] / "web" / "playground.html"
    ).read_text(encoding="utf-8")

    assert "task.media_type==='image'" in html
    assert "task.image_urls||[]" in html
    assert "fetchResultBlob(task.id,index)" in html
    assert "renderGallery(task,items)" in html
    assert "Download" in html


def test_image_scheduler_requires_verified_login(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "accounts" / "bad").mkdir(parents=True)
    (tmp_path / "accounts" / "good").mkdir(parents=True)
    pool = BrowserPool(max_concurrency=2, db_path=str(tmp_path / "pool.db"))

    pool.set_login_status("bad", False)
    pool.set_login_status("good", True)

    accounts = {item["name"]: item for item in pool.list_accounts()}
    assert pool._image_schedulable(accounts["bad"]) is False
    assert pool._image_schedulable(accounts["good"]) is True
    assert pool.available_for_image is True


def test_image_scheduler_waits_for_busy_account(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "accounts" / "only").mkdir(parents=True)
    pool = BrowserPool(max_concurrency=2, db_path=str(tmp_path / "pool.db"))
    pool.set_login_status("only", True)
    calls = []

    async def exercise():
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def fake_generate(account, prompt, ratio, style):
            calls.append(prompt)
            if prompt == "first":
                first_started.set()
                await release_first.wait()
            return {"local_path": str(tmp_path / f"{prompt}.png"), "account": account}

        monkeypatch.setattr("browser_pool.generate_image_for_account", fake_generate)
        first = asyncio.create_task(pool.generate_image("first"))
        await first_started.wait()
        second = asyncio.create_task(pool.generate_image("second"))
        await asyncio.sleep(0.05)
        assert not second.done()
        release_first.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())
    assert calls == ["first", "second"]
    assert pool.used_today("only") == 2


def test_observed_usage_does_not_create_an_assumed_dola_limit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "accounts" / "only").mkdir(parents=True)
    pool = BrowserPool(max_concurrency=1, db_path=str(tmp_path / "pool.db"))
    pool.set_login_status("only", True)

    for _ in range(5):
        pool._claim("only")

    account = pool.list_accounts()[0]
    assert account["used_today"] == 5
    assert account["quota_status"] == "unavailable"
    assert account["limit"] is None
    assert account["remaining"] is None
    assert pool._schedulable(account) is True

    pool._mark_daily_limit("only", "Dola reported a daily limit")
    blocked = pool.list_accounts()[0]
    assert blocked["used_today"] == 5
    assert blocked["quota_status"] == "blocked"
    assert pool._schedulable(blocked) is False
    assert pool.all_accounts_limited is True


def test_admin_stats_reports_dola_quota_as_unavailable(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    (tmp_path / "accounts" / "acc1").mkdir(parents=True)
    server.pool.list_accounts()
    server.pool.set_login_status("acc1", True)
    server.pool._claim("acc1")

    with TestClient(server.app) as client:
        response = client.get("/api/admin/stats")

    assert response.status_code == 200
    stats = response.json()
    assert stats["quota_status"] == "unavailable"
    assert stats["total_remaining"] is None
    assert stats["available_accounts"] == 1
    assert stats["per_account"][0]["used_today"] == 1
    assert stats["per_account"][0]["limit"] is None
    assert stats["per_account"][0]["remaining"] is None


def test_store_separates_image_recovery_from_video(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    store.create("video_1", "seedance-2.0", "video", "1:1", 10)
    store.create(
        "image_1",
        "dola-image",
        "image",
        "3:2",
        0,
        media_type="image",
        style="watercolor",
    )

    assert [row["id"] for row in store.recoverable_queued_tasks()] == ["video_1"]
    images = store.recoverable_image_tasks()
    assert [row["id"] for row in images] == ["image_1"]
    assert images[0]["style"] == "watercolor"


def _load_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOLA_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("DOLA_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("DOLA_IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("DOLA_PUBLIC_BASE", "http://testserver")
    monkeypatch.setenv("DOLA_PROXY", "")
    monkeypatch.setenv("DOLA_API_KEYS", "")
    monkeypatch.setenv("DOLA_ADMIN_KEY", "")

    import config
    import server

    importlib.reload(config)
    return importlib.reload(server)


def test_image_api_completes_with_mocked_worker(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    (tmp_path / "accounts" / "acc2").mkdir(parents=True)
    server.pool.list_accounts()
    server.pool.set_login_status("acc2", True)
    output = tmp_path / "images" / "mock.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"mock")

    async def fake_generate_image(prompt, ratio, style, on_start=None):
        assert prompt == "a test image"
        assert ratio == "1:1"
        assert style == "minimal"
        if on_start:
            on_start("acc2")
        return {"local_path": str(output), "account": "acc2"}

    monkeypatch.setattr(server.pool, "generate_image", fake_generate_image)
    with TestClient(server.app) as client:
        response = client.post(
            "/v1/images/generations",
            json={"prompt": "a test image", "size": "1024x1024", "style": "minimal"},
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]
        for _ in range(50):
            result = client.get(f"/v1/images/{task_id}")
            if result.json()["status"] != "queued":
                break
            time.sleep(0.01)

    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["image_url"] == f"http://testserver/v1/images/{task_id}/content/0"
    assert result.json()["image_urls"] == [result.json()["image_url"]]
    with TestClient(server.app) as client:
        content = client.get(f"/v1/images/{task_id}/content")
    assert content.status_code == 200
    assert content.content == b"mock"


def test_image_api_returns_and_serves_every_generated_image(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    (tmp_path / "accounts" / "acc2").mkdir(parents=True)
    server.pool.list_accounts()
    server.pool.set_login_status("acc2", True)
    outputs = []
    for index, body in enumerate((b"first", b"second")):
        path = tmp_path / "images" / f"result-{index}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        outputs.append(path)

    async def fake_generate_image(prompt, ratio, style, on_start=None):
        if on_start:
            on_start("acc2")
        images = [
            {"local_path": str(path), "account": "acc2"} for path in outputs
        ]
        return {**images[0], "images": images}

    monkeypatch.setattr(server.pool, "generate_image", fake_generate_image)
    with TestClient(server.app) as client:
        response = client.post("/v1/images/generations", json={"prompt": "batch"})
        task_id = response.json()["id"]
        for _ in range(50):
            result = client.get(f"/v1/images/{task_id}")
            if result.json()["status"] == "completed":
                break
            time.sleep(0.01)
        first = client.get(f"/v1/images/{task_id}/content/0")
        second = client.get(f"/v1/images/{task_id}/content/1")
        missing = client.get(f"/v1/images/{task_id}/content/2")

    assert result.json()["image_urls"] == [
        f"http://testserver/v1/images/{task_id}/content/0",
        f"http://testserver/v1/images/{task_id}/content/1",
    ]
    assert first.content == b"first"
    assert second.content == b"second"
    assert missing.status_code == 404


def test_image_request_contract_is_visible_in_openapi(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    schema = server.app.openapi()
    assert "/v1/images/generations" in schema["paths"]
    request = schema["components"]["schemas"]["ImageGenRequest"]
    assert request["additionalProperties"] is False
    assert request["properties"]["n"]["maximum"] == 1


def test_image_submission_is_idempotent(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    (tmp_path / "accounts" / "acc1").mkdir(parents=True)
    server.pool.list_accounts()
    server.pool.set_login_status("acc1", True)
    output = tmp_path / "images" / "same.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"same")
    calls = []

    async def fake_generate_image(prompt, ratio, style, on_start=None):
        calls.append(prompt)
        if on_start:
            on_start("acc1")
        return {"local_path": str(output), "account": "acc1"}

    monkeypatch.setattr(server.pool, "generate_image", fake_generate_image)
    headers = {"Idempotency-Key": "playground-one"}
    with TestClient(server.app) as client:
        first = client.post(
            "/v1/images/generations", json={"prompt": "one"}, headers=headers
        )
        task_id = first.json()["id"]
        for _ in range(50):
            status = client.get(f"/v1/images/{task_id}").json()
            if status["status"] == "completed":
                break
            time.sleep(0.01)
        second = client.post(
            "/v1/images/generations", json={"prompt": "one"}, headers=headers
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == task_id
    assert calls == ["one"]


def test_image_content_is_scoped_to_client_key(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    first_key = server.store.create_key("first")["key"]
    second_key = server.store.create_key("second")["key"]
    output = tmp_path / "images" / "private.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"private")
    server.store.create(
        "image_private",
        "dola-image",
        "private",
        "1:1",
        0,
        api_key_hash=server.store.hash_api_key(first_key),
        media_type="image",
    )
    server.store.update(
        "image_private",
        status="completed",
        local_path=str(output),
        image_url="http://testserver/v1/images/image_private/content",
    )

    with TestClient(server.app) as client:
        allowed = client.get(
            "/v1/images/image_private/content",
            headers={"Authorization": f"Bearer {first_key}"},
        )
        hidden = client.get(
            "/v1/images/image_private/content",
            headers={"Authorization": f"Bearer {second_key}"},
        )

    assert allowed.status_code == 200
    assert allowed.content == b"private"
    assert hidden.status_code == 404


def test_image_history_is_scoped_to_client_key(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    first_key = server.store.create_key("first")["key"]
    second_key = server.store.create_key("second")["key"]
    for task_id, key, prompt in (
        ("image_first", first_key, "first prompt"),
        ("image_second", second_key, "second prompt"),
    ):
        server.store.create(
            task_id,
            "dola-image",
            prompt,
            "1:1",
            0,
            api_key_hash=server.store.hash_api_key(key),
            media_type="image",
            style="auto",
        )

    with TestClient(server.app) as client:
        first = client.get(
            "/v1/images?limit=20",
            headers={"Authorization": f"Bearer {first_key}"},
        )
        second = client.get(
            "/v1/images?limit=20",
            headers={"Authorization": f"Bearer {second_key}"},
        )

    assert [task["id"] for task in first.json()] == ["image_first"]
    assert [task["id"] for task in second.json()] == ["image_second"]
    assert first.json()[0]["prompt"] == "first prompt"
    assert first.json()[0]["ratio"] == "1:1"
    assert first.json()[0]["style"] == "auto"


def test_video_api_history_content_and_idempotency(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    (tmp_path / "accounts" / "acc1").mkdir(parents=True)
    server.pool.list_accounts()
    output = tmp_path / "downloads" / "result.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"mock-video")
    calls = []

    async def fake_generate_video(
        prompt,
        ratio,
        duration,
        model,
        on_conversation_id=None,
        on_poll=None,
        reference_image_paths=None,
    ):
        calls.append((prompt, ratio, duration, model, reference_image_paths))
        return {"local_path": str(output), "account": "acc1"}

    monkeypatch.setattr(server.pool, "generate_video", fake_generate_video)
    headers = {"Idempotency-Key": "playground-video-one"}
    body = {
        "prompt": "a test video",
        "ratio": "16:9",
        "duration": 10,
        "model": "seedance-2.0",
    }
    with TestClient(server.app) as client:
        first = client.post("/v1/videos/generations", json=body, headers=headers)
        assert first.status_code == 200, first.text
        task_id = first.json()["id"]
        for _ in range(50):
            result = client.get(f"/v1/videos/{task_id}")
            if result.json()["status"] == "completed":
                break
            time.sleep(0.01)
        history = client.get("/v1/videos?limit=20")
        content = client.get(f"/v1/videos/{task_id}/content")
        repeated = client.post("/v1/videos/generations", json=body, headers=headers)

    assert result.json()["status"] == "completed"
    assert result.json()["ratio"] == "16:9"
    assert result.json()["duration"] == 10
    assert result.json()["created_at"]
    assert [task["id"] for task in history.json()] == [task_id]
    assert content.content == b"mock-video"
    assert repeated.json()["id"] == task_id
    assert calls == [("a test video", "16:9", 10, "seedance-2.0", [])]


def test_playground_uses_opt_in_tab_scoped_key_storage(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path)
    with TestClient(server.app) as client:
        response = client.get("/playground")
    assert response.status_code == 200
    assert "Dola Gateway" in response.text
    assert "Idempotency-Key" in response.text
    assert "/v1/images/" in response.text
    assert "/v1/videos/" in response.text
    assert "videoMode" in response.text
    assert "What will you create?" in response.text
    assert "Your creations" in response.text
    assert 'data-page="create"' in response.text
    assert 'data-page="creations"' in response.text
    assert 'data-page="accounts"' in response.text
    assert 'data-page="activity"' in response.text
    assert 'data-page="settings"' in response.text
    assert "Library" in response.text
    assert "Settings &amp; Help" in response.text
    assert 'data-filter="image"' in response.text
    assert 'data-filter="video"' in response.text
    assert 'data-filter="running"' in response.text
    assert "Advanced options" in response.text
    assert "Prompt starters" in response.text
    assert "max-height:calc(100vh - 190px)" in response.text
    assert "overflow-y:auto" in response.text
    assert "overflow-x:hidden" in response.text
    assert "creation-prompt" in response.text
    assert "STATUS_LABELS" in response.text
    assert "shortRunId" in response.text
    assert "shortError" in response.text
    assert "status-badge" in response.text
    assert "-webkit-line-clamp:2" in response.text
    assert "selectTask" in response.text
    assert "loadHistory" in response.text
    assert "Promise.allSettled" in response.text
    assert "loadPoolStatus" in response.text
    assert "fetch('/health',{headers:authHeaders()})" in response.text
    assert "submissionLocked=true" in response.text
    assert "if(submissionLocked)return" in response.text
    assert "Remember for this tab" in response.text
    assert "Clear key" in response.text
    assert "sessionStorage" in response.text
    assert "restoreRememberedKey" in response.text
    assert "localStorage" not in response.text
    assert "X-Admin-Key" in response.text
    assert "/api/admin/accounts" in response.text
    assert "/api/admin/tasks?limit=50" in response.text
    assert "/api/admin/stats" in response.text
    assert "function openRemoveAccount" in response.text
    assert "data-remove-error" in response.text
    assert "if(!confirm(`Remove account" not in response.text


def test_playground_requires_create_another_before_a_second_submission():
    html = (Path(__file__).resolve().parents[1] / "web" / "playground.html").read_text()
    posts = []

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

                async def handle(route, request):
                    path = request.url.split("?", 1)[0]
                    if path == "http://playground.test/playground":
                        await route.fulfill(status=200, content_type="text/html", body=html)
                    elif path == "http://playground.test/health":
                        body = {
                            "available": True,
                            "image_available": True,
                            "pending_tasks": 0,
                            "accounts": [
                                {
                                    "login_ok": True,
                                    "rate_limited": False,
                                    "quota_blocked": False,
                                }
                            ],
                        }
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(body),
                        )
                    elif path in {
                        "http://playground.test/v1/images",
                        "http://playground.test/v1/videos",
                    }:
                        await route.fulfill(
                            status=200, content_type="application/json", body="[]"
                        )
                    elif path.endswith("/generations") and request.method == "POST":
                        request_body = json.loads(request.post_data)
                        posts.append(
                            {
                                "url": request.url,
                                "key": request.headers.get("idempotency-key"),
                            }
                        )
                        await asyncio.sleep(0.05)
                        if request_body["prompt"] == "fail request":
                            await route.fulfill(
                                status=503,
                                content_type="application/json",
                                body=json.dumps({"detail": "mock unavailable"}),
                            )
                            return
                        media = "video" if "/videos/" in path else "image"
                        task = {
                            "id": f"{media}_{len(posts):04d}",
                            "status": "queued",
                            "prompt": request_body["prompt"],
                            "created_at": 1_800_000_000 + len(posts),
                        }
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(task),
                        )
                    elif "/v1/images/" in path or "/v1/videos/" in path:
                        media = "video" if "/videos/" in path else "image"
                        task_id = path.rsplit("/", 1)[-1]
                        task = {
                            "id": task_id,
                            "status": "queued",
                            "prompt": "pending",
                            "media_type": media,
                        }
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(task),
                        )
                    else:
                        await route.abort()

                await page.route("**/*", handle)
                await page.goto("http://playground.test/playground")
                await page.locator("#prompt").fill("first request")

                # Both clicks occur in one browser task; the synchronous lock must
                # prevent the second handler from sending another request.
                await page.locator("#generate").evaluate(
                    "button => { button.click(); button.click(); }"
                )
                await page.wait_for_function(
                    "() => document.querySelector('#generate').dataset.action === 'create-another'"
                )
                assert len(posts) == 1
                assert await page.locator("#generate").text_content() == "Create another"

                # A mode switch deliberately preserves the reset gate.
                await page.locator("#videoMode").click()
                assert (
                    await page.locator("#generate").get_attribute("data-action")
                    == "create-another"
                )
                assert await page.locator("#generate").text_content() == "Create another"

                await page.locator("#generate").click()
                assert len(posts) == 1
                assert await page.locator("#prompt").input_value() == ""
                assert await page.locator("#submitNote").text_content() == ""
                assert await page.locator("#historyList .creation").count() == 1
                assert "first request" in await page.locator(
                    "#historyList .creation"
                ).text_content()
                assert (
                    await page.locator("#generate").get_attribute("data-action")
                    == "generate"
                )
                assert (
                    await page.locator("#generate").text_content()
                    == "Generate video →"
                )
                assert await page.locator("#stateText").text_content() == "Ready"
                await page.wait_for_function(
                    "() => document.querySelector('#capacityText').textContent === 'Ready to generate now'"
                )
                assert await page.locator("#prompt").evaluate(
                    "element => element === document.activeElement"
                )

                await page.locator("#prompt").fill("second request")
                await page.locator("#generate").click()
                await page.wait_for_function(
                    "() => document.querySelector('#generate').dataset.action === 'create-another'"
                )
                assert len(posts) == 2
                assert all(item["key"] for item in posts)
                assert posts[0]["key"] != posts[1]["key"]
                assert "/v1/images/generations" in posts[0]["url"]
                assert "/v1/videos/generations" in posts[1]["url"]

                await page.locator("#generate").click()
                await page.locator("#prompt").fill("fail request")
                await page.locator("#generate").click()
                await page.wait_for_function(
                    "() => document.querySelector('#error').textContent === 'mock unavailable'"
                )
                assert len(posts) == 3
                assert (
                    await page.locator("#generate").get_attribute("data-action")
                    == "generate"
                )
                assert (
                    await page.locator("#generate").text_content()
                    == "Generate video →"
                )
            finally:
                await browser.close()

    asyncio.run(check())


def test_unified_owner_shell_authenticates_and_loads_admin_views():
    html = (Path(__file__).resolve().parents[1] / "web" / "playground.html").read_text()
    authorized_requests = []

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

                async def handle(route, request):
                    path = request.url.split("?", 1)[0]
                    if path == "http://playground.test/playground":
                        await route.fulfill(status=200, content_type="text/html", body=html)
                    elif path == "http://playground.test/health":
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "available": True,
                                    "image_available": True,
                                    "pending_tasks": 1,
                                    "accounts": [{"login_ok": True}],
                                }
                            ),
                        )
                    elif path in {
                        "http://playground.test/v1/images",
                        "http://playground.test/v1/videos",
                    }:
                        await route.fulfill(
                            status=200, content_type="application/json", body="[]"
                        )
                    elif path == "http://playground.test/api/admin/login":
                        assert json.loads(request.post_data)["key"] == "owner-secret"
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body='{"ok":true,"auth_required":true}',
                        )
                    elif path.startswith("http://playground.test/api/admin/"):
                        key = request.headers.get("x-admin-key", "")
                        if not key:
                            await route.fulfill(
                                status=401,
                                content_type="application/json",
                                body='{"detail":"wrong admin key"}',
                            )
                            return
                        authorized_requests.append((path, key, request.method))
                        if path.endswith("/accounts/kent") and request.method == "DELETE":
                            body = {"ok": True}
                        elif path.endswith("/accounts"):
                            body = {
                                "accounts": [
                                    {
                                        "name": "kent",
                                        "email": "kent@example.com",
                                        "login_ok": 1,
                                        "scheduling": True,
                                        "used_today": 2,
                                        "last_used_at": 1_800_000_000,
                                    }
                                ]
                            }
                        elif path.endswith("/jobs"):
                            body = {"jobs": {}}
                        elif path.endswith("/tasks"):
                            body = {
                                "tasks": [
                                    {
                                        "id": "video_activity0001",
                                        "prompt": "owner activity prompt",
                                        "media_type": "video",
                                        "duration": 10,
                                        "status": "processing",
                                        "account": "kent",
                                        "api_key_name": "friend",
                                        "created_at": 1_800_000_000,
                                    }
                                ]
                            }
                        elif path.endswith("/stats"):
                            body = {
                                "today_completed": 4,
                                "success_rate": 0.8,
                            }
                        else:
                            await route.abort()
                            return
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(body),
                        )
                    elif path == "http://playground.test/docs":
                        await route.fulfill(status=200, content_type="text/html", body="docs")
                    else:
                        await route.abort()

                await page.route("**/*", handle)
                await page.goto("http://playground.test/playground")

                assert await page.locator("[data-page]").all_text_contents() == [
                    "Create",
                    "Library",
                    "Accounts",
                    "Activity",
                    "Settings & Help",
                ]
                await page.locator('[data-page="accounts"]').click()
                await page.locator("#accountsGate [data-admin-key]").fill(
                    "owner-secret"
                )
                await page.locator("#accountsGate [data-admin-login]").evaluate(
                    "form => form.requestSubmit()"
                )
                await page.wait_for_function(
                    "() => document.querySelector('#accountsContent').hidden === false"
                )
                assert "kent@example.com" in await page.locator(
                    "#accountsTable"
                ).text_content()

                await page.locator('[data-account-delete="kent"]').click()
                await page.wait_for_function(
                    "() => document.querySelector('#ownerDialog').open"
                )
                assert "saved login session cannot be recovered" in (
                    await page.locator("#ownerDialogBody").text_content()
                ).lower()
                assert not any(
                    method == "DELETE" for _, _, method in authorized_requests
                )
                await page.locator("#ownerDialogSubmit").click()
                await page.wait_for_function(
                    "() => !document.querySelector('#ownerDialog').open"
                )
                assert any(
                    path.endswith("/accounts/kent") and method == "DELETE"
                    for path, _, method in authorized_requests
                )

                await page.locator('[data-page="activity"]').click()
                await page.wait_for_function(
                    "() => document.querySelector('#activityTable').textContent.includes('owner activity prompt')"
                )
                assert "owner activity prompt" in await page.locator(
                    "#activityTable"
                ).text_content()
                assert "80%" in await page.locator("#activityMetrics").text_content()
                assert authorized_requests
                assert all(key == "owner-secret" for _, key, _ in authorized_requests)
            finally:
                await browser.close()

    asyncio.run(check())
