from __future__ import annotations

import asyncio

import aiohttp

from mcp_guard.client import DecisionClient
from mcp_guard.config import Settings, read_runtime, read_token
from mcp_guard.models import ApprovalRequest, Reason, Verdict


def _req(tool: str) -> ApprovalRequest:
    return ApprovalRequest(server_name="s", method="tools/call", params={"name": tool, "arguments": {}})


async def _post(path: str, headers: dict | None = None, json: dict | None = None) -> int:
    port = read_runtime()["port"]
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}{path}", headers=headers or {}, json=json or {}) as r:
            return r.status


async def test_api_requires_token(start_daemon):
    await start_daemon()
    assert await _post("/v1/approvals") == 401
    assert await _post("/v1/approvals", {"Authorization": "Bearer wrong"}) == 401


async def test_api_rejects_browser_origin(start_daemon):
    await start_daemon()
    headers = {"Authorization": f"Bearer {read_token()}", "Origin": "https://evil.example"}
    assert await _post("/v1/approvals", headers) == 403


async def test_api_rejects_non_loopback_host(start_daemon):
    await start_daemon()
    headers = {"Authorization": f"Bearer {read_token()}", "Host": "attacker.example"}
    assert await _post("/v1/approvals", headers) == 403


async def test_client_round_trip(start_daemon):
    await start_daemon()
    client = DecisionClient(Settings.load())
    try:
        assert (await client.request_approval(_req("allow_x"))).verdict is Verdict.ALLOW
        assert (await client.request_approval(_req("block_x"))).verdict is Verdict.BLOCK
    finally:
        await client.close()


async def test_overload_blocks_new_requests(start_daemon):
    daemon, _ = await start_daemon(max_pending=1)
    client = DecisionClient(Settings.load())
    try:
        first = asyncio.create_task(client.request_approval(_req("hold_1")))
        while not daemon.pending:
            await asyncio.sleep(0.02)
        second = await client.request_approval(_req("hold_2"))
        assert second.reason is Reason.OVERLOADED
        first.cancel()
    finally:
        await client.close()


async def test_shutdown_blocks_pending_requests(start_daemon):
    daemon, _ = await start_daemon()
    client = DecisionClient(Settings.load())
    try:
        pending = asyncio.create_task(client.request_approval(_req("hold_1")))
        while not daemon.pending:
            await asyncio.sleep(0.02)
        await daemon.stop()
        decision = await pending
        assert decision.verdict is Verdict.BLOCK
        assert decision.reason in (Reason.SHUTDOWN, Reason.DAEMON_UNAVAILABLE)
    finally:
        await client.close()


async def test_audit_log_written(start_daemon, home):
    await start_daemon()
    client = DecisionClient(Settings.load())
    try:
        await client.request_approval(_req("allow_x"))
    finally:
        await client.close()
    lines = (home / "audit.jsonl").read_text().splitlines()
    assert '"verdict": "allow"' in lines[-1] and '"target": "allow_x"' in lines[-1]


def test_token_file_is_private(home):
    import os
    import stat

    from mcp_guard.config import load_or_create_token, token_path

    load_or_create_token()
    if os.name == "posix":
        assert stat.S_IMODE(token_path().stat().st_mode) == 0o600
