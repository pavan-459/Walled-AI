from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_guard.config import Settings
from mcp_guard.daemon import GuardDaemon
from mcp_guard.models import ApprovalRequest, Verdict
from mcp_guard.ui.auto import AutoUI

FAKE_SERVER = str(Path(__file__).with_name("fake_server.py"))


def by_prefix(request: ApprovalRequest) -> Verdict | None:
    """Test policy: tool names starting allow_/block_ decide; hold_ never decides."""
    name = request.target
    if name.startswith("allow_"):
        return Verdict.ALLOW
    if name.startswith("block_"):
        return Verdict.BLOCK
    return None


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MCP_GUARD_HOME", str(tmp_path))
    write_settings(tmp_path)
    return tmp_path


def write_settings(home: Path, **overrides: Any) -> Settings:
    data = {"auto_start_daemon": False, "decision_timeout": 10}
    data.update(overrides)
    (home / "config.json").write_text(json.dumps(data))
    return Settings.load()


@pytest.fixture
async def start_daemon(home: Path):
    started: list[GuardDaemon] = []

    async def _start(policy=by_prefix, delay: float = 0.0, **settings: Any) -> tuple[GuardDaemon, AutoUI]:
        ui = AutoUI(policy, delay=delay)
        daemon = GuardDaemon(write_settings(home, **settings), ui, port=0)
        await daemon.start()
        started.append(daemon)
        return daemon, ui

    yield _start
    for daemon in started:
        await daemon.stop()


class ProxyProcess:
    """Drives ``mcp-guard proxy`` over real pipes, like Claude Desktop does."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.responses: dict[Any, dict[str, Any]] = {}
        self.raw: list[Any] = []
        self._arrived = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, home: Path, server: str = FAKE_SERVER) -> "ProxyProcess":
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp_guard", "proxy", "--name", "fake", "--", sys.executable, server,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "MCP_GUARD_HOME": str(home)}, limit=64 * 1024 * 1024,
        )
        return cls(proc)

    async def _read(self) -> None:
        assert self.proc.stdout
        while line := await self.proc.stdout.readline():
            msg = json.loads(line)
            self.raw.append(msg)
            for m in msg if isinstance(msg, list) else [msg]:
                self.responses[m.get("id")] = m
            self._arrived.set()

    async def send(self, message: Any) -> None:
        assert self.proc.stdin
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def call(self, msg_id: Any, tool: str, arguments: dict | None = None) -> None:
        await self.send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                         "params": {"name": tool, "arguments": arguments or {}}})

    async def response(self, msg_id: Any, timeout: float = 10) -> dict[str, Any]:
        async def wait() -> dict[str, Any]:
            while msg_id not in self.responses:
                self._arrived.clear()
                await self._arrived.wait()
            return self.responses[msg_id]
        return await asyncio.wait_for(wait(), timeout)

    async def close(self) -> int:
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            code = await asyncio.wait_for(self.proc.wait(), 10)
        finally:
            if self.proc.returncode is None:
                self.proc.kill()
            self._reader.cancel()
        return code


@pytest.fixture
async def proxy(home: Path):
    procs: list[ProxyProcess] = []

    async def _start(**kw: Any) -> ProxyProcess:
        p = await ProxyProcess.start(home, **kw)
        procs.append(p)
        return p

    yield _start
    for p in procs:
        if p.proc.returncode is None:
            await p.close()


def text_of(response: dict[str, Any]) -> str:
    return response["result"]["content"][0]["text"]
