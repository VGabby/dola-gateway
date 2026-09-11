"""Dola text-to-image worker backed by an isolated persistent browser profile."""

from __future__ import annotations

import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from PIL import Image
from patchright.async_api import async_playwright

import config
from browser import cookie_value, launch_account_context
from upstream_errors import (
    DolaTemporarilyUnavailableError,
    VerificationRequiredError,
    error_from_upstream_text,
)


class ImageGenerationError(RuntimeError):
    """Dola did not return a usable generated image."""


class LoginExpiredError(ImageGenerationError):
    """The selected account no longer has an authenticated Dola session."""


ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

IMAGE_MODE_LABELS = ("画像を作成", "Create image", "生成图片", "生成圖片")
RATIO_MENU_LABELS = ("比率", "Ratio", "比例")
STYLE_MENU_LABELS = ("スタイル", "Style", "风格", "風格")
STYLE_LABELS = {
    "portrait": "ポートレート",
    "landscape": "風景",
    "anime": "アニメ",
    "3d": "3D",
    "cyberpunk": "サイバーパンク",
    "oil painting": "油絵",
    "watercolor": "水彩",
    "flat illustration": "フラットイラスト",
    "kids drawing": "子どものお絵かき",
    "pixel": "ピクセル",
    "colored pencil": "色鉛筆",
    "ink wash": "水墨画",
    "ink print": "インク印刷",
}


def build_cookie_header(cookies: list[dict]) -> str:
    """Build a deterministic Cookie header without logging credential values."""
    values = {
        str(cookie.get("name", "")): str(cookie.get("value", ""))
        for cookie in cookies
        if cookie.get("name") and cookie.get("value")
    }
    return "; ".join(f"{name}={values[name]}" for name in sorted(values))


def localized_style_label(style: str) -> str | None:
    """Translate supported API style names to the current Japanese Dola UI."""
    return STYLE_LABELS.get(style.strip().lower())


def select_generated_images(candidates: list[dict], baseline: set[str]) -> list[str]:
    """Choose every new full-size generated image and ignore existing artwork."""
    eligible = []
    seen = set()
    for item in candidates:
        src = item.get("src", "")
        if (
            src.startswith(("http://", "https://"))
            and "/rc_gen_image/" in src
            and src not in baseline
            and src not in seen
            and int(item.get("width") or 0) >= 512
            and int(item.get("height") or 0) >= 512
        ):
            eligible.append(src)
            seen.add(src)
    return eligible


def select_generated_image(candidates: list[dict], baseline: set[str]) -> str:
    """Backward-compatible helper returning the largest new generated image."""
    selected = set(select_generated_images(candidates, baseline))
    eligible = [
        item for item in candidates
        if item.get("src") in selected
    ]
    if not eligible:
        return ""
    return max(
        eligible,
        key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0),
    )["src"]


async def _click_visible_text(page, labels, *, exact=True):
    for label in labels:
        locator = page.get_by_text(label, exact=exact)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                await candidate.click(timeout=5000)
                return
    raise ImageGenerationError(f"Dola control not found: {labels[0]}")


async def _page_images(page) -> list[dict]:
    return await page.locator("img").evaluate_all(
        """images => images.map(image => ({
            src: image.currentSrc || image.src || '',
            width: image.naturalWidth || image.width || 0,
            height: image.naturalHeight || image.height || 0
        }))"""
    )


async def _wait_for_generated_images(
    page, baseline: set[str], baseline_text: set[str] | None = None
) -> list[str]:
    deadline = time.monotonic() + config.IMAGE_TIMEOUT
    challenge_announced = False
    latest = []
    changed_at = None
    baseline_text = baseline_text or set()
    while time.monotonic() < deadline:
        try:
            current_lines = {
                line.strip()
                for line in (await page.locator("body").inner_text(timeout=1000)).splitlines()
                if line.strip()
            }
        except Exception:
            current_lines = set()
        for line in current_lines - baseline_text:
            classified = error_from_upstream_text(line, pre_generation=True)
            if not isinstance(classified, DolaTemporarilyUnavailableError):
                raise classified
        challenge_visible = any(
            token in (frame.url or "").lower()
            for frame in page.frames[1:]
            for token in ("captcha", "verify", "challenge")
        )
        if challenge_visible and config.IMAGE_HEADLESS:
            raise VerificationRequiredError(
                "Dola requested verification; retry with DOLA_IMAGE_HEADLESS=0"
            )
        if challenge_visible and not challenge_announced:
            print(
                "Dola verification is waiting in the Chromium window; "
                "complete it manually to continue",
                flush=True,
            )
            challenge_announced = True
        image_urls = select_generated_images(await _page_images(page), baseline)
        if image_urls and image_urls != latest:
            latest = image_urls
            changed_at = time.monotonic()
        if latest and changed_at is not None:
            if time.monotonic() - changed_at >= config.IMAGE_SETTLE_SECONDS:
                return latest
        await page.wait_for_timeout(1000)
    if latest:
        return latest
    raise TimeoutError("Dola did not render an image before the configured timeout")


async def download_generated_image(url: str, account: str) -> dict:
    """Download, size-limit, and decode-check a generated image."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ImageGenerationError("Dola returned an invalid image URL")

    timeout = aiohttp.ClientTimeout(total=config.IMAGE_DOWNLOAD_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.get(url, proxy=config.PROXY or None) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > config.IMAGE_MAX_BYTES:
                raise ImageGenerationError("Generated image exceeds configured size limit")
            chunks = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > config.IMAGE_MAX_BYTES:
                    raise ImageGenerationError("Generated image exceeds configured size limit")
                chunks.append(chunk)

    data = b"".join(chunks)
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
    except Exception as exc:
        raise ImageGenerationError("Dola response is not a valid image") from exc

    extension = ALLOWED_FORMATS.get(image_format)
    if not extension:
        raise ImageGenerationError(f"Unsupported generated image format: {image_format}")

    output_dir = Path(config.IMAGE_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{account}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        f"{extension}"
    )
    output_path = output_dir / filename
    output_path.write_bytes(data)
    return {
        "local_path": str(output_path),
        "source_url": url,
        "width": width,
        "height": height,
        "format": image_format.lower(),
        "account": account,
    }


async def generate_image(
    account: str,
    prompt: str,
    ratio: str = "1:1",
    style: str = "auto",
) -> dict:
    """Generate one image through Dola's authenticated image-creation UI."""
    async with async_playwright() as playwright:
        context = await launch_account_context(
            playwright, account, headless=config.IMAGE_HEADLESS
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                "https://www.dola.com/chat",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            if not cookie_value(cookies, "sessionid"):
                raise LoginExpiredError(f"{account} Dola session has expired")

            await _click_visible_text(page, IMAGE_MODE_LABELS)
            await page.wait_for_timeout(1000)

            if ratio:
                await _click_visible_text(page, RATIO_MENU_LABELS)
                await page.wait_for_timeout(300)
                ratio_option = page.get_by_text(
                    re.compile(rf"^{re.escape(ratio)}(?:$|\\s|（)")
                )
                clicked = False
                for index in range(await ratio_option.count()):
                    candidate = ratio_option.nth(index)
                    if await candidate.is_visible():
                        await candidate.click(timeout=5000)
                        clicked = True
                        break
                if not clicked:
                    raise ImageGenerationError(f"Dola ratio option is unavailable: {ratio}")

            style_label = localized_style_label(style)
            if style_label:
                await _click_visible_text(page, STYLE_MENU_LABELS)
                await page.wait_for_timeout(300)
                await _click_visible_text(page, (style_label,))

            baseline = {item["src"] for item in await _page_images(page) if item["src"]}
            try:
                baseline_text = {
                    line.strip()
                    for line in (await page.locator("body").inner_text()).splitlines()
                    if line.strip()
                }
            except Exception:
                baseline_text = set()
            editor = page.locator('[contenteditable="true"][role="textbox"]').last
            if not await editor.count() or not await editor.is_visible():
                raise ImageGenerationError("Dola image prompt editor was not found")
            await editor.fill(prompt)
            await editor.press("Enter")
            image_urls = await _wait_for_generated_images(
                page, baseline, baseline_text
            )
            images = [
                await download_generated_image(image_url, account)
                for image_url in image_urls
            ]
            return {**images[0], "images": images}
        finally:
            await context.close()
