"""Approval gateway for *remote* MCP servers (Streamable HTTP transport).

Why this exists: when you add a custom connector in claude.ai (or in Claude Desktop's
Connectors settings), the MCP traffic does not leave your computer at all. Anthropic's
cloud connects to the server URL directly. A local proxy therefore cannot see it.
The only place the traffic can be intercepted is on the path to the MCP server, so
this gateway is a reverse proxy you put in front of the real server:

    claude.ai ─▶ Anthropic cloud ─▶ https://<tunnel>/mcp ─▶ mcp-guard gateway ─▶ real MCP server
                                                              │
                                                              └─▶ daemon ─▶ popup on your screen

You register the gateway's public URL (e.g. from ``cloudflared tunnel --url``) as the
connector URL instead of the real server's. JSON-RPC requests that need approval are
held until you decide; blocked ones get a JSON-RPC response from the gateway and
never reach the server. Everything else, including SSE streams, session headers and
Authorization headers, is passed through unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal
import sys
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from . import jsonrpc
from .client import DecisionClient
from .config import Settings
from .models import ApprovalRequest, Decision

log = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length",
}
SESSION_HEADER = "Mcp-Session-Id"


class HttpGateway:
    def __init__(self, upstream_url: str, server_name: str, settings: Settings,
                 secret_prefix: str | None = None, client: DecisionClient | None = None) -> None:
        parts = urlsplit(upstream_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"upstream must be an http(s) URL, got {upstream_url!r}")
        self.upstream_origin = f"{parts.scheme}://{parts.netloc}"
        self.upstream_path = parts.path or "/"
        self.server_name = server_name
        self.settings = settings
        self.secret_prefix = secret_prefix.strip("/") if secret_prefix else None
        self.client = client or DecisionClient(settings)
        self._session: aiohttp.ClientSession | None = None
        self._client_by_session: dict[str, tuple[str | None, str | None]] = {}
        self._last_client: tuple[str | None, str | None] = (None, None)

    @property
    def mount_path(self) -> str:
        return (f"/{self.secret_prefix}" if self.secret_prefix else "") + self.upstream_path

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        app.on_cleanup.append(self._cleanup)
        return app

    async def _cleanup(self, _app: web.Application) -> None:
        if self._session is not None:
            await self._session.close()
        await self.client.close()

    def _upstream_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # auto_decompress=False: pass bodies through byte-for-byte (Content-Encoding stays valid).
            # trust_env=True: honour HTTPS_PROXY etc. on corporate networks.
            self._session = aiohttp.ClientSession(auto_decompress=False, trust_env=True)
        return self._session

    # ------------------------------------------------------------------ request handling
    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.rel_url.path
        if self.secret_prefix:
            prefix = f"/{self.secret_prefix}"
            if path != prefix and not path.startswith(prefix + "/"):
                return web.Response(status=404)
            path = path[len(prefix):] or "/"
        target = self.upstream_origin + path + (f"?{request.rel_url.query_string}" if request.rel_url.query_string else "")
        body = await request.read()

        message = jsonrpc.parse(body) if request.method == "POST" and body else None
        info = jsonrpc.client_info(message) if message is not None else None
        if info is not None:
            self._last_client = (info.get("name"), info.get("version"))

        held = self._held_messages(message)
        if held:
            decisions = await self._decide_all(held, request)
            if decisions is None:  # client went away while we were waiting
                return web.Response(status=499)
            blocked = [(m, d) for m, d in zip(held, decisions) if not d.allowed]
            if blocked:
                return self._deny(message, held, blocked)

        return await self._forward(request, target, body, initialize=info is not None)

    def _held_messages(self, message: Any) -> list[dict[str, Any]]:
        methods = self.settings.intercept_methods
        if isinstance(message, list):
            return [m for m in message if jsonrpc.needs_approval(m, methods)]
        if jsonrpc.needs_approval(message, methods):
            return [message]
        return []

    async def _decide_all(self, held: list[dict[str, Any]], request: web.Request) -> list[Decision] | None:
        client_name, client_version = self._client_by_session.get(
            request.headers.get(SESSION_HEADER, ""), self._last_client)
        approvals = [
            ApprovalRequest(server_name=self.server_name, method=m["method"], params=m.get("params"),
                            jsonrpc_id=m.get("id"), source="http",
                            client_name=client_name or (jsonrpc.meta_client_info(m) or {}).get("name"),
                            client_version=client_version or (jsonrpc.meta_client_info(m) or {}).get("version"))
            for m in held
        ]
        task = asyncio.ensure_future(asyncio.gather(*(self.client.request_approval(a) for a in approvals)))
        # Watch for the HTTP client disconnecting (Claude gave up / user stopped the turn).
        while not task.done():
            await asyncio.wait({task}, timeout=0.5)
            transport = request.transport
            if not task.done() and (transport is None or transport.is_closing()):
                task.cancel()
                await asyncio.gather(*(self.client.cancel(a.id) for a in approvals), return_exceptions=True)
                log.info("client disconnected while %d request(s) awaited approval", len(approvals))
                return None
        return task.result()

    def _deny(self, message: Any, held: list[dict[str, Any]],
              blocked: list[tuple[dict[str, Any], Decision]]) -> web.Response:
        style = self.settings.block_response
        for m, d in blocked:
            log.info("BLOCK %s %s (%s)", m.get("method"), (m.get("params") or {}).get("name"), d.reason.value)
        if isinstance(message, list):
            # One blocked item blocks the whole batch: partial forwarding would need the
            # gateway to merge upstream SSE/JSON responses, which isn't worth the risk.
            by_message = {id(m): d for m, d in blocked}
            sibling = Decision.block(blocked[0][1].reason, "another request in the same batch was blocked")
            replies = [jsonrpc.denial_response(m, by_message.get(id(m), sibling), style)
                       for m in message if jsonrpc.is_request(m)]
            return web.json_response(replies)
        return web.json_response(jsonrpc.denial_response(message, blocked[0][1], style))

    async def _forward(self, request: web.Request, target: str, body: bytes, initialize: bool) -> web.StreamResponse:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["X-Forwarded-For"] = request.remote or ""
        headers["X-Forwarded-Proto"] = request.scheme
        headers["X-Forwarded-Host"] = request.host
        try:
            upstream = await self._upstream_session().request(
                request.method, target, headers=headers, data=body or None, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("upstream unreachable: %s", exc)
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": f"upstream unreachable: {exc}"}},
                status=502)

        async with upstream:
            if initialize and SESSION_HEADER in upstream.headers:
                self._client_by_session[upstream.headers[SESSION_HEADER]] = self._last_client
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in upstream.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    response.headers.add(key, value)
            if "Content-Length" in upstream.headers and "Transfer-Encoding" not in upstream.headers:
                response.content_length = int(upstream.headers["Content-Length"])
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():  # streams SSE as it arrives
                    await response.write(chunk)
                await response.write_eof()
            except (ConnectionResetError, aiohttp.ClientError, asyncio.CancelledError) as exc:
                log.debug("stream ended early: %r", exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
        return response


def generate_secret() -> str:
    return secrets.token_urlsafe(18)


def run_gateway(upstream: str, name: str, host: str, port: int, secret: str | None) -> int:
    settings = Settings.load()
    gateway = HttpGateway(upstream, name, settings, secret_prefix=secret)

    async def main() -> None:
        runner = web.AppRunner(gateway.build_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        print(f"MCP Guard gateway for '{name}'\n"
              f"  upstream : {upstream}\n"
              f"  local URL: http://{host}:{port}{gateway.mount_path}\n"
              "Expose it with a tunnel (e.g. `cloudflared tunnel --url http://localhost:%d`) and use\n"
              "<tunnel URL>%s as the custom connector URL in claude.ai." % (port, gateway.mount_path),
              file=sys.stderr, flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await stop.wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0
