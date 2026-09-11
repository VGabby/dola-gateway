"""Offline Patchright Chromium launch check."""

import asyncio

from patchright.async_api import async_playwright


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<title>dola-browser-check</title><h1>ok</h1>")
            if await page.title() != "dola-browser-check":
                raise RuntimeError("unexpected browser result")
        finally:
            await browser.close()
    print("[OK] Patchright Chromium offline check passed")


if __name__ == "__main__":
    asyncio.run(main())
