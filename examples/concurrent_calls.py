"""Send two tool calls at the same time through the MCP Guard stdio proxy.

Claude Desktop usually sends tool calls one at a time, so this script shows
concurrent handling directly: two popups appear and each is decided on its own.

    python examples/concurrent_calls.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

DEMO_SERVER = Path(__file__).resolve().parent / "demo_server.py"


async def call(client: Client, name: str, args: dict) -> None:
    start = time.time()
    result = await client.call_tool(name, args)
    text = " ".join(getattr(c, "text", str(c)) for c in result.content)
    status = "BLOCKED" if getattr(result, "is_error", getattr(result, "isError", False)) else "ALLOWED"
    print(f"[{time.time() - start:5.1f}s] {name}: {status} -> {text}")


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_guard", "proxy", "--name", "demo-files", "--", sys.executable, str(DEMO_SERVER)],
        env=dict(os.environ),
    )
    async with Client(params, read_timeout_seconds=300) as client:
        print("Sending delete_file and send_email at the same time...")
        await asyncio.gather(
            call(client, "delete_file", {"name": "notes.txt"}),
            call(client, "send_email", {"to": "boss@example.com", "subject": "hi", "body": "test"}),
        )


if __name__ == "__main__":
    asyncio.run(main())
