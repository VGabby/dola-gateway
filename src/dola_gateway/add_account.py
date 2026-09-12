"""Manual Google sign-in for an isolated Dola browser profile.

The user enters Google credentials and 2FA directly in the browser. This module
never accepts, logs, or stores a Google password or TOTP secret.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from patchright.async_api import async_playwright

from .browser import LAUNCH_ARGS
from . import config


async def has_dola_session(context) -> bool:
    """Return whether the browser context contains an active-looking Dola session."""
    cookies = await context.cookies("https://www.dola.com")
    return any(
        cookie.get("name") == "sessionid" and cookie.get("value")
        for cookie in cookies
    )


async def _click_first_visible(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


async def _open_google_sign_in(page) -> None:
    """Best-effort navigation to Google sign-in; the user can also click manually."""
    await _click_first_visible(page, (
        "text=ログイン",
        "text=Log in",
        "text=Sign in",
        "text=登录",
        "text=登入",
        "text=로그인",
    ))
    await page.wait_for_timeout(1000)
    await _click_first_visible(page, (
        "text=Googleで続ける",
        "text=Continue with Google",
        "text=Sign in with Google",
        "text=使用 Google 继续",
        "text=使用 Google 登入",
        "text=Google 계정으로 계속",
    ))


async def add_account_flow(account: str) -> bool:
    """Open a persistent profile and wait for the user to complete Google sign-in."""
    profile_dir = Path(config.ACCOUNTS_DIR) / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        kwargs = {
            "headless": False,
            "args": LAUNCH_ARGS,
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
        }
        if config.BROWSER_EXECUTABLE:
            executable = Path(config.BROWSER_EXECUTABLE).expanduser()
            if not executable.is_file():
                raise FileNotFoundError(f"Configured browser does not exist: {executable}")
            kwargs["executable_path"] = str(executable)
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}

        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir), **kwargs
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                "https://www.dola.com/chat",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            await page.bring_to_front()

            if await has_dola_session(context):
                print(f"[{account}] Active Dola session already exists", flush=True)
                return True

            await _open_google_sign_in(page)
            print(
                f"[{account}] Complete Google sign-in in the browser window. "
                "Credentials and 2FA stay in Google's page.",
                flush=True,
            )

            deadline = time.monotonic() + config.LOGIN_TIMEOUT
            while time.monotonic() < deadline:
                if await has_dola_session(context):
                    print(
                        f"[{account}] Login successful; session saved in {profile_dir}",
                        flush=True,
                    )
                    await page.wait_for_timeout(1000)
                    return True
                await asyncio.sleep(2)

            try:
                await page.screenshot(path=f"dbg_add_account_{account}.png")
            except Exception:
                pass
            raise TimeoutError(
                f"Dola login was not detected within {config.LOGIN_TIMEOUT} seconds"
            )
        finally:
            await context.close()
