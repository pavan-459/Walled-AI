"""Transparent stdio proxy that sits between Claude Desktop and one local MCP server.

Claude Desktop launches each local MCP server as a child process and speaks
newline-delimited JSON-RPC over its stdin/stdout. ``mcp-guard install`` rewrites
claude_desktop_config.json so that Claude launches *this* proxy instead, with the
original command after ``--``. The proxy then launches the real server and relays
traffic in both directions:

    Claude Desktop ──stdin──▶ proxy ──stdin──▶ real MCP server
    Claude Desktop ◀─stdout── proxy ◀─stdout── real MCP server

Every client→server message is inspected. Requests whose method is in
``intercept_methods`` (``tools/call`` by default) are held back while the daemon asks
the user. Each held request is handled in its own task, so other traffic (pings,
tools/list, other tool calls) keeps flowing and simultaneous requests are decided
independently. Allowed requests are forwarded byte-for-byte; blocked ones never
reach the server and Claude receives a denial response with the same JSON-RPC id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import threading
from typing import Any

from . import jsonrpc
from .client import DecisionClient
from .config import Settings
from .models import ApprovalRequest

log = logging.getLogger(__name__)

STREAM_LIMIT = 64 * 1024 * 1024  # max single message size (asyncio default is only 64 KiB)


class StdioProxy:
    def __init__(self, server_name: str, command: str, args: list[str], settings: Settings,
                 client: DecisionClient | None = None) -> None:
        self.server_name = server_name
        self.command = command
        self.args = args
        self.settings = settings
        self.client = client or DecisionClient(settings)

        self.proc: asyncio.subprocess.Process | None = None
        self.client_name: str | None = None
        self.client_version: str | None = None
        self.server_info: str | None = None
        self._init_request_id: Any = None
        self._held: dict[str, asyncio.Task] = {}  # JSON-RPC id -> approval task
        self._out_lock = asyncio.Lock()
        self._child_lock = asyncio.Lock()
        self._stdin_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    # ------------------------------------------------------------------ entry point
    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        executable = shutil.which(self.command) or self.command
        try:
            self.proc = await asyncio.create_subprocess_exec(
                executable, *self.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None,  # stderr -> Claude's log
                limit=STREAM_LIMIT,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            log.error("could not start MCP server %r (%s): %s", self.server_name, self.command, exc)
            await self.client.close()
            return 127
        log.info("started %s (pid %s): %s %s", self.server_name, self.proc.pid, self.command, " ".join(self.args))

        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                try:
                    loop.add_signal_handler(sig, self._on_signal)
                except (NotImplementedError, RuntimeError):
                    pass

        threading.Thread(target=self._read_stdin, args=(loop,), name="stdin-reader", daemon=True).start()
        upstream = asyncio.create_task(self._pump_client_to_server(), name="client->server")
        downstream = asyncio.create_task(self._pump_server_to_client(), name="server->client")
        child_exit = asyncio.create_task(self.proc.wait(), name="child-exit")

        done, _ = await asyncio.wait({upstream, downstream, child_exit}, return_when=asyncio.FIRST_COMPLETED)
        if upstream in done and child_exit not in done:
            log.info("client closed stdin; shutting down server")
            await self._cancel_held()
            await self._stop_child()
        else:
            log.info("server exited")
            await self._cancel_held()

        # Give the server's final output a moment to drain, then tidy up.
        try:
            await asyncio.wait_for(asyncio.shield(downstream), timeout=2)
        except (asyncio.TimeoutError, Exception):
            pass
        for task in (upstream, downstream, child_exit):
            task.cancel()
        await self.client.close()
        code = self.proc.returncode if self.proc.returncode is not None else 0
        log.info("proxy exiting with code %s", code)
        return code

    def _on_signal(self) -> None:
        log.info("signal received; stopping")
        self._stdin_queue.put_nowait(None)  # behaves like the client closing stdin

    # ------------------------------------------------------------------ client -> server
    def _read_stdin(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking stdin reads on a thread (portable: async stdin pipes don't work on Windows)."""
        stdin = sys.stdin.buffer
        try:
            while True:
                line = stdin.readline()
                if not line:
                    break
                loop.call_soon_threadsafe(self._stdin_queue.put_nowait, line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                loop.call_soon_threadsafe(self._stdin_queue.put_nowait, None)
            except RuntimeError:  # loop already closed
                pass

    async def _pump_client_to_server(self) -> None:
        while True:
            line = await self._stdin_queue.get()
            if line is None:
                return
            if not line.strip():
                continue
            try:
                await self._handle_client_line(line)
            except Exception:
                log.exception("error handling client message; forwarding unchanged")
                await self._write_child(line)

    async def _handle_client_line(self, raw: bytes) -> None:
        message = jsonrpc.parse(raw)
        if message is None:  # not JSON: let the server produce the parse error
            await self._write_child(raw)
            return

        info = jsonrpc.client_info(message)
        if info is not None:
            self.client_name = info.get("name")
            self.client_version = info.get("version")
            self._init_request_id = message.get("id")
            log.info("client: %s %s", self.client_name, self.client_version or "")

        cancelled = jsonrpc.cancelled_request_id(message)
        if cancelled is not None:
            task = self._held.get(_key(cancelled))
            if task is not None:
                # The server never saw this request, so swallow the notification too.
                log.info("client cancelled held request %s", cancelled)
                task.cancel()
                return

        if isinstance(message, list):
            if any(jsonrpc.needs_approval(m, self.settings.intercept_methods) for m in message):
                asyncio.create_task(self._handle_batch(message))
                return
        elif jsonrpc.needs_approval(message, self.settings.intercept_methods):
            key = _key(message["id"])
            if key in self._held:  # duplicate in-flight id: a client bug; reject rather than guess
                await self._write_client(jsonrpc.dumps_line({
                    "jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32600, "message": "duplicate request id"}}))
                return
            self._held[key] = asyncio.create_task(self._hold(message, raw), name=f"hold-{key}")
            return

        await self._write_child(raw)

    def _approval_request(self, message: dict[str, Any]) -> ApprovalRequest:
        meta_info = jsonrpc.meta_client_info(message) or {}
        return ApprovalRequest(
            server_name=self.server_name, method=message["method"], params=message.get("params"),
            jsonrpc_id=message.get("id"), source="stdio",
            client_name=self.client_name or meta_info.get("name"),
            client_version=self.client_version or meta_info.get("version"),
            server_info=self.server_info,
        )

    async def _hold(self, message: dict[str, Any], raw: bytes) -> None:
        """Wait for the user's decision on one request, then forward or deny it."""
        key = _key(message["id"])
        request = self._approval_request(message)
        try:
            decision = await self.client.request_approval(request)
        except asyncio.CancelledError:
            await self.client.cancel(request.id)  # closes the popup
            self._held.pop(key, None)
            raise
        try:
            if decision.allowed:
                log.info("ALLOW %s %s", request.method, request.target)
                await self._write_child(raw)
            else:
                log.info("BLOCK %s %s (%s)", request.method, request.target, decision.reason.value)
                denial = jsonrpc.denial_response(message, decision, self.settings.block_response)
                await self._write_client(jsonrpc.dumps_line(denial))
        finally:
            self._held.pop(key, None)

    async def _handle_batch(self, batch: list[Any]) -> None:
        """JSON-RPC batches (protocol 2025-03-26) are approved item by item."""
        methods = self.settings.intercept_methods
        held = [m for m in batch if jsonrpc.needs_approval(m, methods)]
        decisions = await asyncio.gather(*(self.client.request_approval(self._approval_request(m)) for m in held))
        verdicts = {id(m): d for m, d in zip(held, decisions)}
        forward = [m for m in batch if id(m) not in verdicts or verdicts[id(m)].allowed]
        denials = [jsonrpc.denial_response(m, verdicts[id(m)], self.settings.block_response)
                   for m in held if not verdicts[id(m)].allowed]
        if forward:
            await self._write_child(jsonrpc.dumps_line(forward))
        if denials:
            await self._write_client(jsonrpc.dumps_line(denials))

    # ------------------------------------------------------------------ server -> client
    async def _pump_server_to_client(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.error("server sent a message larger than %d bytes; dropping it", STREAM_LIMIT)
                continue
            if not line:
                return
            if self._init_request_id is not None:
                self._capture_server_info(line)
            await self._write_client(line)

    def _capture_server_info(self, line: bytes) -> None:
        message = jsonrpc.parse(line)
        if isinstance(message, dict) and message.get("id") == self._init_request_id and "result" in message:
            info = (message.get("result") or {}).get("serverInfo") or {}
            if info:
                self.server_info = f"{info.get('name', '?')} {info.get('version', '')}".strip()
            self._init_request_id = None

    # ------------------------------------------------------------------ I/O helpers
    async def _write_client(self, data: bytes) -> None:
        async with self._out_lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._stdin_queue.put_nowait(None)  # client is gone

    async def _write_child(self, data: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        async with self._child_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                log.warning("could not write to server: %s", exc)

    async def _cancel_held(self) -> None:
        tasks = list(self._held.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_child(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except (asyncio.TimeoutError, OSError):
            pass
        for action in (proc.terminate, proc.kill):
            try:
                action()
                await asyncio.wait_for(proc.wait(), timeout=3)
                return
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                continue


def _key(jsonrpc_id: Any) -> str:
    return json.dumps(jsonrpc_id, sort_keys=True)


def run_proxy(server_name: str, command: list[str]) -> int:
    if not command:
        print("mcp-guard proxy: missing server command after --", file=sys.stderr)
        return 2
    settings = Settings.load()
    proxy = StdioProxy(server_name, command[0], command[1:], settings)
    try:
        code = asyncio.run(proxy.run())
    except KeyboardInterrupt:
        code = 130
    # The stdin reader thread may still be blocked inside readline(). A normal interpreter
    # shutdown would then abort ("could not acquire lock for <stdin>"), so flush and exit hard.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    logging.shutdown()
    os._exit(code)
