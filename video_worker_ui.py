"""Video generation worker v2: UI automation with OpenCV slider puzzle solver."""
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

import aiohttp
from patchright.async_api import async_playwright

from gap import find_gap_x

import config
from browser import cookie_value, launch_account_context
from dola_client import CreditError
from video_worker import POLL_JS, RiskControlError, _download, extract_unwatermarked_url
from upstream_errors import (
    DolaTemporarilyUnavailableError,
    VerificationRequiredError,
    error_from_upstream_text,
)

# Daily limit pattern matching response text
DAILY_LIMIT_PATTERN = re.compile(
    r"動画生成の\s*1日あたりの上限|每日(?:视频|影片)?生成.*(?:上限|限额|额度)|"
    r"daily.*(?:limit|quota)|(?:limit|quota).*per\s*day",
    re.IGNORECASE,
)
VIDEO_NOT_STARTED_PATTERN = re.compile(
    r"supports?\s+(?:video\s+generation\s+)?durations?\s+from|"
    r"nearest\s+supported\s+duration|"
    r"動画生成.{0,80}(?:対応|サポート).{0,40}秒|"
    r"(?:生成|作成).{0,60}(?:最も近い|最寄り).{0,40}秒",
    re.IGNORECASE,
)


class AccountLimitedError(Exception):
    """Account reached daily video generation limit."""


VIDEO_BTN = "text=動画を作成"          # Entry point button in ja-JP locale
CAPTCHA_FRAME_KEY = "bdcaptcha.html"   # Captcha verifycenter iframe


async def _first_visible(locator, description: str, timeout: int = 5000):
    """Waits for the first visible match, including dynamically mounted controls."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(0.1, remaining))
    raise RuntimeError(f"Visible {description} not found within {timeout}ms")


async def _click_first_visible(locator, description: str, timeout: int = 5000):
    """Waits for and clicks the first visible match instead of a hidden duplicate."""
    candidate = await _first_visible(locator, description, timeout)
    await candidate.click(timeout=timeout)
    return candidate


async def _dismiss_cookie_notice(page) -> bool:
    """Dismisses Dola's optional cookie notice when it covers the composer."""
    try:
        body_text = await page.locator("body").inner_text(timeout=1000)
    except Exception:
        return False
    if not re.search(r"cookie|クッキー", body_text, re.IGNORECASE):
        return False
    try:
        await _click_first_visible(
            page.get_by_role("button", name="OK", exact=True),
            "cookie notice OK button",
            timeout=1000,
        )
        await page.wait_for_timeout(250)
        return True
    except Exception:
        return False


def _submission_prompt(prompt: str, duration: int | None) -> str:
    """Disambiguate native durations from aspect-ratio text in Dola chat."""
    if duration in (10, 15):
        return f"Create a {duration}-second video: {prompt}"
    # The duration extension attaches 30 seconds through protocol metadata.
    return prompt


def find_captcha_frame(page):
    for f in page.frames:
        if CAPTCHA_FRAME_KEY in f.url:
            return f
    return None


async def _fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=config.PROXY or None) as r:
            return await r.read()



async def attach_reference_images(page, image_paths: list[str]) -> None:
    """Uploads reference images through native file input and waits for TOS upload."""
    if not image_paths:
        return
    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=10000)
    events = []

    def on_response(response):
        url = response.url
        if "/alice/resource/prepare_upload" in url or "/upload/v1/" in url:
            events.append((response.status, url))

    page.on("response", on_response)
    try:
        await file_input.set_input_files(image_paths)
        expected = len(image_paths)
        deadline = time.time() + max(60, expected * 20)
        while time.time() < deadline:
            prepare_count = sum("/alice/resource/prepare_upload" in url and 200 <= status < 300
                                for status, url in events)
            tos_count = sum("/upload/v1/" in url and 200 <= status < 300
                            for status, url in events)
            # Wait for thumbnails and TOS completion before sending
            thumb_count = await page.locator('img[alt]').count()
            if prepare_count >= expected and tos_count >= expected and thumb_count >= expected:
                await page.wait_for_timeout(800)
                print(f"[upload] Reference images uploaded: {expected} image(s)", flush=True)
                return
            await page.wait_for_timeout(250)
        raise TimeoutError(
            f"Reference image upload timeout: prepare={prepare_count}/{expected}, tos={tos_count}/{expected}"
        )
    finally:
        page.remove_listener("response", on_response)


def _gen_track(distance: float):
    """Generates humanized mouse drag trajectory."""
    steps = random.randint(45, 65)
    overshoot = random.uniform(3, 9)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        s = 10 * t**3 - 15 * t**4 + 6 * t**5
        x = (distance + overshoot) * s
        y = random.uniform(-1.5, 1.5) if 0.1 < t < 0.95 else 0
        pts.append((x, y, random.randint(8, 22)))
    for i in range(1, random.randint(3, 5) + 1):
        pts.append((distance + overshoot * (1 - i / 5), random.uniform(-0.8, 0.8), random.randint(15, 30)))
    return pts


async def solve_slider(page, frame, attempt: int) -> bool:
    """Solves captcha slider notch within iframe and performs drag."""
    await frame.wait_for_selector("img", timeout=15000)
    await frame.evaluate("""async () => {
        const t0 = Date.now();
        while (Date.now() - t0 < 10000) {
            const imgs = [...document.images];
            if (imgs.length >= 2 && imgs.every(im => im.complete && im.naturalWidth > 0)) return;
            await new Promise(r => setTimeout(r, 200));
        }
        throw new Error("captcha images load timeout");
    }""")
    await page.wait_for_timeout(800)

    imgs = await frame.evaluate("""() => [...document.images].map(im => ({
        src: im.src, w: im.naturalWidth, h: im.naturalHeight,
        bw: im.getBoundingClientRect().width,
        left: im.getBoundingClientRect().left,
    }))""")
    bg = next((i for i in imgs if ".jpeg" in i["src"] or "-2." in i["src"]), None)
    piece = next((i for i in imgs if i is not bg and (".png" in i["src"] or "-1." in i["src"])), None)
    if not bg or not piece:
        print("  ✗ Captcha background or puzzle image not found", flush=True)
        return False

    bg_bytes = await _fetch_bytes(bg["src"])
    piece_bytes = await _fetch_bytes(piece["src"])
    Path("dbg_bg.jpg").write_bytes(bg_bytes)
    Path("dbg_piece.png").write_bytes(piece_bytes)

    gap_x, conf = find_gap_x(bg_bytes, piece_bytes)
    scale = bg["bw"] / bg["w"] if bg["w"] else 340 / 552
    distance = (gap_x - (piece["left"] - bg["left"]) / scale) * scale
    print(f"  [solve#{attempt}] gap_x={gap_x} conf={conf:.3f} scale={scale:.2f} distance={distance:.0f}px", flush=True)

    btn = frame.locator(".captcha-slider-btn")
    bb = await btn.bounding_box()
    if not bb:
        print("  ✗ Drag handle .captcha-slider-btn not found", flush=True)
        return False
    sx, sy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(150, 350))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(80, 180))
    for dx, dy, dt in _gen_track(distance):
        await page.mouse.move(sx + dx, sy + dy)
        await asyncio.sleep(dt / 1000)
    await page.wait_for_timeout(random.randint(120, 260))
    await page.mouse.up()

    for _ in range(10):
        await page.wait_for_timeout(700)
        if not find_captcha_frame(page):
            return True
    return False



async def poll_conversation(account: str, page, context, conversation_id: str,
                            timeout: int, on_poll=None) -> dict:
    """Polls accepted conversation for video completion."""
    cookies = await context.cookies("https://www.dola.com")
    ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
    start = time.time()
    last_callback = 0.0
    while time.time() - start < timeout:
        await asyncio.sleep(5)
        try:
            poll = await asyncio.wait_for(page.evaluate(
                POLL_JS, {"conversationId": conversation_id, "msToken": ms_token, "fp": fp}), timeout=30)
        except Exception as e:
            print(f"  Polling exception: {e}", flush=True)
            continue
        now = time.time()
        if on_poll and now - last_callback >= 30:
            on_poll(now)
            last_callback = now
        for text in poll.get("texts", []):
            if VIDEO_NOT_STARTED_PATTERN.search(text):
                raise RuntimeError(
                    "Dola did not start video generation and requested a different "
                    f"duration: {text[:180]}"
                )
            classified = error_from_upstream_text(text, pre_generation=True)
            # Ordinary progress text is intentionally ignored. Only the shared
            # classifier's explicit outcomes are actionable here.
            if not isinstance(classified, DolaTemporarilyUnavailableError):
                raise classified
        if poll.get("videos"):
            video_models = poll.get("videoModels", [])
            url = extract_unwatermarked_url(
                video_models[0] if video_models else "", poll["videos"][0])
            print(f"[{account}] Completed! Downloading (unwatermarked priority)...", flush=True)
            local = await _download(url, account)
            print(f"[{account}] Downloaded {local} ({local.stat().st_size / 1e6:.1f} MB)", flush=True)
            return {"video_url": url, "local_path": str(local),
                    "conversation_id": conversation_id, "account": account}
        print(f"  ...Generating ({int(time.time() - start)}s)", flush=True)
    raise TimeoutError(f"No video generated within {timeout}s (conversation_id={conversation_id})")


async def resume_video(account: str, conversation_id: str, timeout: int,
                       on_poll=None) -> dict:
    """Recovers accepted session after server restart without re-sending prompt."""
    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False, use_extension=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://www.dola.com/chat/{conversation_id}",
                            timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            return await poll_conversation(account, page, context, conversation_id, timeout, on_poll)
        finally:
            await context.close()


async def generate_video(account: str, prompt: str, ratio: str = None,
                         duration: int = None, timeout: int = None,
                         model: str = "seedance_v2.0", use_extension: bool = True,
                         on_conversation_id=None, on_poll=None,
                         reference_image_paths: list[str] | None = None) -> dict:
    """Full generation flow via UI automation."""
    timeout = timeout or config.VIDEO_TIMEOUT
    model_key = model.lower().replace("-", "_")
    if model_key in ("seedance_2.5", "seedance_v2.5", "seedance_25", "seedance_v25"):
        model_key = "seedance_v2.5"
    elif model_key in ("seedance_2.0", "seedance_v2.0", "seedance_20", "seedance_v20"):
        model_key = "seedance_v2.0"
    else:
        raise ValueError(f"Unsupported model: {model} (supported: seedance-2.0 / seedance-2.5)")
    if duration is not None and duration not in (10, 15, 30):
        raise ValueError("Dola supports durations of 10s, 15s, and 30s via extension")
    if duration == 30 and not use_extension:
        raise ValueError("30s generation requires Dola30 extension enabled")
    # 30s videos require extended generation timeout
    if duration == 30:
        timeout = max(timeout, 1800)
    if reference_image_paths:
        timeout = max(timeout, config.REFERENCE_VIDEO_TIMEOUT)
    async with async_playwright() as p:
        context = await launch_account_context(
            p, account, headless=False if use_extension else None,
            use_extension=use_extension)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await _dismiss_cookie_notice(page)
            # ---- UI Submission ----
            await page.click(VIDEO_BTN)
            await page.wait_for_timeout(1500)
            if reference_image_paths:
                await attach_reference_images(page, reference_image_paths)
            # Select model in UI
            try:
                await _click_first_visible(
                    page.get_by_role("button", name=re.compile(r"^モデル")),
                    "model button",
                    timeout=15000,
                )
                await page.wait_for_timeout(500)
                options = (("Dreamina Seedance 2.5",)
                           if model_key == "seedance_v2.5"
                           else ("Dreamina Seedance 2.0高速", "Dreamina Seedance 2.0", "Seedance2.0Fast"))
                selected = False
                for option_text in options:
                    loc = page.get_by_role("menuitem").filter(has_text=option_text)
                    try:
                        await _click_first_visible(loc, f"model option {option_text}")
                        selected = True
                        break
                    except RuntimeError:
                        continue
                if not selected:
                    raise RuntimeError("Model option not found")
                await page.wait_for_timeout(500)
            except Exception as e:
                raise RuntimeError(f"Failed to set model ({model_key}): {str(e)[:120]}") from e
            if ratio:
                try:
                    await _click_first_visible(
                        page.get_by_role("button", name="比率", exact=True),
                        "ratio button",
                        timeout=3000,
                    )
                    await page.wait_for_timeout(500)
                    await _click_first_visible(
                        page.get_by_role("menuitem", name=ratio, exact=True),
                        f"ratio option {ratio}",
                        timeout=3000,
                    )
                except Exception as e:
                    print(f"  (Failed to set ratio, using default: {str(e)[:80]})", flush=True)
            if duration:
                try:
                    duration_label = f"{duration}s"
                    duration_button = page.get_by_role(
                        "button", name=re.compile(r"^\d+s$"))
                    current = await _first_visible(
                        duration_button, "duration button", timeout=5000)
                    # Clicking the already-selected value only opens the menu and
                    # leaves it covering the editor. Skip that click entirely.
                    if (await current.inner_text()).strip() != duration_label:
                        await current.click(timeout=3000)
                        await page.wait_for_timeout(500)
                        await _click_first_visible(
                            page.get_by_role("menuitem", name=duration_label, exact=True),
                            f"duration option {duration_label}",
                            timeout=3000,
                        )
                        await page.wait_for_timeout(500)
                    selected_duration_button = await _first_visible(
                        duration_button, "selected duration button", timeout=5000)
                    selected_duration = (await selected_duration_button.inner_text()).strip()
                    if selected_duration != duration_label:
                        raise RuntimeError(
                            f"Duration selection did not stick (wanted {duration_label}, "
                            f"got {selected_duration or 'no visible value'})"
                        )
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to set requested duration {duration}s: {str(e)[:120]}"
                    ) from e
            await page.keyboard.press("Escape")
            box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
            if box is None:
                raise RuntimeError("Prompt editor not found")
            await box.click()
            submitted_prompt = _submission_prompt(prompt, duration)
            await page.keyboard.type(submitted_prompt, delay=100)
            await page.wait_for_timeout(600)
            await page.keyboard.press("Enter")
            print(f"[{account}] UI submitted prompt: {submitted_prompt[:60]}", flush=True)

            # ---- Manual verification only; never automate Dola's challenge ----
            frame = None
            for _ in range(20):
                await page.wait_for_timeout(1000)
                frame = find_captcha_frame(page)
                if frame:
                    break
            if frame:
                print(
                    f"[{account}] Dola verification is waiting in Chromium; "
                    "complete it manually to continue",
                    flush=True,
                )
                deadline = time.monotonic() + config.VIDEO_VERIFICATION_TIMEOUT
                while find_captcha_frame(page) and time.monotonic() < deadline:
                    await page.wait_for_timeout(1000)
                if find_captcha_frame(page):
                    raise VerificationRequiredError("Manual Dola verification timed out")
                await page.wait_for_timeout(3000)

            # ---- Wait for real conversation_id ----
            conv_id = ""
            for _ in range(30):
                await page.wait_for_timeout(1000)
                tail = page.url.rstrip("/").split("/")[-1]
                if tail.isdigit():
                    conv_id = tail
                    break
            if not conv_id:
                await page.screenshot(path="no_conv.png")
                raise TimeoutError("conversation_id not acquired within 30s")
            print(f"[{account}] conversation_id={conv_id}, polling for video...", flush=True)

            deadline = time.time() + timeout
            if on_conversation_id:
                on_conversation_id(account, conv_id, deadline)
            return await poll_conversation(account, page, context, conv_id, timeout, on_poll)
        finally:
            await context.close()


async def _main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "An orange cat napping on a sunny windowsill"
    ratio = sys.argv[3] if len(sys.argv) > 3 else None
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else None
    model = sys.argv[5] if len(sys.argv) > 5 else "seedance_v2.0"
    result = await generate_video(account, prompt, ratio, duration, model=model)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception:
        import traceback
        traceback.print_exc()
