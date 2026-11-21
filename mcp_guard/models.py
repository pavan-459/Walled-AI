"""Data types shared by the interceptors, the daemon and the UI."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class Reason(str, Enum):
    USER = "user"                       # the user clicked Allow / Block
    TIMEOUT = "timeout"                 # nobody answered within decision_timeout
    CANCELLED = "cancelled"             # the client cancelled the request / disconnected
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    SHUTDOWN = "shutdown"               # daemon is stopping
    OVERLOADED = "overloaded"           # too many pending approvals
    ERROR = "error"


@dataclass
class ApprovalRequest:
    """Everything the user needs to see to decide on one intercepted MCP request."""

    server_name: str                    # name from claude_desktop_config.json / gateway --name
    method: str                         # JSON-RPC method, e.g. "tools/call"
    params: Any = None                  # raw JSON-RPC params
    jsonrpc_id: Any = None
    source: str = "stdio"               # "stdio" (Claude Desktop) or "http" (remote connector)
    client_name: str | None = None      # clientInfo.name from initialize, e.g. "claude-ai"
    client_version: str | None = None
    server_info: str | None = None      # serverInfo name/version reported by the MCP server
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    @property
    def tool_name(self) -> str | None:
        if self.method == "tools/call" and isinstance(self.params, dict):
            name = self.params.get("name")
            return str(name) if name is not None else None
        return None

    @property
    def arguments(self) -> Any:
        if not isinstance(self.params, dict):
            return self.params
        if self.method == "tools/call":
            return self.params.get("arguments", {})
        if self.method == "resources/read":
            return {"uri": self.params.get("uri")}
        return self.params

    @property
    def target(self) -> str:
        """Short human label: the tool name, resource URI or method."""
        if self.tool_name:
            return self.tool_name
        if self.method == "resources/read" and isinstance(self.params, dict):
            return str(self.params.get("uri"))
        if self.method == "prompts/get" and isinstance(self.params, dict):
            return str(self.params.get("name"))
        return self.method

    @property
    def client_label(self) -> str:
        """Best-effort friendly name of the app that issued the request."""
        name = (self.client_name or "").lower()
        if self.source == "http":
            return "Claude (remote connector: claude.ai / Desktop)"
        if "claude" in name:
            return f"Claude Desktop ({self.client_name})"
        return self.client_name or "unknown MCP client"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRequest":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class Decision:
    verdict: Verdict
    reason: Reason
    message: str = ""
    decided_at: float = field(default_factory=time.time)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason.value,
            "message": self.message,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        return cls(
            verdict=Verdict(data["verdict"]),
            reason=Reason(data.get("reason", "error")),
            message=data.get("message", ""),
            decided_at=data.get("decided_at", time.time()),
        )

    @classmethod
    def block(cls, reason: Reason, message: str = "") -> "Decision":
        return cls(Verdict.BLOCK, reason, message)

    @classmethod
    def allow(cls, reason: Reason = Reason.USER, message: str = "") -> "Decision":
        return cls(Verdict.ALLOW, reason, message)
