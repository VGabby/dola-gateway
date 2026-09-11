"""Desktop-owned Uvicorn entry point with graceful cross-platform shutdown."""

from __future__ import annotations

import ctypes
import os
import threading
import time

import uvicorn

import config
import server


def _parent_is_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_parent(uvicorn_server: uvicorn.Server, parent_pid: int) -> None:
    while not uvicorn_server.should_exit:
        if not _parent_is_alive(parent_pid):
            uvicorn_server.should_exit = True
            return
        time.sleep(1.0)


def main() -> None:
    if not config.DESKTOP_MODE:
        raise SystemExit("desktop_entry requires DOLA_DESKTOP_TOKEN")
    uvicorn_config = uvicorn.Config(
        server.app,
        host=config.HOST,
        port=config.PORT,
        access_log=False,
        log_level="info",
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)
    server.DESKTOP_UVICORN_SERVER = uvicorn_server
    parent_pid = int(os.getenv("DOLA_PARENT_PID", "0") or "0")
    if parent_pid:
        threading.Thread(
            target=_watch_parent,
            args=(uvicorn_server, parent_pid),
            name="dola-parent-watchdog",
            daemon=True,
        ).start()
    uvicorn_server.run()


if __name__ == "__main__":
    main()
