"""Minimal stdio MCP server used by the tests (no SDK dependency, fully predictable).

tools/call behaviour by tool name suffix:
  *_count  -> returns how many tools/call requests this server has executed
  *_crash  -> exits with status 3
  *_big    -> returns a payload of arguments["n"] characters
  anything -> echoes the name and arguments
"""

import json
import sys

executed = 0


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    if isinstance(msg, list):  # batch: answer each request as a batch
        out = []
        for m in msg:
            if "id" in m:
                executed += 1
                out.append({"jsonrpc": "2.0", "id": m["id"],
                            "result": {"content": [{"type": "text", "text": f"ran {m['params']['name']}"}]}})
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
        continue
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        continue  # notification
    if method == "initialize":
        reply(msg_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                       "serverInfo": {"name": "fake-server", "version": "9.9"}})
    elif method == "tools/list":
        reply(msg_id, {"tools": []})
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        if name.endswith("_crash"):
            sys.exit(3)
        executed += 1
        if name.endswith("_count"):
            text = str(executed)
        elif name.endswith("_big"):
            text = "x" * int(args["n"])
        else:
            text = f"ran {name} {json.dumps(args, sort_keys=True)}"
        reply(msg_id, {"content": [{"type": "text", "text": text}]})
    else:
        reply(msg_id, error={"code": -32601, "message": "method not found"})
