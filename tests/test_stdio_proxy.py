"""End-to-end tests: real proxy subprocess + fake MCP server + in-process daemon."""

from __future__ import annotations

import asyncio
import time

from conftest import text_of, write_settings


async def test_allowed_call_is_forwarded(start_daemon, proxy):
    _, ui = await start_daemon()
    p = await proxy()
    await p.call(1, "allow_echo", {"x": 1})
    resp = await p.response(1)
    assert text_of(resp) == 'ran allow_echo {"x": 1}'
    assert ui.shown[0].tool_name == "allow_echo"
    assert ui.shown[0].arguments == {"x": 1}


async def test_blocked_call_never_reaches_server(start_daemon, proxy):
    await start_daemon()
    p = await proxy()
    await p.call(1, "block_delete", {"path": "/"})
    resp = await p.response(1)
    assert resp["result"]["isError"] is True
    assert "blocked" in text_of(resp)
    await p.call(2, "allow_count")
    assert text_of(await p.response(2)) == "1"  # only the count call itself ran


async def test_initialize_and_listing_are_not_intercepted(start_daemon, proxy):
    _, ui = await start_daemon()
    p = await proxy()
    await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "claude-ai", "version": "1.0"}}})
    assert (await p.response(1))["result"]["serverInfo"]["name"] == "fake-server"
    await p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    await p.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    await p.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    await p.response(2)
    await p.response(3)
    assert ui.shown == []
    # The popup gets the client and server identity captured during initialize.
    await p.call(4, "allow_x")
    await p.response(4)
    assert ui.shown[0].client_name == "claude-ai"
    assert ui.shown[0].server_info == "fake-server 9.9"


async def test_concurrent_requests_are_decided_independently(start_daemon, proxy):
    daemon, _ = await start_daemon(delay=0.5)  # every decision takes 0.5 s
    p = await proxy()
    names = ["allow_a", "block_b", "allow_c", "block_d", "allow_e", "block_f", "allow_g", "block_h"]
    started = time.monotonic()
    for i, name in enumerate(names):
        await p.call(i, name)
    results = [await p.response(i) for i in range(len(names))]
    elapsed = time.monotonic() - started
    for name, resp in zip(names, results):
        is_error = resp["result"].get("isError", False)
        assert is_error == name.startswith("block_"), name
    # Handled in parallel: ~0.5 s total, not 8 x 0.5 s.
    assert elapsed < 2.5, elapsed
    assert daemon.pending == {}


async def test_other_traffic_flows_while_a_request_is_pending(start_daemon, proxy):
    daemon, _ = await start_daemon()
    p = await proxy()
    await p.call(1, "hold_forever")
    await p.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    await p.call(3, "allow_quick")
    await p.response(2, timeout=3)
    await p.response(3, timeout=3)
    assert 1 not in p.responses
    assert len(daemon.pending) == 1


async def test_client_cancel_dismisses_popup(start_daemon, proxy):
    daemon, ui = await start_daemon()
    p = await proxy()
    await p.call("abc", "hold_me")
    for _ in range(50):
        if daemon.pending:
            break
        await asyncio.sleep(0.05)
    request_id = next(iter(daemon.pending))
    await p.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "abc"}})
    for _ in range(50):
        if not daemon.pending:
            break
        await asyncio.sleep(0.05)
    assert daemon.pending == {}
    assert (request_id, "cancelled") in ui.dismissed
    await asyncio.sleep(0.3)
    assert "abc" not in p.responses  # cancelled requests get no response


async def test_unanswered_request_times_out_as_block(start_daemon, proxy):
    _, ui = await start_daemon(decision_timeout=1)
    p = await proxy()
    await p.call(1, "hold_me")
    resp = await p.response(1, timeout=5)
    assert resp["result"]["isError"] is True
    assert "in time" in text_of(resp)
    assert ui.dismissed and ui.dismissed[0][1] == "timeout"


async def test_fails_closed_when_daemon_is_down(home, proxy):
    p = await proxy()  # no daemon started, auto-start disabled
    await p.call(1, "allow_anything")
    resp = await p.response(1)
    assert resp["result"]["isError"] is True
    assert "not running" in text_of(resp)


async def test_fail_open_is_opt_in(home, proxy):
    write_settings(home, fail_mode="open")
    p = await proxy()
    await p.call(1, "allow_anything")
    assert text_of(await p.response(1)).startswith("ran allow_anything")


async def test_jsonrpc_error_style(start_daemon, proxy):
    await start_daemon(block_response="jsonrpc_error")
    p = await proxy()
    await p.call(1, "block_x")
    resp = await p.response(1)
    assert resp["error"]["code"] == -32001
    assert resp["error"]["data"]["reason"] == "user"


async def test_large_messages_pass_through(start_daemon, proxy):
    await start_daemon()
    p = await proxy()
    big = "y" * 2_000_000  # far above asyncio's default 64 KiB line limit
    await p.call(1, "allow_big", {"n": 3_000_000, "pad": big})
    assert len(text_of(await p.response(1, timeout=20))) == 3_000_000


async def test_batch_requests_are_approved_per_item(start_daemon, proxy):
    await start_daemon()
    p = await proxy()
    await p.send([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "allow_one"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "block_two"}},
    ])
    assert text_of(await p.response(1)) == "ran allow_one"
    assert (await p.response(2))["result"]["isError"] is True


async def test_server_crash_ends_proxy_with_its_exit_code(start_daemon, proxy):
    await start_daemon()
    p = await proxy()
    await p.call(1, "allow_crash")
    assert await asyncio.wait_for(p.proc.wait(), 10) == 3


async def test_client_disconnect_stops_server_and_cancels_pending(start_daemon, proxy):
    daemon, _ = await start_daemon()
    p = await proxy()
    await p.call(1, "hold_me")
    for _ in range(50):
        if daemon.pending:
            break
        await asyncio.sleep(0.05)
    assert await p.close() == 0
    for _ in range(50):
        if not daemon.pending:
            break
        await asyncio.sleep(0.05)
    assert daemon.pending == {}


async def test_missing_server_command(home, proxy):
    p = await proxy(server="/definitely/not/here.py")
    # python exits non-zero because the script doesn't exist; proxy relays the code
    assert await asyncio.wait_for(p.proc.wait(), 10) != 0
