"""Dola Pool: OpenAI-compatible Video API (FastAPI) and Admin Dashboard.

Endpoints (Asynchronous 2-stage):
POST /v1/videos/generations -> Create task (status=queued)
GET  /v1/videos/<id>         -> Query task status (queued/processing/completed/failed)
GET  /videos/<file>          -> Static video download server

Unified local app: GET / and /playground; owner API /api/admin/*
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import stat
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse, urlsplit

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import config
from .add_account import add_account_flow
from .app_version import APP_VERSION
from .browser_pool import AllAccountsLimitedError, AllAccountsQuotaBlockedError, BrowserPool
from .desktop_support import create_support_bundle
from .media import download_reference_images, validate_reference_urls
from .store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore
from .upstream_errors import (
    DolaTemporarilyUnavailableError,
    ExplicitRestrictionError,
    PromptContentRejectedError,
)

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path(config.IMAGE_DIR).mkdir(parents=True, exist_ok=True)
WEB_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """Recover durable work at startup and stop local workers cleanly."""
    await resume_incomplete_tasks()
    try:
        yield
    finally:
        tasks = [task for task in BACKGROUND_TASKS if not task.done()]
        tasks.extend(task for task in ADD_JOB_TASKS.values() if not task.done())
        for task in set(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*set(tasks), return_exceptions=True)
        if config.DESKTOP_MODE:
            # The desktop process owns this app for its full lifetime.  Closing
            # both SQLite handles here makes installer/update shutdowns clean.
            for owner in (store, pool):
                owner.close()


app = FastAPI(title="Dola Gateway", version=APP_VERSION, lifespan=app_lifespan)
DESKTOP_UVICORN_SERVER = None


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _request_host(raw: str) -> str | None:
    try:
        parsed = urlsplit(f"//{raw}")
        # Accessing .port validates malformed/non-numeric ports.
        _ = parsed.port
        return parsed.hostname
    except ValueError:
        return None


def _trusted_origin(raw: str) -> bool:
    try:
        parsed = urlparse(raw)
        return (
            parsed.scheme == "http"
            and parsed.hostname in _LOOPBACK_HOSTS
            and parsed.port == config.PORT
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


@app.middleware("http")
async def desktop_loopback_boundary(request: Request, call_next):
    """Block DNS rebinding and cross-site browser calls in desktop mode."""
    if config.DESKTOP_MODE:
        if _request_host(request.headers.get("host", "")) not in _LOOPBACK_HOSTS:
            return JSONResponse(status_code=400, content={"detail": "invalid host"})
        origin = request.headers.get("origin")
        if origin and not _trusted_origin(origin):
            return JSONResponse(status_code=403, content={"detail": "invalid origin"})
    return await call_next(request)

store = TaskStore(config.DB_PATH)
pool = BrowserPool(
    accounts_dir=config.ACCOUNTS_DIR,
    db_path=config.POOL_DB_PATH,
    max_concurrency=config.MAX_CONCURRENCY,
)

if config.DESKTOP_MODE and os.name == "posix":
    for private_file in (Path(config.DB_PATH), Path(config.POOL_DB_PATH)):
        if private_file.is_file():
            private_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

if not config.DESKTOP_MODE:
    app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")

# Background jobs (add/verify), in-memory
JOBS: dict[str, dict] = {}
ADD_JOB_TASKS: dict[str, asyncio.Task] = {}
BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background_task(coroutine) -> asyncio.Task:
    """Start and retain a worker task until it completes."""
    task = asyncio.create_task(coroutine)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
IMAGE_SIZE_TO_RATIO = {
    "1024x1024": "1:1",
    "1024x1536": "2:3",
    "1536x2048": "3:4",
    "2048x1536": "4:3",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
}
SUPPORTED_IMAGE_RATIOS = {"1:1", "2:3", "16:9", "9:16", "4:3", "3:4"}
SUPPORTED_DURATIONS = (10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConcurrencyLimiter:
    """Concurrency limits per API Key; 0 = unlimited."""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active: defaultdict[str, int] = defaultdict(int)

    async def acquire(self, api_key_hash: str | None, limit: int):
        if not api_key_hash or limit <= 0:
            return
        async with self._condition:
            while self._active[api_key_hash] >= limit:
                await self._condition.wait()
            self._active[api_key_hash] += 1

    async def release(self, api_key_hash: str | None):
        if not api_key_hash:
            return
        async with self._condition:
            if self._active[api_key_hash] > 0:
                self._active[api_key_hash] -= 1
            if self._active[api_key_hash] == 0:
                self._active.pop(api_key_hash, None)
            self._condition.notify_all()



key_limiter = KeyConcurrencyLimiter()


# ===== Authentication =====


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


DESKTOP_OWNER_HASH = _hash_key("dola-gateway-desktop-owner-v1")


def _secret_matches(candidate: str | None, expected: str | None) -> bool:
    if not candidate or not expected:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[7:].strip()


def _anonymous_client() -> dict:
    return {
        "api_key_hash": None,
        "api_key_name": "Anonymous",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _env_client(key: str) -> dict:
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": f"Env Key ({key[:8]}…)",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _desktop_owner_client() -> dict:
    """One durable owner namespace authenticated by a fresh launch token."""
    return {
        "api_key_hash": DESKTOP_OWNER_HASH,
        "api_key_name": "Desktop Owner",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _auth(authorization):
    """Returns client policy for caller; empty key enables dev mode."""
    key = _bearer_token(authorization)
    if config.DESKTOP_MODE:
        if _secret_matches(key, config.DESKTOP_TOKEN):
            return _desktop_owner_client()
        if not config.LOCAL_API_ENABLED:
            raise HTTPException(403, "local API is disabled")
        # Enabling the integration does not silently create an unauthenticated
        # API. The owner can explicitly configure or create a client key.
        if not key:
            raise HTTPException(401, "missing bearer token")
        if not config.API_KEYS and not store.has_enabled_keys():
            raise HTTPException(401, "invalid api key")

    if not config.API_KEYS and not store.has_enabled_keys():
        return _anonymous_client()
    if not key:
        raise HTTPException(401, "missing bearer token")
    if any(_secret_matches(key, expected) for expected in config.API_KEYS):
        return _env_client(key)
    record = store.get_key(key)
    if not record or not store.is_key_valid(key):
        raise HTTPException(401, "invalid api key")
    store.touch_key(key)
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": record["name"] or "Unnamed Client",
        "daily_limit": record["daily_limit"],
        "concurrency_limit": record["concurrency_limit"],
        "allowed_durations": record["allowed_durations"],
    }


def _admin_auth(x_admin_key: str | None):
    if config.DESKTOP_MODE and _secret_matches(x_admin_key, config.DESKTOP_TOKEN):
        return
    if config.DESKTOP_MODE and not config.ADMIN_KEY:
        raise HTTPException(401, "invalid owner token")
    if not config.ADMIN_KEY:
        return
    if not _secret_matches(x_admin_key, config.ADMIN_KEY):
        raise HTTPException(401, "invalid admin key")


def _desktop_owner_auth(
    authorization: str | None,
    x_admin_key: str | None,
) -> None:
    """Protect detailed desktop-only status without changing dev behavior."""
    if not config.DESKTOP_MODE:
        return
    if _secret_matches(_bearer_token(authorization), config.DESKTOP_TOKEN):
        return
    _admin_auth(x_admin_key)


def _shutdown_process():
    """Request a graceful server stop after the HTTP response is delivered."""
    if config.DESKTOP_MODE and DESKTOP_UVICORN_SERVER is not None:
        DESKTOP_UVICORN_SERVER.should_exit = True
        return
    os.kill(os.getpid(), signal.SIGTERM)


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, "allowed_durations must be an array of 10, 15, or 30")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(422, "allowed_durations must contain at least one of 10, 15, 30")
    return normalized


# ===== Client API =====


class VideoGenRequest(BaseModel):
    model: str = "seedance-2.0"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=10, le=30)
    # Accepts durations: 10, 15, 30 seconds.
    reference_images: list[str] = Field(default_factory=list)
    retry_account: str | None = Field(None, min_length=1, max_length=32)


class TaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None
    ratio: str | None = None
    duration: int | None = None
    created_at: float | None = None


class ImageGenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "dola-image"
    prompt: str = Field(..., min_length=1, max_length=4000)
    size: str | None = None
    ratio: str | None = None
    style: str = Field("auto", min_length=1, max_length=100)
    n: int = Field(1, ge=1, le=1)
    retry_account: str | None = Field(None, min_length=1, max_length=32)


class ImageTaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    error: str | None = None
    ratio: str | None = None
    style: str | None = None
    created_at: float | None = None


def _json_string_list(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def _row_image_urls(row: dict) -> list[str]:
    values = _json_string_list(row.get("image_urls"))
    if values:
        return values
    return [row["image_url"]] if row.get("image_url") else []


def _image_task_response(row: dict) -> ImageTaskResponse:
    image_urls = _row_image_urls(row)
    return ImageTaskResponse(
        id=row["id"],
        status=row["status"],
        model=row.get("model"),
        prompt=row.get("prompt"),
        image_url=image_urls[0] if image_urls else None,
        image_urls=image_urls,
        error=row.get("error"),
        ratio=row.get("ratio"),
        style=row.get("style"),
        created_at=row.get("created_at"),
    )


def _video_task_response(row: dict) -> TaskResponse:
    ratio = row.get("ratio")
    return TaskResponse(
        id=row["id"],
        status=row["status"],
        model=row.get("model"),
        prompt=row.get("prompt"),
        video_url=row.get("video_url"),
        error=row.get("error"),
        ratio=None if ratio == "default" else ratio,
        duration=row.get("duration"),
        created_at=row.get("created_at"),
    )


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise HTTPException(
            422,
            "Idempotency-Key must contain 1-128 letters, numbers, '.', '_', ':', or '-'",
        )
    return value


def _stored_image_path(row: dict, index: int = 0) -> Path:
    paths = _json_string_list(row.get("local_paths"))
    if not paths and row.get("local_path"):
        paths = [row["local_path"]]
    if paths:
        if index < 0 or index >= len(paths):
            raise HTTPException(404, "image result not found")
        candidate = Path(paths[index])
    else:
        # Compatibility for image jobs created before local_path was stored.
        if index != 0:
            raise HTTPException(404, "image result not found")
        filename = Path(urlparse(row.get("image_url") or "").path).name
        candidate = Path(config.IMAGE_DIR) / filename
    root = Path(config.IMAGE_DIR).resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(404, "image result not found")
    return resolved


def _stored_video_path(row: dict) -> Path:
    if row.get("local_path"):
        candidate = Path(row["local_path"])
    else:
        # Compatibility for video jobs completed before local_path was persisted.
        filename = Path(urlparse(row.get("video_url") or "").path).name
        candidate = Path(config.DOWNLOAD_DIR) / filename
    root = Path(config.DOWNLOAD_DIR).resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(404, "video result not found")
    return resolved


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


def _resolve_image_ratio(size: str | None, ratio: str | None) -> str:
    if size:
        if size not in IMAGE_SIZE_TO_RATIO:
            raise HTTPException(
                422, f"Unsupported image size; use one of {sorted(IMAGE_SIZE_TO_RATIO)}"
            )
        return IMAGE_SIZE_TO_RATIO[size]
    resolved = ratio or "1:1"
    if resolved not in SUPPORTED_IMAGE_RATIOS:
        raise HTTPException(
            422, f"Unsupported image ratio; use one of {sorted(SUPPORTED_IMAGE_RATIOS)}"
        )
    return resolved


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client,
                    retry_account=None):
    api_key_hash = client.get("api_key_hash")
    acquired = False
    reference_root = None
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        store.update(task_id, status="processing", started_at=time.time())

        def on_conversation_id(account, conversation_id, deadline_at):
            store.update(task_id, status="processing", account=account,
                         conversation_id=conversation_id, deadline_at=deadline_at,
                         last_poll_at=time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        reference_root, reference_paths = await download_reference_images(
            reference_images or [], task_id)
        generation_options = {
            "on_conversation_id": on_conversation_id,
            "on_poll": on_poll,
            "reference_image_paths": reference_paths,
        }
        if retry_account:
            generation_options["retry_account"] = retry_account
        result = await pool.generate_video(
            prompt, ratio, duration, model, **generation_options
        )
        public_url = (
            f"{config.PUBLIC_BASE}/v1/videos/{task_id}/content"
            if config.DESKTOP_MODE
            else f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        )
        store.update(task_id, status="completed", video_url=public_url,
                     local_path=result["local_path"],
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError,
            ExplicitRestrictionError) as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     failure_code="account_restricted", finished_at=time.time())
    except PromptContentRejectedError as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     failure_code="content_rejected", finished_at=time.time())
    except DolaTemporarilyUnavailableError as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     failure_code="upstream_temporary", finished_at=time.time())
    except Exception as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     finished_at=time.time())
    finally:
        if reference_root:
            shutil.rmtree(reference_root, ignore_errors=True)
        if acquired:
            await key_limiter.release(api_key_hash)


async def _run_image_task(task_id, model, prompt, ratio, style, client,
                          retry_account=None):
    api_key_hash = client.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True

        def on_start(account):
            store.update(
                task_id,
                status="processing",
                account=account,
                started_at=time.time(),
            )

        generation_options = {"on_start": on_start}
        if retry_account:
            generation_options["retry_account"] = retry_account
        result = await pool.generate_image(
            prompt, ratio, style, **generation_options
        )
        images = result.get("images") or [result]
        local_paths = [image["local_path"] for image in images]
        public_urls = [
            f"{config.PUBLIC_BASE}/v1/images/{task_id}/content/{index}"
            for index in range(len(local_paths))
        ]
        store.update(
            task_id,
            status="completed",
            image_url=public_urls[0],
            image_urls=json.dumps(public_urls),
            local_path=local_paths[0],
            local_paths=json.dumps(local_paths),
            account=result.get("account"),
            finished_at=time.time(),
        )
    except PromptContentRejectedError as exc:
        store.update(task_id, status="failed", error=str(exc)[:500],
                     failure_code="content_rejected", finished_at=time.time())
    except DolaTemporarilyUnavailableError as exc:
        store.update(task_id, status="failed", error=str(exc)[:500],
                     failure_code="upstream_temporary", finished_at=time.time())
    except ExplicitRestrictionError as exc:
        store.update(task_id, status="failed", error=str(exc)[:500],
                     failure_code="account_restricted", finished_at=time.time())
    except Exception as exc:
        store.update(
            task_id,
            status="failed",
            error=str(exc)[:500],
            finished_at=time.time(),
        )
    finally:
        if acquired:
            await key_limiter.release(api_key_hash)


async def _resume_task(row: dict):
    task_id = row["id"]
    deadline = row.get("deadline_at") or (
        time.time() + (1800 if row.get("duration") == 30 else config.VIDEO_TIMEOUT)
    )
    remaining = max(1, int(deadline - time.time()))
    api_key_hash = row.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        store.update(task_id, status="processing", last_poll_at=time.time(),
                     started_at=row.get("started_at") or time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining, on_poll=on_poll)
        public_url = (
            f"{config.PUBLIC_BASE}/v1/videos/{task_id}/content"
            if config.DESKTOP_MODE
            else f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        )
        store.update(task_id, status="completed", video_url=public_url,
                     local_path=result["local_path"],
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except Exception as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     finished_at=time.time())
    finally:
        if acquired:
            await key_limiter.release(api_key_hash)


def _task_client(row: dict) -> dict:
    """Restores client context from task snapshot."""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "Historical Task",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _task_reference_images(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


async def resume_incomplete_tasks():
    """Recovers accepted sessions on startup and requeues pending tasks."""
    store.fail_interrupted_manual_retries()
    store.fail_interrupted_image_tasks()
    for row in store.recoverable_tasks():
        _spawn_background_task(_resume_task(row))
    for row in store.recoverable_queued_tasks():
        ratio = row.get("ratio")
        if ratio == "default":
            ratio = None
        _spawn_background_task(_run_task(
            row["id"], row["model"], row["prompt"], ratio, row["duration"],
            _task_reference_images(row.get("reference_images")), _task_client(row),
        ))
    for row in store.recoverable_image_tasks():
        _spawn_background_task(_run_image_task(
            row["id"],
            row["model"],
            row["prompt"],
            row.get("ratio") or "1:1",
            row.get("style") or "auto",
            _task_client(row),
        ))


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(
    req: VideoGenRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_admin_key: str | None = Header(default=None),
):
    client = _auth(authorization)
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    if req.retry_account:
        _admin_auth(x_admin_key)
    existing = store.find_idempotent(
        client["api_key_hash"], "video", idempotency_key
    )
    if existing:
        return _video_task_response(existing)
    if req.retry_account:
        if not req.prompt.strip():
            raise HTTPException(422, "Manual retry requires a real prompt")
        if not NAME_RE.fullmatch(req.retry_account):
            raise HTTPException(422, "invalid retry_account")
        try:
            pool.validate_manual_retry(req.retry_account, "video")
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    duration = req.duration or 10
    if duration not in SUPPORTED_DURATIONS:
        raise HTTPException(422, "Currently supports durations of 10s, 15s, and 30s")
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"Current API Key is not allowed to generate {duration}s videos")
    model_key = req.model.lower().replace("-", "_")
    if model_key not in (
        "seedance_2.0", "seedance_2.5", "seedance_v2.0", "seedance_v2.5",
        "seedance_20", "seedance_25", "seedance_v20", "seedance_v25",
    ):
        raise HTTPException(422, "Supported models are seedance-2.0 and seedance-2.5")
    try:
        reference_images = await validate_reference_urls(req.reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Queue task when accounts are busy; reject only when pool is fully exhausted.
    if not req.retry_account and not pool.available and pool.all_accounts_limited:
        raise HTTPException(
            429,
            "All enabled accounts have a video restriction. Use Retry video now on Accounts for one deliberate attempt.",
        )
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = "video_" + uuid.uuid4().hex
    ratio = _resolve_ratio(req.size, req.ratio)
    try:
        created = store.create(
            task_id,
            req.model,
            req.prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
            media_type="video",
            idempotency_key=idempotency_key,
            retry_account=req.retry_account,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    if created["id"] != task_id:
        return _video_task_response(created)
    _spawn_background_task(
        _run_task(
            task_id, req.model, req.prompt, ratio, duration, reference_images,
            client, req.retry_account,
        )
    )
    return _video_task_response(created)


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(task_id: str, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row or row.get("media_type") != "video":
        raise HTTPException(404, "task not found")
    return _video_task_response(row)


@app.get("/v1/videos", response_model=list[TaskResponse])
async def list_videos(
    limit: int = 20,
    authorization: str | None = Header(default=None),
):
    client = _auth(authorization)
    rows = store.recent_tasks_for_client(
        client["api_key_hash"], media_type="video", limit=min(max(limit, 1), 50)
    )
    return [_video_task_response(row) for row in rows]


@app.get("/v1/videos/{task_id}/content")
async def get_video_content(
    task_id: str,
    authorization: str | None = Header(default=None),
):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row or row.get("media_type") != "video":
        raise HTTPException(404, "video task not found")
    if row.get("status") != "completed":
        raise HTTPException(409, "video task is not completed")
    path = _stored_video_path(row)
    return FileResponse(path, filename=f"{task_id}{path.suffix}")


@app.post("/v1/images/generations", response_model=ImageTaskResponse)
async def create_image(
    req: ImageGenRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_admin_key: str | None = Header(default=None),
):
    client = _auth(authorization)
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    if req.retry_account:
        _admin_auth(x_admin_key)
    existing = store.find_idempotent(
        client["api_key_hash"], "image", idempotency_key
    )
    if existing:
        return _image_task_response(existing)
    if req.retry_account:
        if not req.prompt.strip():
            raise HTTPException(422, "Manual retry requires a real prompt")
        if not NAME_RE.fullmatch(req.retry_account):
            raise HTTPException(422, "invalid retry_account")
        try:
            pool.validate_manual_retry(req.retry_account, "image")
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if req.model.lower() not in {"dola-image", "dola-image-1"}:
        raise HTTPException(422, "Supported image model is dola-image")
    ratio = _resolve_image_ratio(req.size, req.ratio)
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    if not req.retry_account and not pool.available_for_image:
        raise HTTPException(
            429,
            "All enabled accounts have an image restriction or are unavailable. "
            "Use Retry image now on Accounts for a deliberate restricted-account attempt.",
        )

    task_id = "image_" + uuid.uuid4().hex
    try:
        created = store.create(
            task_id,
            req.model,
            req.prompt,
            ratio,
            0,
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
            media_type="image",
            style=req.style,
            idempotency_key=idempotency_key,
            retry_account=req.retry_account,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    if created["id"] != task_id:
        return _image_task_response(created)
    _spawn_background_task(
        _run_image_task(
            task_id, req.model, req.prompt, ratio, req.style, client,
            req.retry_account,
        )
    )
    return _image_task_response(created)


@app.get("/v1/images/{task_id}", response_model=ImageTaskResponse)
async def get_image(task_id: str, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row or row.get("media_type") != "image":
        raise HTTPException(404, "image task not found")
    return _image_task_response(row)


@app.get("/v1/images", response_model=list[ImageTaskResponse])
async def list_images(
    limit: int = 20,
    authorization: str | None = Header(default=None),
):
    client = _auth(authorization)
    rows = store.recent_tasks_for_client(
        client["api_key_hash"], media_type="image", limit=min(max(limit, 1), 50)
    )
    return [_image_task_response(row) for row in rows]


@app.get("/v1/images/{task_id}/content")
async def get_image_content(
    task_id: str,
    authorization: str | None = Header(default=None),
):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row or row.get("media_type") != "image":
        raise HTTPException(404, "image task not found")
    if row.get("status") != "completed":
        raise HTTPException(409, "image task is not completed")
    path = _stored_image_path(row, 0)
    return FileResponse(path, filename=f"{task_id}{path.suffix}")


@app.get("/v1/images/{task_id}/content/{index}")
async def get_image_content_at_index(
    task_id: str,
    index: int,
    authorization: str | None = Header(default=None),
):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row or row.get("media_type") != "image":
        raise HTTPException(404, "image task not found")
    if row.get("status") != "completed":
        raise HTTPException(409, "image task is not completed")
    path = _stored_image_path(row, index)
    return FileResponse(path, filename=f"{task_id}_{index + 1}{path.suffix}")


@app.get("/health")
async def health(
    authorization: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
):
    """Compatibility health summary used by existing launchers and clients."""
    _desktop_owner_auth(authorization, x_admin_key)
    return {
        "ok": True,
        "version": APP_VERSION,
        "accounts": pool.account_status(),
        "available": pool.available,
        "image_available": pool.available_for_image,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
    }


@app.get("/health/live", include_in_schema=False)
async def health_live():
    """Minimal process liveness check with no browser or database dependency."""
    result = {"ok": True, "status": "live", "version": APP_VERSION}
    if config.DESKTOP_MODE:
        result["instance_id"] = config.INSTANCE_ID
    return result


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    """Report whether local state and bundled UI are ready to serve requests."""
    try:
        accounts = pool.account_status()
        pending_tasks = store.pending_task_count()
        ui_available = (WEB_DIR / "playground.html").is_file()
        if not ui_available:
            raise RuntimeError("unified app shell is missing")
    except Exception:
        content = {
            "ok": False,
            "status": "not_ready",
            "version": APP_VERSION,
            "error": "local_dependencies_unavailable",
        }
        if config.DESKTOP_MODE:
            content["instance_id"] = config.INSTANCE_ID
        return JSONResponse(
            status_code=503,
            content=content,
        )
    if config.DESKTOP_MODE:
        return {
            "ok": True,
            "status": "ready",
            "version": APP_VERSION,
            "instance_id": config.INSTANCE_ID,
        }
    return {
        "ok": True,
        "status": "ready",
        "version": APP_VERSION,
        "setup_complete": any(account.get("login_ok") for account in accounts),
        "account_count": len(accounts),
        "generation_available": pool.available or pool.available_for_image,
        "pending_tasks": pending_tasks,
    }


@app.get("/api/app/bootstrap", include_in_schema=False)
async def app_bootstrap():
    """Safe, secret-free state for the local owner application shell."""
    accounts = pool.account_status()
    return {
        "name": "Dola Gateway",
        "version": APP_VERSION,
        "auth_required": bool(config.ADMIN_KEY or config.DESKTOP_MODE),
        "desktop_mode": config.DESKTOP_MODE,
        "local_api_enabled": config.LOCAL_API_ENABLED,
        "client_key_configured": bool(config.API_KEYS or store.has_enabled_keys()),
        "setup_complete": any(account.get("login_ok") for account in accounts),
        "account_count": len(accounts),
        "logged_in_account_count": sum(
            1 for account in accounts if account.get("login_ok")
        ),
        "docs_url": "/docs",
        "playground_url": "/playground",
    }


# ===== Admin Dashboard API =====


class AdminLogin(BaseModel):
    key: str


class AccountPatch(BaseModel):
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None


class AccountAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    email: str = ""


class KeyCreate(BaseModel):
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    if config.DESKTOP_MODE and _secret_matches(body.key, config.DESKTOP_TOKEN):
        return {"ok": True, "auth_required": True, "desktop_owner": True}
    if config.DESKTOP_MODE and not config.ADMIN_KEY:
        raise HTTPException(401, "wrong owner token")
    if not config.ADMIN_KEY:
        return {"ok": True, "auth_required": False}
    if _secret_matches(body.key, config.ADMIN_KEY):
        return {"ok": True, "auth_required": True}
    raise HTTPException(401, "wrong admin key")


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"accounts": pool.list_accounts()}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    return {"ok": True}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    login_task = ADD_JOB_TASKS.get(name)
    if login_task and not login_task.done():
        login_task.cancel()
        try:
            await login_task
        except asyncio.CancelledError:
            pass
    try:
        # Keep discovery, lock inspection, profile removal, and metadata cleanup
        # in one event-loop turn so a generation cannot claim this profile in
        # the middle of deletion.
        pool.delete_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except OSError as e:
        raise HTTPException(
            409,
            "Could not remove the saved profile. Close its sign-in/browser window and try again.",
        ) from e
    ADD_JOB_TASKS.pop(name, None)
    JOBS.pop(name, None)
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        ok = await pool.verify_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"ok": ok}


async def _run_add_job(name: str, email: str):
    previous = JOBS.get(name) or {}
    JOBS[name] = {
        "kind": "add",
        "status": "running",
        "error": "",
        "message": "Waiting for manual Google sign-in in browser",
        "started_at": previous.get("started_at") or time.time(),
    }
    try:
        await add_account_flow(name)
        pool.set_email(name, email)
        pool.set_login_status(name, True)
        JOBS[name] = {**JOBS[name], "status": "success", "message": "Login complete"}
    except asyncio.CancelledError:
        JOBS[name] = {
            **JOBS[name],
            "status": "cancelled",
            "message": "Login cancelled",
        }
        raise
    except Exception as e:
        JOBS[name] = {**JOBS[name], "status": "failed", "error": str(e)[:300]}
    finally:
        current = asyncio.current_task()
        if ADD_JOB_TASKS.get(name) is current:
            ADD_JOB_TASKS.pop(name, None)


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    existing_task = ADD_JOB_TASKS.get(body.name)
    if existing_task and not existing_task.done():
        raise HTTPException(
            409, f"Login is already open for {body.name}; cancel it before retrying"
        )
    if JOBS.get(body.name, {}).get("status") == "running":
        # A running marker without a live task is stale and must not block retry.
        JOBS[body.name] = {
            **JOBS[body.name],
            "status": "failed",
            "error": "Login job stopped unexpectedly; safe to retry",
        }
    if body.name in pool.accounts:
        account = next(row for row in pool.list_accounts() if row["name"] == body.name)
        if account.get("login_ok"):
            raise HTTPException(409, "account exists and is already logged in")

    JOBS[body.name] = {
        "kind": "add",
        "status": "running",
        "error": "",
        "message": "Waiting for manual Google sign-in in browser",
        "started_at": time.time(),
    }
    task = _spawn_background_task(_run_add_job(body.name, body.email))
    ADD_JOB_TASKS[body.name] = task
    return {
        "ok": True,
        "job": "running",
        "message": "Complete Google sign-in in the Chromium window",
    }


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": JOBS}


@app.delete("/api/admin/jobs/{name}")
async def admin_job_cancel(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    job = JOBS.get(name)
    if not job or job.get("kind") != "add":
        raise HTTPException(404, "add job not found")
    task = ADD_JOB_TASKS.get(name)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ADD_JOB_TASKS.pop(name, None)
        if JOBS[name].get("status") == "running":
            JOBS[name] = {
                **JOBS[name],
                "status": "cancelled",
                "message": "Login cancelled",
            }
    elif job.get("status") == "running":
        JOBS[name] = {
            **job,
            "status": "cancelled",
            "message": "Stale login job cleared",
        }
    return {"ok": True, "status": JOBS[name]["status"]}


@app.get("/api/admin/tasks")
async def admin_tasks(limit: int = 50, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    tasks = store.recent_tasks(min(max(limit, 1), 200))
    for task in tasks:
        task["image_count"] = len(_row_image_urls(task))
    return {"tasks": tasks}


@app.get("/api/admin/tasks/{task_id}/content")
async def admin_task_content(
    task_id: str,
    x_admin_key: str | None = Header(default=None),
):
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row or row.get("media_type") != "image":
        raise HTTPException(404, "image task not found")
    if row.get("status") != "completed":
        raise HTTPException(409, "image task is not completed")
    path = _stored_image_path(row, 0)
    return FileResponse(path, filename=f"{task_id}{path.suffix}")


@app.get("/api/admin/tasks/{task_id}/content/{index}")
async def admin_task_content_at_index(
    task_id: str,
    index: int,
    x_admin_key: str | None = Header(default=None),
):
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row or row.get("media_type") != "image":
        raise HTTPException(404, "image task not found")
    if row.get("status") != "completed":
        raise HTTPException(409, "image task is not completed")
    path = _stored_image_path(row, index)
    return FileResponse(path, filename=f"{task_id}_{index + 1}{path.suffix}")


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    st = store.stats()
    accs = pool.list_accounts()
    sched = [a for a in accs if a["scheduling"] and not a["cooling"]]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if pool._schedulable(a))
    st["quota_status"] = (
        "blocked"
        if pool.all_accounts_quota_blocked or pool.all_accounts_limited
        else "unavailable"
    )
    # Retain the old field for API compatibility, but never invent a number.
    st["total_remaining"] = None
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
    return st


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    keys = []
    for key in store.list_keys():
        usage = store.key_usage(store.hash_api_key(key["key"]))
        keys.append({**key, **{
            "today_total": usage["total"],
            "today_completed": usage["completed"],
            "today_failed": usage["failed"],
            "today_active": usage["active"],
            "today_queued": usage["queued"],
        }})
    return {"keys": keys, "env_keys": len(config.API_KEYS)}


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    allowed = _normalize_allowed_durations(body.allowed_durations)
    return {"created": store.create_key(
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
    )}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.concurrency_limit is not None:
        fields["concurrency_limit"] = body.concurrency_limit
    if body.allowed_durations is not None:
        fields["allowed_durations"] = _normalize_allowed_durations(body.allowed_durations)
    if body.expires_at is not None:
        fields["expires_at"] = body.expires_at
    store.update_key(key, **fields)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    store.delete_key(key)
    return {"ok": True}


async def _create_desktop_support_bundle() -> Path:
    return await asyncio.to_thread(
        create_support_bundle,
        state_dir=Path(config.STATE_DIR),
        output_dir=Path(config.SUPPORT_DIR),
        log_dir=Path(config.LOG_DIR),
        version=APP_VERSION,
        instance_id=config.INSTANCE_ID,
        db_path=Path(config.DB_PATH),
        pool_db_path=Path(config.POOL_DB_PATH),
        accounts_dir=Path(config.ACCOUNTS_DIR),
        downloads_dir=Path(config.DOWNLOAD_DIR),
        secrets=tuple(
            value
            for value in (
                config.DESKTOP_TOKEN,
                config.ADMIN_KEY,
                config.PROXY,
                *config.API_KEYS,
            )
            if value
        ),
    )


@app.post("/api/admin/support-bundle")
async def admin_support_bundle(x_admin_key: str | None = Header(default=None)):
    """Create a redacted diagnostic archive and return its local path."""
    _admin_auth(x_admin_key)
    bundle = await _create_desktop_support_bundle()
    return {"ok": True, "filename": bundle.name, "path": str(bundle)}


@app.post("/api/app/support-bundle", include_in_schema=False)
async def app_support_bundle(x_admin_key: str | None = Header(default=None)):
    """Download a fresh privacy-safe archive from the desktop UI."""
    _admin_auth(x_admin_key)
    bundle = await _create_desktop_support_bundle()
    return FileResponse(bundle, filename=bundle.name, media_type="application/zip")


@app.post("/api/admin/shutdown")
async def admin_shutdown(
    background_tasks: BackgroundTasks,
    x_admin_key: str | None = Header(default=None),
):
    _admin_auth(x_admin_key)
    background_tasks.add_task(_shutdown_process)
    return {"ok": True, "message": "Gateway is stopping"}


@app.get("/playground", include_in_schema=False)
async def playground():
    return FileResponse(WEB_DIR / "playground.html")


@app.get("/", include_in_schema=False)
async def application_shell():
    """Canonical local application entry point."""
    return FileResponse(WEB_DIR / "playground.html")


def create_app() -> FastAPI:
    """Uvicorn factory-compatible access to the configured app singleton."""
    return app


# Dashboard single-file frontend
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
