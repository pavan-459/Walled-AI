"""Small JSON-RPC 2.0 helpers used by both interceptors.

MCP messages are JSON-RPC 2.0. Over stdio each message is one line of JSON; over
Streamable HTTP each POST body is one message (or, in older protocol revisions, a
batch array). These helpers decide which messages need approval and build the
replies sent back when the user blocks one.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .models import Decision, Reason

# Implementation-defined server error code used for user denials.
BLOCKED_ERROR_CODE = -32001

_REASON_TEXT = {
    Reason.USER: "The user blocked this request.",
    Reason.TIMEOUT: "No decision was made in time, so the request was blocked.",
    Reason.CANCELLED: "The request was cancelled before a decision was made.",
    Reason.DAEMON_UNAVAILABLE: "The MCP Guard approval service is not running, so the request was blocked.",
    Reason.SHUTDOWN: "MCP Guard is shutting down, so the request was blocked.",
    Reason.OVERLOADED: "Too many requests are waiting for approval, so this one was blocked.",
    Reason.ERROR: "MCP Guard hit an internal error, so the request was blocked.",
}


def parse(raw: bytes | str) -> Any | None:
    """Parse a JSON-RPC message; return None if it isn't valid JSON."""
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def dumps_line(message: Any) -> bytes:
    """Serialise a message as one newline-terminated line (stdio framing)."""
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def is_request(message: Any) -> bool:
    return isinstance(message, dict) and "method" in message and "id" in message


def is_notification(message: Any) -> bool:
    return isinstance(message, dict) and "method" in message and "id" not in message


def needs_approval(message: Any, methods: Iterable[str]) -> bool:
    """True when ``message`` is a request (not a notification) for an intercepted method."""
    return is_request(message) and message.get("method") in set(methods)


def cancelled_request_id(message: Any) -> Any | None:
    """If ``message`` is notifications/cancelled, return the id it cancels."""
    if is_notification(message) and message.get("method") == "notifications/cancelled":
        params = message.get("params") or {}
        return params.get("requestId")
    return None


def client_info(message: Any) -> dict[str, Any] | None:
    """clientInfo from an initialize request, if this is one."""
    if is_request(message) and message.get("method") == "initialize":
        info = (message.get("params") or {}).get("clientInfo")
        if isinstance(info, dict):
            return info
    return None


CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"


def meta_client_info(message: Any) -> dict[str, Any] | None:
    """clientInfo carried in a request's ``_meta`` (protocol 2026-07-28, which has no
    initialize handshake and stamps client identity on every request)."""
    if isinstance(message, dict):
        meta = (message.get("params") or {}).get("_meta") if isinstance(message.get("params"), dict) else None
        info = (meta or {}).get(CLIENT_INFO_META_KEY)
        if isinstance(info, dict):
            return info
    return None


def denial_response(request: dict[str, Any], decision: Decision, style: str = "tool_error") -> dict[str, Any]:
    """Build the response returned to the client when ``request`` is not allowed to run.

    For tools/call the default is a *tool result* with ``isError: true``: Claude shows it
    as a failed tool call and the model can tell the user it was blocked. Other methods
    (and ``style="jsonrpc_error"``) get a standard JSON-RPC error object.
    """
    text = _REASON_TEXT.get(decision.reason, "The request was blocked.")
    if decision.message:
        text = f"{text} ({decision.message})"
    text = f"[MCP Guard] {text}"

    if request.get("method") == "tools/call" and style == "tool_error":
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            # resultType is required from protocol 2026-07-28 and ignored by older peers.
            "result": {"content": [{"type": "text", "text": text}], "isError": True, "resultType": "complete"},
        }
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": BLOCKED_ERROR_CODE,
            "message": text,
            "data": {"blockedBy": "mcp-guard", "reason": decision.reason.value},
        },
    }
