"""Wire MCP Guard into Claude Desktop by rewriting claude_desktop_config.json.

Each stdio server entry

    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

becomes

    "filesystem": {"command": "/path/to/python", "args": ["-m", "mcp_guard", "proxy", "--name", "filesystem",
                   "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

The original command is kept verbatim after ``--``, so ``uninstall`` can restore it
exactly, and ``env`` and any other keys are left untouched. A timestamped backup
is written before every change.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WRAP_PREFIX = ["-m", "mcp_guard", "proxy"]


def default_config_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


@dataclass
class Change:
    server: str
    action: str   # "wrapped" | "unwrapped" | "already wrapped" | "skipped"
    detail: str = ""


def is_wrapped(entry: dict[str, Any]) -> bool:
    args = entry.get("args") or []
    return list(args[: len(WRAP_PREFIX)]) == WRAP_PREFIX and "--" in args


def wrap_entry(name: str, entry: dict[str, Any], python: str) -> dict[str, Any]:
    wrapped = dict(entry)
    wrapped["command"] = python
    wrapped["args"] = [*WRAP_PREFIX, "--name", name, "--", entry["command"], *(entry.get("args") or [])]
    return wrapped


def unwrap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    args = list(entry.get("args") or [])
    split = args.index("--")
    original = args[split + 1:]
    if not original:
        raise ValueError("wrapped entry has no original command")
    restored = dict(entry)
    restored["command"] = original[0]
    restored["args"] = original[1:]
    return restored


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Open Claude Desktop > Settings > Developer > Edit Config once, add your MCP "
            "servers, then run install again (or pass --config).")
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _save(path: Path, data: dict[str, Any]) -> Path:
    backup = path.with_name(f"{path.name}.mcp-guard-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic: Claude never sees a half-written file
    return backup


def install(path: Path, only: list[str] | None = None, dry_run: bool = False,
            python: str | None = None) -> tuple[list[Change], Path | None]:
    python = python or sys.executable
    data = _load(path)
    servers = data.get("mcpServers") or {}
    changes: list[Change] = []
    for name, entry in servers.items():
        if only and name not in only:
            continue
        if not isinstance(entry, dict) or "command" not in entry:
            changes.append(Change(name, "skipped", "not a stdio server (remote servers: use `mcp-guard gateway`)"))
            continue
        if is_wrapped(entry):
            changes.append(Change(name, "already wrapped"))
            continue
        servers[name] = wrap_entry(name, entry, python)
        changes.append(Change(name, "wrapped", f"{entry['command']} {' '.join(entry.get('args') or [])}".strip()))
    backup = None
    if not dry_run and any(c.action == "wrapped" for c in changes):
        data["mcpServers"] = servers
        backup = _save(path, data)
    return changes, backup


def uninstall(path: Path, dry_run: bool = False) -> tuple[list[Change], Path | None]:
    data = _load(path)
    servers = data.get("mcpServers") or {}
    changes: list[Change] = []
    for name, entry in servers.items():
        if isinstance(entry, dict) and is_wrapped(entry):
            servers[name] = unwrap_entry(entry)
            changes.append(Change(name, "unwrapped", servers[name]["command"]))
    backup = None
    if not dry_run and changes:
        data["mcpServers"] = servers
        backup = _save(path, data)
    return changes, backup


def wrapped_servers(path: Path) -> list[str]:
    try:
        servers = _load(path).get("mcpServers") or {}
    except (OSError, ValueError):
        return []
    return [n for n, e in servers.items() if isinstance(e, dict) and is_wrapped(e)]
