"""The approval daemon: a background process that owns the UI and every pending decision.

Interceptors (one stdio proxy per MCP server, plus optional HTTP gateways) are
separate processes. Each intercepted request is POSTed here and the HTTP call simply
stays open until the user decides, the request times out, or the interceptor cancels
it. Keeping all decisions in one process means one place shows popups, enforces
timeouts and limits, and writes the audit log.

HTTP API (127.0.0.1 only, bearer-token protected):

    POST   /v1/shutdown           stop the daemon gracefully
    POST   /v1/approvals          body: ApprovalRequest  -> Decision (long-poll)
    DELETE /v1/approvals/{id}     cancel a pending request
    GET    /v1/approvals          list pending requests
    GET    /v1/health             liveness + pending count
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from . import __version__
from .audit import AuditLog
from .config import Settings, audit_path, clear_runtime, load_or_create_token, write_runtime
from .models import ApprovalRequest, Decision, Reason, Verdict
from .ui.base import ApprovalUI

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024 * 1024


class AlreadyRunning(RuntimeError):
    pass


@dataclass
class _Pending:
    request: ApprovalRequest
    future: "asyncio.Future[Decision]"
    deadline: float


class GuardDaemon:
    def __init__(self, settings: Settings, ui: ApprovalUI, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.settings = settings
        self.ui = ui
        self.host = host
        self.port = settings.daemon_port if port is None else port
        self.token = load_or_create_token()
        self.audit = AuditLog(audit_path(), settings.audit_arguments, settings.audit_max_chars)
        self.pending: dict[str, _Pending] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._stop = threading.Event()
        self.ui.bind(self._decide_threadsafe)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> int:
        """Start the HTTP API; returns the bound port and writes daemon.json."""
        self.loop = asyncio.get_running_loop()
        app = web.Application(middlewares=[self._security_middleware], client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/v1/health", self._health)
        app.router.add_get("/v1/approvals", self._list)
        app.router.add_post("/v1/approvals", self._approve)
        app.router.add_delete("/v1/approvals/{request_id}", self._cancel)
        app.router.add_post("/v1/shutdown", self._shutdown)
        # handler_cancellation: if an interceptor dies mid-wait, its handler is cancelled
        # and we close the popup instead of leaving an orphan on screen.
        self._runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        server = site._server  # aiohttp exposes the bound sockets only here
        self.port = server.sockets[0].getsockname()[1] if server and server.sockets else self.port
        write_runtime({"pid": os.getpid(), "port": self.port, "host": self.host,
                       "started_at": time.time(), "version": __version__})
        log.info("daemon listening on %s:%s (pid %s)", self.host, self.port, os.getpid())
        return self.port

    async def stop(self) -> None:
        """Block everything still pending, stop the API and remove daemon.json."""
        for pending in list(self.pending.values()):
            self._resolve(pending.request.id, Decision.block(Reason.SHUTDOWN))
        await asyncio.sleep(0.05)  # let handlers flush their responses
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        clear_runtime(os.getpid())
        log.info("daemon stopped")

    def run(self) -> None:
        """Blocking entry point used by ``mcp-guard daemon``.

        Tk must own the main thread, so with a Tk UI the asyncio server runs in a
        worker thread; otherwise everything runs on the main thread.
        """
        stop = self._stop

        def request_stop(*_args: Any) -> None:
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, request_stop)
            except (ValueError, OSError):
                pass
        if hasattr(signal, "SIGBREAK"):  # Ctrl+Break on Windows
            signal.signal(signal.SIGBREAK, request_stop)  # type: ignore[attr-defined]

        loop = asyncio.new_event_loop()
        started = threading.Event()
        failure: list[BaseException] = []

        def serve() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.start())
            except BaseException as exc:  # e.g. port in use
                failure.append(exc)
                started.set()
                stop.set()
                return
            started.set()
            loop.run_forever()
            loop.run_until_complete(self.stop())
            loop.close()

        worker = threading.Thread(target=serve, name="guard-api", daemon=True)
        worker.start()
        started.wait()
        if failure:
            raise failure[0]
        try:
            if self.ui.needs_main_thread:
                self.ui.run(stop)  # returns when stop is set
            else:
                while not stop.wait(0.5):
                    pass
        finally:
            stop.set()
            loop.call_soon_threadsafe(loop.stop)
            worker.join(timeout=5)
            self.ui.close()

    # ------------------------------------------------------------------ decisions
    def _decide_threadsafe(self, request_id: str, verdict: Verdict) -> None:
        """Called by the UI (any thread) when the user clicks Allow / Block."""
        if self.loop is None or self.loop.is_closed():
            return
        decision = Decision.allow() if verdict is Verdict.ALLOW else Decision.block(Reason.USER)
        self.loop.call_soon_threadsafe(self._resolve, request_id, decision)

    def _resolve(self, request_id: str, decision: Decision) -> bool:
        pending = self.pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(decision)
        return True

    async def decide(self, request: ApprovalRequest) -> Decision:
        """Register ``request``, show it, and wait for the outcome."""
        if request.id in self.pending:
            return Decision.block(Reason.ERROR, "duplicate request id")
        if len(self.pending) >= self.settings.max_pending:
            decision = Decision.block(Reason.OVERLOADED)
            self.audit.record(request, decision)
            return decision

        assert self.loop is not None
        deadline = time.time() + self.settings.decision_timeout
        future: asyncio.Future[Decision] = self.loop.create_future()
        self.pending[request.id] = _Pending(request, future, deadline)
        log.info("pending %s: %s %s/%s", request.id[:8], request.method, request.server_name, request.target)
        try:
            self.ui.show(request, deadline)
        except Exception:
            log.exception("UI failed to show request")
            self._resolve(request.id, Decision.block(Reason.ERROR, "could not display the approval prompt"))

        try:
            decision = await asyncio.wait_for(asyncio.shield(future), self.settings.decision_timeout)
        except asyncio.TimeoutError:
            decision = Decision.block(Reason.TIMEOUT)
        except asyncio.CancelledError:
            # The interceptor disconnected while waiting.
            decision = Decision.block(Reason.CANCELLED, "client disconnected")
            self._finish(request, decision)
            raise
        self._finish(request, decision)
        return decision

    def _finish(self, request: ApprovalRequest, decision: Decision) -> None:
        self.pending.pop(request.id, None)
        if decision.reason is not Reason.USER:
            self.ui.dismiss(request.id, decision.reason.value)
        self.audit.record(request, decision)
        log.info("decided %s: %s (%s)", request.id[:8], decision.verdict.value, decision.reason.value)

    # ------------------------------------------------------------------ HTTP
    @web.middleware
    async def _security_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        # 1. Browsers always send Origin on cross-site requests; legitimate clients never do.
        #    Rejecting it blocks drive-by requests from web pages to this localhost port.
        if request.headers.get("Origin"):
            return web.json_response({"error": "forbidden"}, status=403)
        # 2. Host must be loopback (defeats DNS-rebinding).
        host = (request.host or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return web.json_response({"error": "forbidden host"}, status=403)
        # 3. Shared secret from the 0600 token file.
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), self.token.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "pid": os.getpid(), "pending": len(self.pending),
                                  "version": __version__})

    async def _list(self, _request: web.Request) -> web.Response:
        items = [
            {"id": p.request.id, "server": p.request.server_name, "method": p.request.method,
             "target": p.request.target, "seconds_left": max(0, round(p.deadline - time.time()))}
            for p in self.pending.values()
        ]
        return web.json_response({"pending": items})

    async def _approve(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            approval = ApprovalRequest.from_dict(data)
        except Exception as exc:
            return web.json_response({"error": f"bad request: {exc}"}, status=400)
        decision = await self.decide(approval)
        return web.json_response(decision.to_dict())

    async def _shutdown(self, _request: web.Request) -> web.Response:
        """Graceful stop requested by ``mcp-guard stop`` (works the same on every OS)."""
        self._stop.set()
        return web.json_response({"stopping": True})

    async def _cancel(self, request: web.Request) -> web.Response:
        request_id = request.match_info["request_id"]
        cancelled = self._resolve(request_id, Decision.block(Reason.CANCELLED, "cancelled by client"))
        return web.json_response({"cancelled": cancelled})


def configure_logging(name: str, verbose: bool = False) -> None:
    """Log to a file in the MCP Guard directory and to stderr (never stdout: it's the MCP channel)."""
    from .config import log_path

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(log_path(name), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=f"%(asctime)s [{name}] %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
