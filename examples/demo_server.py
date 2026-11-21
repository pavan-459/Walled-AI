"""A small, harmless MCP server for demonstrating MCP Guard.

Tools look like the kind of thing you'd want to approve (deleting files, sending
email), but they only touch a sandbox folder and never send anything.

    python examples/demo_server.py                  # stdio (for Claude Desktop)
    python examples/demo_server.py --http 9000      # Streamable HTTP at http://127.0.0.1:9000/mcp
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import tempfile
from pathlib import Path

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

SANDBOX = Path(os.environ.get("DEMO_SANDBOX", Path(tempfile.gettempdir()) / "mcp-guard-demo"))
SANDBOX.mkdir(parents=True, exist_ok=True)
if not (SANDBOX / "notes.txt").exists():
    (SANDBOX / "notes.txt").write_text("Quarterly numbers are in the spreadsheet.\n")

mcp = _Server("guard-demo")


def _safe(name: str) -> Path:
    path = (SANDBOX / name).resolve()
    if SANDBOX.resolve() not in path.parents and path != SANDBOX.resolve():
        raise ValueError("path escapes the demo sandbox")
    return path


@mcp.tool()
def get_time(timezone: str = "UTC") -> str:
    """Return the current time (read-only)."""
    return f"{dt.datetime.now(dt.timezone.utc).isoformat()} (requested zone: {timezone})"


@mcp.tool()
def list_files() -> list[str]:
    """List files in the demo sandbox folder."""
    return sorted(p.name for p in SANDBOX.iterdir())


@mcp.tool()
def write_file(name: str, content: str) -> str:
    """Create or overwrite a file in the demo sandbox."""
    _safe(name).write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {name}"


@mcp.tool()
def delete_file(name: str) -> str:
    """Delete a file from the demo sandbox."""
    path = _safe(name)
    if not path.exists():
        return f"{name} does not exist"
    path.unlink()
    return f"deleted {name}"


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Pretend to send an email (nothing is sent; it's logged to the sandbox)."""
    with (SANDBOX / "outbox.log").open("a", encoding="utf-8") as fh:
        fh.write(f"to={to} subject={subject!r} body={body!r}\n")
    return f"email to {to} queued (demo only, nothing was sent)"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", type=int, metavar="PORT", help="serve Streamable HTTP on this port")
    args = parser.parse_args()
    if args.http:
        try:
            mcp.run("streamable-http", port=args.http)
        except TypeError:  # SDK 1.x takes the port in settings
            mcp.settings.port = args.http  # type: ignore[attr-defined]
            mcp.run("streamable-http")
    else:
        mcp.run()
