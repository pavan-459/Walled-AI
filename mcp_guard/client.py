"""Client side of the daemon API, used by the stdio proxy and the HTTP gateway.

Failure policy lives here: if the daemon can't be reached (not running, crashed,
wrong token) the request is *blocked* unless the user set ``fail_mode: "open"``.
A guard that silently allows everything when it breaks is not a guard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import aiohttp

from .config import Settings, guard_home, read_runtime, read_token
from .models import ApprovalRequest, Decision, Reason

log = logging.getLogger(__name__)


class DecisionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._start_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _endpoint(self) -> tuple[str, dict[str, str]] | None:
        # Re-read every time: the daemon may have restarted on a different port.
        runtime, token = read_runtime(), read_token()
        if not runtime or not token:
            return None
        return f"http://127.0.0.1:{runtime['port']}", {"Authorization": f"Bearer {token}"}

    def _unavailable(self, detail: str) -> Decision:
        if self.settings.fail_mode == "open":
            log.warning("daemon unavailable (%s); fail_mode=open so allowing", detail)
            return Decision.allow(Reason.DAEMON_UNAVAILABLE, detail)
        log.warning("daemon unavailable (%s); blocking", detail)
        return Decision.block(Reason.DAEMON_UNAVAILABLE, detail)

    async def request_approval(self, request: ApprovalRequest) -> Decision:
        """Ask the daemon (and therefore the user) about ``request``. Never raises."""
        timeout = aiohttp.ClientTimeout(total=self.settings.decision_timeout + 30, connect=3)
        last_error = "not running"
        for attempt in range(2):
            endpoint = self._endpoint()
            if endpoint is None and self.settings.auto_start_daemon:
                async with self._start_lock:
                    if self._endpoint() is None:
                        await asyncio.to_thread(ensure_daemon, self.settings)
                endpoint = self._endpoint()
            if endpoint is None:
                return self._unavailable("daemon is not running")
            base, headers = endpoint
            try:
                async with self._session_get().post(
                    f"{base}/v1/approvals", json=request.to_dict(), headers=headers, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        return self._unavailable(f"daemon replied HTTP {resp.status}")
                    return Decision.from_dict(await resp.json())
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0 and self.settings.auto_start_daemon:
                    # Stale daemon.json (daemon crashed). Try to bring one up once.
                    async with self._start_lock:
                        await asyncio.to_thread(ensure_daemon, self.settings)
                    continue
            except Exception as exc:  # malformed response etc.
                last_error = f"{type(exc).__name__}: {exc}"
                break
        return self._unavailable(last_error)

    async def cancel(self, request_id: str) -> None:
        """Tell the daemon to drop a pending request (closes its popup). Best effort."""
        endpoint = self._endpoint()
        if endpoint is None:
            return
        base, headers = endpoint
        try:
            async with self._session_get().delete(
                f"{base}/v1/approvals/{request_id}", headers=headers, timeout=aiohttp.ClientTimeout(total=3)
            ):
                pass
        except Exception as exc:
            log.debug("cancel failed: %s", exc)


# ---------------------------------------------------------------------- sync helpers
def daemon_health(timeout: float = 1.5) -> dict[str, Any] | None:
    """Return the daemon's /v1/health payload, or None if it isn't reachable."""
    runtime, token = read_runtime(), read_token()
    if not runtime or not token:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{runtime['port']}/v1/health", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def spawn_daemon(ui: str) -> subprocess.Popen:
    """Launch ``mcp-guard daemon`` detached from the current process / console."""
    python = sys.executable
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": os.environ.copy(),
    }
    if sys.platform == "win32":
        # pythonw.exe has no console window; DETACHED_PROCESS keeps it alive after we exit.
        pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(pythonw):
            python = pythonw
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([python, "-m", "mcp_guard", "daemon", "--ui", ui], **kwargs)


def ensure_daemon(settings: Settings, wait: float = 8.0) -> bool:
    """Make sure a daemon is running, starting one if needed. Safe to call concurrently
    from several proxies: a lock file ensures only one of them spawns the daemon."""
    if daemon_health():
        return True
    lock = guard_home() / "daemon.start.lock"
    owner = False
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        owner = True
    except FileExistsError:
        # Someone else is starting it. Treat locks older than 30 s as stale.
        try:
            if time.time() - lock.stat().st_mtime > 30:
                lock.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        if owner:
            log.info("starting MCP Guard daemon (ui=%s)", settings.ui)
            spawn_daemon(settings.ui)
        deadline = time.time() + wait
        while time.time() < deadline:
            if daemon_health():
                return True
            time.sleep(0.2)
        return False
    finally:
        if owner:
            lock.unlink(missing_ok=True)
