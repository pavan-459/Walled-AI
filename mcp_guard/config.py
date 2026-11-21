"""Paths, settings and shared secrets for MCP Guard.

Everything MCP Guard persists lives in one per-user directory:

    config.json   user-editable settings (see ``Settings``)
    token         random bearer token that proxies use to talk to the daemon (0600)
    daemon.json   runtime info written by the running daemon (port, pid)
    audit.jsonl   one JSON line per decision
    logs/         daemon / proxy / gateway log files

The directory can be overridden with the ``MCP_GUARD_HOME`` environment variable,
which the test-suite uses to run fully isolated instances.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "mcp-guard"


def guard_home() -> Path:
    """Return (and create) the per-user MCP Guard directory."""
    override = os.environ.get("MCP_GUARD_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(exist_ok=True)
    return base


def settings_path() -> Path:
    return guard_home() / "config.json"


def runtime_path() -> Path:
    return guard_home() / "daemon.json"


def token_path() -> Path:
    return guard_home() / "token"


def audit_path() -> Path:
    return guard_home() / "audit.jsonl"


def log_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return guard_home() / "logs" / f"{safe}.log"


@dataclass
class Settings:
    """User settings. Unknown keys in config.json are ignored; missing keys use defaults."""

    # Port for the daemon's localhost API. 0 = pick a free port (proxies read daemon.json).
    daemon_port: int = 0
    # Seconds to wait for the user before a pending request is automatically blocked.
    decision_timeout: float = 120.0
    # JSON-RPC methods that require approval. tools/call is the one that has side effects;
    # add "resources/read" or "prompts/get" for stricter control.
    intercept_methods: list[str] = field(default_factory=lambda: ["tools/call"])
    # What to do when the daemon cannot be reached: "closed" blocks (safe default), "open" allows.
    fail_mode: str = "closed"
    # Start the daemon automatically when a proxy needs it and it isn't running.
    auto_start_daemon: bool = True
    # Which UI the auto-started daemon uses: "tk" | "console" | "auto-allow" | "auto-block".
    ui: str = "tk"
    # How a block is reported for tools/call: "tool_error" (result with isError=true, so the
    # model can explain it to the user) or "jsonrpc_error" (protocol-level error).
    block_response: str = "tool_error"
    # Max pending approvals before new ones are blocked outright (protects against floods).
    max_pending: int = 50
    # Write tool arguments into the audit log (truncated to audit_max_chars).
    audit_arguments: bool = True
    audit_max_chars: int = 2000

    @classmethod
    def load(cls) -> "Settings":
        path = settings_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[mcp-guard] ignoring unreadable {path}: {exc}", file=sys.stderr)
        known = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in data.items() if k in known})
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.fail_mode not in ("closed", "open"):
            raise ValueError("fail_mode must be 'closed' or 'open'")
        if self.block_response not in ("tool_error", "jsonrpc_error"):
            raise ValueError("block_response must be 'tool_error' or 'jsonrpc_error'")
        if self.decision_timeout <= 0:
            raise ValueError("decision_timeout must be positive")

    def save(self) -> None:
        settings_path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def _write_private(path: Path, content: str) -> None:
    """Write a file readable only by the current user."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def load_or_create_token() -> str:
    """Return the shared daemon token, generating it on first use."""
    path = token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    _write_private(path, token)
    return token


def read_token() -> str | None:
    try:
        return token_path().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_runtime(info: dict[str, Any]) -> None:
    _write_private(runtime_path(), json.dumps(info))


def read_runtime() -> dict[str, Any] | None:
    try:
        return json.loads(runtime_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_runtime(pid: int | None = None) -> None:
    """Remove daemon.json, but only if it still belongs to ``pid`` (when given)."""
    info = read_runtime()
    if info is None:
        return
    if pid is not None and info.get("pid") != pid:
        return
    try:
        runtime_path().unlink()
    except OSError:
        pass
