"""Offline browser smoke check used by Windows setup and diagnostics."""

from __future__ import annotations

import asyncio

from patchright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<title>dola-browser-check</title><h1>ok</h1>")
            if await page.title() != "dola-browser-check":
                raise RuntimeError("Chromium returned an unexpected page title")
        finally:
            await browser.close()
    print("Patchright Chromium offline check passed")


if __name__ == "__main__":
    asyncio.run(main())
