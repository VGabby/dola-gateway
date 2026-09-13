"""Dola Pool Configuration: Environment variable based with smart defaults."""
import os
import stat
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins."""
    configured = os.getenv("DOLA_ENV_FILE", "").strip()
    if configured:
        path = Path(configured).expanduser()
    elif os.getenv("DOLA_STATE_DIR"):
        path = Path(os.environ["DOLA_STATE_DIR"]).expanduser() / ".env.local"
    else:
        path = APP_DIR / ".env.local"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()

# The desktop shell provides a fresh capability and process identity for every
# launch.  A token is what enables desktop mode; normal source and API-server
# runs retain their historical behavior when it is absent.
DESKTOP_TOKEN = os.getenv("DOLA_DESKTOP_TOKEN", "").strip()
INSTANCE_ID = os.getenv("DOLA_INSTANCE_ID", "").strip()
DESKTOP_MODE = bool(DESKTOP_TOKEN)
LOCAL_API_ENABLED = os.getenv(
    "DOLA_LOCAL_API_ENABLED", "0" if DESKTOP_MODE else "1"
) == "1"

STATE_DIR = Path(os.getenv("DOLA_STATE_DIR", ".")).expanduser().resolve()


def _protect_directory(path: Path) -> None:
    """Create a private desktop-state directory and tighten POSIX permissions."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(stat.S_IRWXU)


def _protect_file(path: Path) -> None:
    """Tighten an existing desktop-state file without creating it."""
    if path.is_file() and os.name == "posix":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


if DESKTOP_MODE:
    _protect_directory(STATE_DIR)


def _state_path(env_name: str, default: str) -> str:
    """Resolve mutable relative paths underneath the configured state directory."""
    path = Path(os.getenv(env_name, default)).expanduser()
    if not path.is_absolute():
        path = STATE_DIR / path
    return str(path.resolve())


def _app_path(env_name: str, default: str) -> str:
    """Resolve packaged application assets relative to the source directory."""
    path = Path(os.getenv(env_name, default)).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return str(path.resolve())


HOST = os.getenv("DOLA_HOST", "127.0.0.1")
PORT = int(os.getenv("DOLA_PORT", "8000"))

if DESKTOP_MODE and HOST not in {"127.0.0.1", "localhost", "::1"}:
    raise ValueError("desktop mode must bind only to a loopback host")

# Service API keys (comma-separated; empty = no auth, local debug only)
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# Max concurrent video generation tasks
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "3"))

# Global pending task queue limit (queued + processing), 0 = unlimited
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "100"))

# Video generation timeout in seconds. Dola may finish well after five minutes,
# so keep polling long enough to collect late results instead of marking them lost.
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "900"))

# SQLite database path
DB_PATH = _state_path("DOLA_DB_PATH", "tasks.db")
POOL_DB_PATH = _state_path("DOLA_POOL_DB_PATH", "pool_usage.db")

# Video download storage directory (served statically by FastAPI)
DOWNLOAD_DIR = _state_path("DOLA_DOWNLOAD_DIR", "downloads")

# Generated image storage and time/size bounds.
IMAGE_DIR = _state_path("DOLA_IMAGE_DIR", str(Path(DOWNLOAD_DIR) / "images"))
IMAGE_TIMEOUT = int(os.getenv("DOLA_IMAGE_TIMEOUT", "180"))
IMAGE_SETTLE_SECONDS = float(os.getenv("DOLA_IMAGE_SETTLE_SECONDS", "5"))
IMAGE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_IMAGE_DOWNLOAD_TIMEOUT", "60"))
IMAGE_MAX_BYTES = int(os.getenv("DOLA_IMAGE_MAX_BYTES", str(25 * 1024 * 1024)))

# Explicit browser proxy (must point to JP/KR egress; empty = system proxy)
PROXY = os.getenv("DOLA_PROXY", "")

# Optional browser override. Empty uses Patchright's bundled Chromium.
BROWSER_EXECUTABLE = os.getenv("DOLA_BROWSER_EXECUTABLE", "")

# Each direct child directory is one isolated browser profile. Account labels and
# scheduling metadata live in POOL_DB_PATH; there is no separate account config.
ACCOUNTS_DIR = _state_path("DOLA_ACCOUNTS_DIR", "accounts")

# The desktop supervisor owns these paths.  Keeping them configurable makes
# offline tests and support tooling deterministic without weakening defaults.
LOG_DIR = _state_path("DOLA_LOG_DIR", "logs")
SUPPORT_DIR = _state_path("DOLA_SUPPORT_DIR", "support")

# Maximum time to wait for a user to complete manual Google sign-in.
LOGIN_TIMEOUT = int(os.getenv("DOLA_LOGIN_TIMEOUT", "600"))
VIDEO_VERIFICATION_TIMEOUT = int(os.getenv("DOLA_VIDEO_VERIFICATION_TIMEOUT", "180"))

# Run browser in headless mode (login always runs with head)
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"
# Image generation runs headed by default so a human can complete Dola
# verification in every deployment mode. Set the image-specific override to 1
# only when an unattended environment explicitly accepts verification failures.
IMAGE_HEADLESS = os.getenv("DOLA_IMAGE_HEADLESS", "0") == "1"

# Base public URL for returning static video links
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# Admin web dashboard password (empty = no auth)
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")

# Dola 30s / Watermark Removal Chromium extension path
EXTENSION_DIR = _app_path("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Public reference image download limits
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))

# Extended generation window for reference image tasks (seconds)
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "900"))


if DESKTOP_MODE:
    for _private_dir in (
        Path(DOWNLOAD_DIR),
        Path(IMAGE_DIR),
        Path(ACCOUNTS_DIR),
        Path(LOG_DIR),
        Path(SUPPORT_DIR),
        Path(DB_PATH).parent,
        Path(POOL_DB_PATH).parent,
    ):
        _protect_directory(_private_dir)
    for _private_file in (
        Path(DB_PATH),
        Path(POOL_DB_PATH),
        STATE_DIR / ".env.local",
    ):
        _protect_file(_private_file)
