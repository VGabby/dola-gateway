"""Browser account pool with observed usage and upstream-limit handling."""
import asyncio
import shutil
import sqlite3
import stat
import time
from datetime import date
from pathlib import Path

from .image_worker import (
    ImageGenerationError,
    LoginExpiredError,
    generate_image as generate_image_for_account,
)
from .video_worker_ui import generate_video, resume_video
from .upstream_errors import (
    DolaTemporarilyUnavailableError,
    ExplicitRestrictionError,
    PromptContentRejectedError,
    classify_upstream_text,
    error_from_upstream_text,
)
from . import config


class AllAccountsLimitedError(RuntimeError):
    """All active schedulable accounts have reached daily video limit."""


class AllAccountsQuotaBlockedError(RuntimeError):
    """Dola explicitly rejected generation for every schedulable account."""


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str = "pool_usage.db",
                 max_concurrency: int = 1):
        self.accounts_dir = Path(accounts_dir)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._account_available = asyncio.Condition()
        self._closed = False
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_meta (
                name TEXT PRIMARY KEY,
                scheduling INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                created_at REAL,
                last_used_at REAL DEFAULT 0,
                login_ok INTEGER,
                login_checked_at REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                rate_limited_until REAL DEFAULT 0,
                limit_reason TEXT DEFAULT '',
                quota_blocked_until REAL DEFAULT 0,
                quota_reason TEXT DEFAULT '',
                video_restricted_detected_at REAL DEFAULT 0,
                video_restriction_reason TEXT DEFAULT '',
                image_restricted_detected_at REAL DEFAULT 0,
                image_restriction_reason TEXT DEFAULT ''
            )
            """
        )
        self._conn.commit()
        # Legacy migration: add metadata columns
        for column, definition in (
            ("email", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("video_restricted_detected_at", "REAL DEFAULT 0"),
            ("video_restriction_reason", "TEXT DEFAULT ''"),
            ("image_restricted_detected_at", "REAL DEFAULT 0"),
            ("image_restriction_reason", "TEXT DEFAULT ''"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass
        self._migrate_legacy_video_restrictions()

    def close(self) -> None:
        """Release the owned SQLite connection exactly once."""
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ===== Account Discovery & Metadata =====

    def _ensure_meta(self, name: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO accounts_meta (name, created_at) VALUES (?, ?)",
            (name, time.time()),
        )
        self._conn.commit()

    @property
    def accounts(self) -> list:
        if not self.accounts_dir.exists():
            return []
        names = sorted(d.name for d in self.accounts_dir.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        for n in names:
            self._ensure_meta(n)
        return names

    def _meta(self, name: str):
        return self._conn.execute(
            "SELECT * FROM accounts_meta WHERE name=?", (name,)).fetchone()

    def used_today(self, account: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=?",
            (account, date.today().isoformat()),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str):
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,1) "
            "ON CONFLICT(account, day) DO UPDATE SET used=used+1",
            (account, date.today().isoformat()),
        )
        self._conn.commit()

    def _migrate_legacy_video_restrictions(self):
        """Conservatively migrate only active, explicit legacy video blocks."""
        now = time.time()
        rows = self._conn.execute(
            "SELECT * FROM accounts_meta WHERE video_restricted_detected_at=0 AND "
            "(rate_limited_until>? OR quota_blocked_until>?)", (now, now)
        ).fetchall()
        changed = False
        for row in rows:
            detail = row["limit_reason"] or row["quota_reason"] or ""
            classified = classify_upstream_text(detail)
            if not classified.is_restriction:
                continue
            detected = row["last_used_at"] or now
            self._conn.execute(
                "UPDATE accounts_meta SET video_restricted_detected_at=?, "
                "video_restriction_reason=? WHERE name=?",
                (detected, classified.kind, row["name"]),
            )
            changed = True
        if changed:
            self._conn.commit()

    def mark_restriction(self, account: str, media_type: str, reason: str):
        if media_type not in {"video", "image"}:
            raise ValueError("media_type must be video or image")
        if reason not in {"daily_limit", "credits_unavailable", "risk_control"}:
            raise ValueError("invalid restriction reason")
        self._ensure_meta(account)
        self._conn.execute(
            f"UPDATE accounts_meta SET {media_type}_restricted_detected_at=?, "
            f"{media_type}_restriction_reason=?, last_used_at=? WHERE name=?",
            (time.time(), reason, time.time(), account),
        )
        self._conn.commit()

    def clear_restriction(self, account: str, media_type: str):
        if media_type not in {"video", "image"}:
            raise ValueError("media_type must be video or image")
        self._conn.execute(
            f"UPDATE accounts_meta SET {media_type}_restricted_detected_at=0, "
            f"{media_type}_restriction_reason='' WHERE name=?", (account,)
        )
        self._conn.commit()

    # Legacy internal names retained for callers during the compatibility release.
    def _mark_quota_blocked(self, account: str, reason: str = ""):
        classified = classify_upstream_text(reason)
        if classified.is_restriction:
            self.mark_restriction(account, "video", classified.kind)

    def _mark_daily_limit(self, account: str, reason: str = ""):
        self.mark_restriction(account, "video", "daily_limit")

    def list_accounts(self) -> list:
        """Dashboard view: combines metadata, quota, and busy status."""
        out = []
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            out.append({
                "name": a,
                "scheduling": bool(m["scheduling"]) if m else True,
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "login_ok": m["login_ok"] if m else None,
                "login_checked_at": m["login_checked_at"] if m else 0,
                "cooldown_until": m["cooldown_until"] if m else 0,
                "cooling": False,
                "rate_limited_until": m["rate_limited_until"] if m and m["rate_limited_until"] else 0,
                "rate_limited": False,
                "limit_reason": m["limit_reason"] if m else "",
                "quota_blocked_until": m["quota_blocked_until"] if m and m["quota_blocked_until"] else 0,
                "quota_blocked": False,
                "quota_reason": m["quota_reason"] if m else "",
                "video_restricted_detected_at": m["video_restricted_detected_at"] if m else 0,
                "video_restriction_reason": m["video_restriction_reason"] if m else "",
                "video_restricted": bool(m and m["video_restricted_detected_at"]),
                "image_restricted_detected_at": m["image_restricted_detected_at"] if m else 0,
                "image_restriction_reason": m["image_restriction_reason"] if m else "",
                "image_restricted": bool(m and m["image_restricted_detected_at"]),
                "used_today": used,
                # Dola currently exposes neither a dependable credit balance nor
                # a numeric image/video quota to the web client.
                "quota_status": (
                    "blocked"
                    if m and (m["video_restricted_detected_at"] or m["image_restricted_detected_at"])
                    else "unavailable"
                ),
                "limit": None,
                "remaining": None,
                "busy": bool(lock and lock.locked()),
            })
        return out

    def set_scheduling(self, name: str, on: bool):
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=? WHERE name=?", (1 if on else 0, name))
        self._conn.commit()

    def set_email(self, name: str, email: str):
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_login_status(self, name: str, ok: bool):
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()

    def set_note(self, name: str, note: str):
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def delete_account(self, name: str):
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("Account is currently in use and cannot be removed")
        profile = self.accounts_dir / name

        def retry_readonly(function, path, _error):
            Path(path).chmod(stat.S_IRWXU)
            function(path)

        if profile.is_symlink():
            profile.unlink()
        elif profile.exists():
            # Chromium can leave read-only cache entries on Windows. Retrying
            # those entries as owner-writable makes account removal portable.
            shutil.rmtree(profile, onerror=retry_readonly)
        with self._conn:
            self._conn.execute("DELETE FROM usage WHERE account=?", (name,))
            self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._locks.pop(name, None)

    async def verify_account(self, name: str) -> bool:
        """Verifies login state in headless mode and updates cache."""
        if name not in self.accounts:
            raise FileNotFoundError(f"Profile does not exist: {name}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Account is generating video, please verify later")
        from .browser import check_login_state
        ok = await check_login_state(name)
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()
        return ok

    # ===== Scheduling =====

    def _schedulable(self, a: dict) -> bool:
        return (a["scheduling"] and bool(a["login_ok"])
                and not a["video_restricted"])

    def _image_schedulable(self, account: dict) -> bool:
        return (
            account["scheduling"]
            and bool(account["login_ok"])
            and not account["image_restricted"]
        )

    @property
    def all_accounts_limited(self) -> bool:
        """Returns True only when Dola reported a limit for every candidate."""
        candidates = [a for a in self.list_accounts() if a["scheduling"]]
        return bool(candidates) and all(a["video_restricted"] for a in candidates)

    @property
    def all_accounts_quota_blocked(self) -> bool:
        return self.all_accounts_limited

    @property
    def available(self) -> bool:
        return any(self._schedulable(a) for a in self.list_accounts())

    @property
    def available_for_image(self) -> bool:
        return any(self._image_schedulable(a) for a in self.list_accounts())

    @property
    def cookie_count(self) -> int:  # /health compatibility
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "remaining": a["remaining"], "quota_status": a["quota_status"],
            "login_ok": a["login_ok"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
            "video_restricted": a["video_restricted"],
            "video_restricted_detected_at": a["video_restricted_detected_at"],
            "video_restriction_reason": a["video_restriction_reason"],
            "image_restricted": a["image_restricted"],
            "image_restricted_detected_at": a["image_restricted_detected_at"],
            "image_restriction_reason": a["image_restriction_reason"],
        } for a in self.list_accounts()]

    def validate_manual_retry(self, account: str, media_type: str, *, require_idle: bool = True):
        if account not in self.accounts:
            raise FileNotFoundError(f"Account does not exist: {account}")
        row = next(item for item in self.list_accounts() if item["name"] == account)
        if not row["scheduling"]:
            raise ValueError("Account dispatch is disabled")
        if not row["login_ok"]:
            raise ValueError("Account is not logged in")
        if not row[f"{media_type}_restricted"]:
            raise ValueError(f"Account has no {media_type} restriction to retry")
        lock = self._locks.setdefault(account, asyncio.Lock())
        if require_idle and lock.locked():
            raise RuntimeError("Account is busy")
        return row

    @staticmethod
    def _upstream_error(exc: Exception, *, pre_generation: bool = True) -> Exception:
        if isinstance(exc, (PromptContentRejectedError,
                            DolaTemporarilyUnavailableError,
                            ExplicitRestrictionError)):
            return exc
        return error_from_upstream_text(exc, pre_generation=pre_generation)

    async def generate_image(
        self,
        prompt: str,
        ratio: str = "1:1",
        style: str = "auto",
        on_start=None,
        retry_account: str | None = None,
    ) -> dict:
        """Generate an image; a manual retry pins one restricted account once."""
        async with self.semaphore:
            while True:
                if retry_account:
                    eligible = [self.validate_manual_retry(retry_account, "image")]
                else:
                    eligible = [
                        item for item in self.list_accounts()
                        if self._image_schedulable(item)
                    ]
                if not eligible:
                    raise RuntimeError(
                        "No verified image-generation account is available"
                    )

                selected = None
                for item in eligible:
                    lock = self._locks.setdefault(item["name"], asyncio.Lock())
                    if not lock.locked():
                        selected = (item["name"], lock)
                        break

                if selected is None:
                    if retry_account:
                        # A deliberate retry is never queued behind another use of
                        # the pinned account: fail without contacting Dola.
                        raise RuntimeError("Account is busy")
                    # Video and image tasks share account locks. The timeout also
                    # observes releases from legacy video paths that do not notify.
                    async with self._account_available:
                        try:
                            await asyncio.wait_for(
                                self._account_available.wait(), timeout=0.5
                            )
                        except TimeoutError:
                            pass
                    continue

                account, lock = selected
                await lock.acquire()
                try:
                    current = next(
                        row for row in self.list_accounts() if row["name"] == account
                    )
                    if retry_account:
                        self.validate_manual_retry(account, "image", require_idle=False)
                    elif not self._image_schedulable(current):
                        continue
                    if on_start:
                        on_start(account)
                    try:
                        result = await generate_image_for_account(
                            account, prompt, ratio, style
                        )
                    except LoginExpiredError:
                        self.set_login_status(account, False)
                        if retry_account:
                            raise
                        continue
                    except (ImageGenerationError, ExplicitRestrictionError,
                            PromptContentRejectedError,
                            DolaTemporarilyUnavailableError) as exc:
                        classified = self._upstream_error(exc)
                        if isinstance(classified, ExplicitRestrictionError):
                            self.mark_restriction(account, "image", classified.kind)
                            if not retry_account and classified.pre_generation:
                                continue
                        raise classified
                    except TimeoutError as exc:
                        raise DolaTemporarilyUnavailableError(
                            "Dola is temporarily unavailable. The image request timed out; "
                            "the account restriction was preserved."
                        ) from exc
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account),
                    )
                    self._claim(account)
                    self._conn.commit()
                    if retry_account:
                        self.clear_restriction(account, "image")
                    return result
                finally:
                    lock.release()
                    async with self._account_available:
                        self._account_available.notify_all()

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None) -> dict:
        """Resumes an accepted session without re-scheduling."""
        async with self.semaphore:
            lock = self._locks.setdefault(account, asyncio.Lock())
            async with lock:
                try:
                    result = await resume_video(account, conversation_id, timeout,
                                                on_poll=on_poll)
                    self._claim(account)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except TimeoutError as exc:
                    self._claim(account)
                    self._conn.commit()
                    raise DolaTemporarilyUnavailableError(
                        "Dola is temporarily unavailable. The accepted video did not "
                        "finish before the timeout."
                    ) from exc
                except (ExplicitRestrictionError, PromptContentRejectedError,
                        DolaTemporarilyUnavailableError) as exc:
                    classified = self._upstream_error(exc, pre_generation=False)
                    if isinstance(classified, ExplicitRestrictionError):
                        self.mark_restriction(account, "video", classified.kind)
                    raise classified

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None,
                             reference_image_paths: list[str] | None = None,
                             retry_account: str | None = None) -> dict:
        """Generate video; explicit pre-submit restrictions alone may rotate."""
        async with self.semaphore:
            last_err = None
            candidates = ([self.validate_manual_retry(retry_account, "video")]
                          if retry_account else self.list_accounts())
            for a in candidates:
                if not retry_account and not self._schedulable(a):
                    continue
                account = a["name"]
                lock = self._locks.setdefault(account, asyncio.Lock())
                # Skip busy accounts to prevent concurrent collisions on same profile.
                if lock.locked():
                    continue
                async with lock:
                    if retry_account:
                        self.validate_manual_retry(account, "video", require_idle=False)
                    elif not self._schedulable(next(x for x in self.list_accounts() if x['name'] == account)):
                        continue  # State changed while waiting
                    try:
                        result = await generate_video(
                            account, prompt, ratio, duration, model=model,
                            on_conversation_id=on_conversation_id, on_poll=on_poll,
                            reference_image_paths=reference_image_paths)
                        self._claim(account)
                        self._conn.execute(
                            "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                            (time.time(), account))
                        self._conn.commit()
                        if retry_account:
                            self.clear_restriction(account, "video")
                        return result
                    except (ExplicitRestrictionError, PromptContentRejectedError,
                            DolaTemporarilyUnavailableError) as exc:
                        classified = self._upstream_error(exc)
                        if isinstance(classified, ExplicitRestrictionError):
                            self.mark_restriction(account, "video", classified.kind)
                            last_err = classified
                            if not retry_account and classified.pre_generation:
                                continue
                        raise classified
                    except RuntimeError as exc:
                        classified = self._upstream_error(exc)
                        if isinstance(classified, ExplicitRestrictionError):
                            self.mark_restriction(account, "video", classified.kind)
                            last_err = classified
                            if not retry_account and classified.pre_generation:
                                continue
                        raise classified
                    except TimeoutError as e:
                        # Once conversation_id is assigned, task continues on Dola side;
                        # do not re-submit because Dola may already be processing it.
                        self._claim(account)
                        self._conn.execute(
                            "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                            (time.time(), account))
                        self._conn.commit()
                        raise DolaTemporarilyUnavailableError(
                            "Dola is temporarily unavailable. The video result was not "
                            "available before the timeout; no other account was tried."
                        ) from e
                    except FileNotFoundError as e:
                        print(f"[pool] {account} profile missing, skipping: {e}", flush=True)
                        last_err = e
                        continue
            if self.all_accounts_limited:
                raise AllAccountsLimitedError(
                    f"All schedulable accounts have an explicit video restriction: {last_err or 'No accounts'}"
                )
            raise RuntimeError(f"No available accounts in pool: {last_err or 'No accounts'}")
