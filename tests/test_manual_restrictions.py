"""Offline acceptance tests for persistent, manual-only restriction recovery."""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dola_gateway.browser_pool import BrowserPool
from dola_gateway.store import TaskStore
from dola_gateway.upstream_errors import (
    CONTENT_REJECTION_JA,
    DolaTemporarilyUnavailableError,
    ExplicitRestrictionError,
    PromptContentRejectedError,
    classify_upstream_text,
)


def make_pool(tmp_path, *accounts):
    for account in accounts:
        (tmp_path / "accounts" / account).mkdir(parents=True)
    pool = BrowserPool(
        accounts_dir=str(tmp_path / "accounts"),
        db_path=str(tmp_path / "pool.db"),
        max_concurrency=2,
    )
    for account in accounts:
        pool.set_login_status(account, True)
    return pool


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        (CONTENT_REJECTION_JA, "content_rejected"),
        (f"Insufficient quota: {CONTENT_REJECTION_JA}", "content_rejected"),
        ("Video daily limit reached", "daily_limit"),
        ("Not enough credits available", "credits_unavailable"),
        ("Too many requests; try later", "risk_control"),
        ("生成できません", "temporary"),
        ("Unable to generate this request", "temporary"),
        ("Credits were not used for this request", "temporary"),
    ],
)
def test_upstream_classifier_is_conservative(message, kind):
    assert classify_upstream_text(message).kind == kind


def test_restrictions_are_independent_persistent_and_ignore_legacy_timers(tmp_path):
    pool = make_pool(tmp_path, "acc")
    pool.mark_restriction("acc", "video", "daily_limit")
    before = pool.list_accounts()[0]
    assert before["video_restricted"] is True
    assert before["image_restricted"] is False
    assert pool.available is False
    assert pool.available_for_image is True

    # Arbitrary elapsed time and a new pool instance do not clear the state.
    pool._conn.execute(
        "UPDATE accounts_meta SET video_restricted_detected_at=? WHERE name='acc'",
        (time.time() - 10 * 365 * 24 * 3600,),
    )
    pool._conn.commit()
    restarted = BrowserPool(
        accounts_dir=str(tmp_path / "accounts"),
        db_path=str(tmp_path / "pool.db"),
    )
    after = restarted.list_accounts()[0]
    assert after["video_restricted"] is True
    assert after["video_restriction_reason"] == "daily_limit"


def test_legacy_migration_is_video_only_and_skips_content_or_ambiguity(tmp_path):
    pool = make_pool(tmp_path, "daily", "content", "ambiguous")
    future = time.time() + 86400
    pool._conn.execute(
        "UPDATE accounts_meta SET rate_limited_until=?, limit_reason=? WHERE name='daily'",
        (future, "Dola reported a daily limit"),
    )
    pool._conn.execute(
        "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name='content'",
        (future, f"Insufficient quota: {CONTENT_REJECTION_JA}"),
    )
    pool._conn.execute(
        "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name='ambiguous'",
        (future, "生成できません"),
    )
    pool._conn.commit()

    migrated = BrowserPool(
        accounts_dir=str(tmp_path / "accounts"), db_path=str(tmp_path / "pool.db")
    )
    rows = {row["name"]: row for row in migrated.list_accounts()}
    assert rows["daily"]["video_restriction_reason"] == "daily_limit"
    assert rows["daily"]["image_restricted"] is False
    assert rows["content"]["video_restricted"] is False
    assert rows["ambiguous"]["video_restricted"] is False


def test_content_and_ambiguous_failures_never_restrict_or_rotate(monkeypatch, tmp_path):
    pool = make_pool(tmp_path, "a", "b")
    calls = []

    async def content_failure(account, *args, **kwargs):
        calls.append(account)
        raise RuntimeError(f"Insufficient quota: {CONTENT_REJECTION_JA}")

    monkeypatch.setattr("dola_gateway.browser_pool.generate_video", content_failure)
    with pytest.raises(PromptContentRejectedError):
        asyncio.run(pool.generate_video("prompt"))
    assert calls == ["a"]
    assert not any(row["video_restricted"] for row in pool.list_accounts())

    calls.clear()

    async def ambiguous_failure(account, *args, **kwargs):
        calls.append(account)
        raise RuntimeError("生成できません")

    monkeypatch.setattr("dola_gateway.browser_pool.generate_video", ambiguous_failure)
    with pytest.raises(DolaTemporarilyUnavailableError):
        asyncio.run(pool.generate_video("prompt"))
    assert calls == ["a"]
    assert not any(row["video_restricted"] for row in pool.list_accounts())


def test_normal_explicit_restriction_rotates_but_manual_retry_never_does(
    monkeypatch, tmp_path
):
    pool = make_pool(tmp_path, "a", "b")
    calls = []

    async def first_restricted(account, *args, **kwargs):
        calls.append(account)
        if account == "a":
            raise ExplicitRestrictionError("daily_limit", "Daily limit reached")
        return {"local_path": str(tmp_path / "ok.mp4"), "account": account}

    monkeypatch.setattr("dola_gateway.browser_pool.generate_video", first_restricted)
    result = asyncio.run(pool.generate_video("prompt"))
    assert result["account"] == "b"
    assert calls == ["a", "b"]
    assert pool.list_accounts()[0]["video_restricted"] is True

    calls.clear()
    pool.mark_restriction("a", "video", "daily_limit")
    previous_detection = pool.list_accounts()[0]["video_restricted_detected_at"]

    async def retry_still_restricted(account, *args, **kwargs):
        calls.append(account)
        raise ExplicitRestrictionError("daily_limit", "Daily limit remains")

    monkeypatch.setattr("dola_gateway.browser_pool.generate_video", retry_still_restricted)
    with pytest.raises(ExplicitRestrictionError):
        asyncio.run(pool.generate_video("real prompt", retry_account="a"))
    assert calls == ["a"]
    retry_state = pool.list_accounts()[0]
    assert retry_state["video_restricted"] is True
    assert retry_state["video_restricted_detected_at"] >= previous_detection


def test_manual_retry_success_clears_only_matching_media(monkeypatch, tmp_path):
    pool = make_pool(tmp_path, "a")
    pool.mark_restriction("a", "video", "credits_unavailable")
    pool.mark_restriction("a", "image", "risk_control")

    async def success(account, *args, **kwargs):
        return {"local_path": str(tmp_path / "ok.mp4"), "account": account}

    monkeypatch.setattr("dola_gateway.browser_pool.generate_video", success)
    asyncio.run(pool.generate_video("real prompt", retry_account="a"))
    account = pool.list_accounts()[0]
    assert account["video_restricted"] is False
    assert account["image_restricted"] is True


def test_manual_image_retry_rejects_busy_race_without_waiting_or_contact(
    monkeypatch, tmp_path
):
    pool = make_pool(tmp_path, "a")
    pool.mark_restriction("a", "image", "daily_limit")
    calls = []

    async def forbidden_contact(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Dola must not be contacted")

    monkeypatch.setattr("dola_gateway.browser_pool.generate_image_for_account", forbidden_contact)

    async def race():
        # Endpoint-time prevalidation succeeds, then another task takes the lock
        # before the background manual retry starts.
        pool.validate_manual_retry("a", "image")
        lock = pool._locks["a"]
        await lock.acquire()
        try:
            with pytest.raises(RuntimeError, match="Account is busy"):
                await asyncio.wait_for(
                    pool.generate_image("real prompt", retry_account="a"),
                    timeout=0.2,
                )
        finally:
            lock.release()

    asyncio.run(race())
    assert calls == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"Insufficient quota: {CONTENT_REJECTION_JA}"),
        RuntimeError("生成できません"),
    ],
)
def test_manual_content_or_ambiguous_failure_preserves_restriction(
    monkeypatch, tmp_path, error
):
    pool = make_pool(tmp_path, "a")
    pool.mark_restriction("a", "video", "credits_unavailable")
    detected = pool.list_accounts()[0]["video_restricted_detected_at"]

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("dola_gateway.browser_pool.generate_video", fail)
    with pytest.raises((PromptContentRejectedError, DolaTemporarilyUnavailableError)):
        asyncio.run(pool.generate_video("different real prompt", retry_account="a"))
    account = pool.list_accounts()[0]
    assert account["video_restriction_reason"] == "credits_unavailable"
    assert account["video_restricted_detected_at"] == detected


def test_interrupted_manual_retry_is_failed_and_never_recovered(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    store.create(
        "video_retry", "seedance-2.0", "real prompt", "16:9", 10,
        retry_account="acc",
    )
    store.create("video_normal", "seedance-2.0", "normal", "16:9", 10)
    assert [row["id"] for row in store.recoverable_queued_tasks()] == ["video_normal"]
    store.fail_interrupted_manual_retries()
    row = store.get("video_retry")
    assert row["status"] == "failed"
    assert row["failure_code"] == "manual_retry_interrupted"


def load_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOLA_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("DOLA_POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setenv("DOLA_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("DOLA_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("DOLA_IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("DOLA_PUBLIC_BASE", "http://testserver")
    monkeypatch.setenv("DOLA_PROXY", "")
    monkeypatch.setenv("DOLA_API_KEYS", "")
    monkeypatch.setenv("DOLA_ADMIN_KEY", "owner-secret")
    from dola_gateway import config, server
    importlib.reload(config)
    return importlib.reload(server)


def test_retry_account_requires_admin_and_dispatches_persisted_pin_once(
    monkeypatch, tmp_path
):
    server = load_server(monkeypatch, tmp_path)
    (tmp_path / "accounts" / "a").mkdir(parents=True)
    server.pool.set_login_status("a", True)
    server.pool.mark_restriction("a", "video", "daily_limit")
    calls = []
    output = tmp_path / "downloads" / "ok.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"video")

    async def fake_generate(*args, **kwargs):
        calls.append(kwargs.get("retry_account"))
        return {"local_path": str(output), "account": "a"}

    monkeypatch.setattr(server.pool, "generate_video", fake_generate)
    body = {
        "prompt": "real prompt", "duration": 10,
        "model": "seedance-2.0", "retry_account": "a",
    }
    idem = {"Idempotency-Key": "manual-one"}
    with TestClient(server.app) as client:
        denied = client.post("/v1/videos/generations", json=body, headers=idem)
        headers = {**idem, "X-Admin-Key": "owner-secret"}
        accepted = client.post("/v1/videos/generations", json=body, headers=headers)
        task_id = accepted.json()["id"]
        for _ in range(100):
            row = server.store.get(task_id)
            if row["status"] == "completed":
                break
            time.sleep(0.01)
        repeated_without_admin = client.post(
            "/v1/videos/generations", json=body, headers=idem
        )
        repeated = client.post("/v1/videos/generations", json=body, headers=headers)

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert repeated_without_admin.status_code == 401
    assert repeated.json()["id"] == task_id
    assert calls == ["a"]
    assert server.store.get(task_id)["retry_account"] == "a"


def test_existing_requests_without_retry_account_remain_compatible(monkeypatch, tmp_path):
    server = load_server(monkeypatch, tmp_path)
    (tmp_path / "accounts" / "a").mkdir(parents=True)
    server.pool.set_login_status("a", True)
    output = tmp_path / "images" / "ok.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"image")

    async def fake_generate(prompt, ratio, style, on_start=None):
        if on_start:
            on_start("a")
        return {"local_path": str(output), "account": "a"}

    monkeypatch.setattr(server.pool, "generate_image", fake_generate)
    with TestClient(server.app) as client:
        response = client.post("/v1/images/generations", json={"prompt": "normal"})
    assert response.status_code == 200


def test_accounts_ui_pins_and_confirms_one_real_manual_retry():
    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "dola_gateway"
        / "web"
        / "playground.html"
    ).read_text()
    posts = []

    async def check():
        from patchright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def route_request(route, request):
                    path = request.url.split("?", 1)[0]
                    if path == "http://retry.test/playground":
                        await route.fulfill(status=200, content_type="text/html", body=html)
                    elif path == "http://retry.test/health":
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body='{"available":false,"image_available":true,"pending_tasks":0,"accounts":[]}',
                        )
                    elif path in {"http://retry.test/v1/images", "http://retry.test/v1/videos"}:
                        await route.fulfill(status=200, content_type="application/json", body="[]")
                    elif path == "http://retry.test/api/admin/accounts":
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=(
                                '{"accounts":[{"name":"kent","email":"kent@example.com",'
                                '"scheduling":true,"login_ok":true,"busy":false,"used_today":0,'
                                '"video_restricted":true,"video_restriction_reason":"daily_limit",'
                                '"video_restricted_detected_at":1800000000,'
                                '"image_restricted":false,"image_restriction_reason":"",'
                                '"image_restricted_detected_at":0}]}'
                            ),
                        )
                    elif path == "http://retry.test/api/admin/jobs":
                        await route.fulfill(status=200, content_type="application/json", body='{"jobs":{}}')
                    elif path.endswith("/v1/videos/generations"):
                        posts.append({
                            "body": json.loads(request.post_data),
                            "admin": request.headers.get("x-admin-key"),
                        })
                        await route.fulfill(
                            status=200, content_type="application/json",
                            body='{"id":"video_retry","status":"queued","prompt":"real prompt"}',
                        )
                    elif path.endswith("/v1/videos/video_retry"):
                        await route.fulfill(
                            status=200, content_type="application/json",
                            body='{"id":"video_retry","status":"queued","prompt":"real prompt"}',
                        )
                    else:
                        await route.abort()

                await page.route("**/*", route_request)
                await page.goto("http://retry.test/playground")
                await page.locator('[data-page="accounts"]').click()
                await page.wait_for_selector('[data-account-retry="kent"]')
                account_text = await page.locator("#accountsTable").text_content()
                assert "Daily limit reported by Dola" in account_text
                assert "Available" in account_text
                await page.locator('[data-account-retry="kent"][data-retry-media="video"]').click()
                assert await page.locator("#videoMode").get_attribute("aria-pressed") == "true"
                assert "pinned to kent" in await page.locator("#submitNote").text_content()
                await page.locator("#prompt").fill("real prompt")
                page.once("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
                await page.locator("#generate").click()
                await page.wait_for_function("() => window.document.querySelector('#generate').dataset.action === 'create-another'")
                assert posts == [{"body": {
                    "model": "seedance-2.0", "prompt": "real prompt", "ratio": "1:1",
                    "duration": 10, "reference_images": [], "retry_account": "kent",
                }, "admin": None}]
            finally:
                await browser.close()

    asyncio.run(check())
