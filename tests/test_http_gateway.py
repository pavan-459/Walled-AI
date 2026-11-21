"""Gateway tests against a fake Streamable HTTP MCP server."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from mcp_guard.config import Settings
from mcp_guard.http_gateway import HttpGateway


@pytest.fixture
async def upstream():
    calls: list[dict] = []

    async def mcp(request: web.Request) -> web.StreamResponse:
        msg = await request.json()
        calls.append({"msg": msg, "auth": request.headers.get("Authorization"),
                      "session": request.headers.get("Mcp-Session-Id")})
        if msg.get("method") == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {"serverInfo": {"name": "up"}}},
                                     headers={"Mcp-Session-Id": "sess-1"})
        if "id" not in msg:
            return web.Response(status=202)
        name = msg["params"]["name"]
        result = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": f"ran {name}"}]}}
        if name.endswith("_sse"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n')
            await asyncio.sleep(0.05)
            await resp.write(f"event: message\ndata: {json.dumps(result)}\n\n".encode())
            await resp.write_eof()
            return resp
        return web.json_response(result)

    app = web.Application()
    app.router.add_post("/mcp", mcp)
    server = TestServer(app)
    await server.start_server()
    yield server, calls
    await server.close()


@pytest.fixture
async def gateway(upstream, start_daemon):
    await start_daemon()
    server, _ = upstream
    gw = HttpGateway(str(server.make_url("/mcp")), "remote-demo", Settings.load())
    gw_server = TestServer(gw.build_app())
    await gw_server.start_server()
    async with aiohttp.ClientSession() as session:
        yield gw_server, session
    await gw_server.close()


def _call(msg_id: int, name: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": {}}}


async def test_allowed_call_reaches_upstream_with_headers(gateway, upstream):
    gw, session = gateway
    _, calls = upstream
    async with session.post(gw.make_url("/mcp"), json=_call(1, "allow_x"),
                            headers={"Authorization": "Bearer user-token", "Mcp-Session-Id": "sess-1"}) as r:
        body = await r.json()
    assert body["result"]["content"][0]["text"] == "ran allow_x"
    assert calls[-1]["auth"] == "Bearer user-token"
    assert calls[-1]["session"] == "sess-1"


async def test_blocked_call_is_answered_by_gateway(gateway, upstream):
    gw, session = gateway
    _, calls = upstream
    async with session.post(gw.make_url("/mcp"), json=_call(7, "block_x")) as r:
        body = await r.json()
    assert r.status == 200
    assert body["id"] == 7 and body["result"]["isError"] is True
    assert calls == []


async def test_sse_responses_stream_through(gateway):
    gw, session = gateway
    async with session.post(gw.make_url("/mcp"), json=_call(2, "allow_sse")) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        text = await r.text()
    assert "notifications/progress" in text and "ran allow_sse" in text


async def test_session_header_and_initialize_pass_through(gateway, start_daemon):
    gw, session = gateway
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"clientInfo": {"name": "claude-ai", "version": "0.1.0"}, "capabilities": {},
                       "protocolVersion": "2025-06-18"}}
    async with session.post(gw.make_url("/mcp"), json=init) as r:
        assert r.headers["Mcp-Session-Id"] == "sess-1"


async def test_secret_prefix(upstream, start_daemon):
    await start_daemon()
    server, _ = upstream
    gw = HttpGateway(str(server.make_url("/mcp")), "remote-demo", Settings.load(), secret_prefix="s3cret")
    gw_server = TestServer(gw.build_app())
    await gw_server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(gw_server.make_url("/mcp"), json=_call(1, "allow_x")) as r:
                assert r.status == 404
            async with session.post(gw_server.make_url("/s3cret/mcp"), json=_call(1, "allow_x")) as r:
                assert (await r.json())["result"]["content"][0]["text"] == "ran allow_x"
    finally:
        await gw_server.close()


async def test_upstream_down_returns_502(start_daemon):
    await start_daemon()
    gw = HttpGateway("http://127.0.0.1:9/mcp", "dead", Settings.load())
    gw_server = TestServer(gw.build_app())
    await gw_server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(gw_server.make_url("/mcp"), json={"jsonrpc": "2.0", "id": 1, "method": "ping"}) as r:
                assert r.status == 502
    finally:
        await gw_server.close()
