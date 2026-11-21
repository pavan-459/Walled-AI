from __future__ import annotations

import json

from mcp_guard.installer import install, uninstall, wrapped_servers

CONFIG = {
    "globalShortcut": "Ctrl+Space",
    "mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                       "env": {"FOO": "bar"}},
        "github": {"command": "docker", "args": ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"]},
        "remote": {"url": "https://example.com/mcp"},
    },
}


def test_install_and_uninstall_round_trip(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps(CONFIG))

    changes, backup = install(path, python="/usr/bin/python3")
    assert backup is not None and backup.exists()
    actions = {c.server: c.action for c in changes}
    assert actions == {"filesystem": "wrapped", "github": "wrapped", "remote": "skipped"}

    data = json.loads(path.read_text())
    fs = data["mcpServers"]["filesystem"]
    assert fs["command"] == "/usr/bin/python3"
    assert fs["args"] == ["-m", "mcp_guard", "proxy", "--name", "filesystem", "--",
                          "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert fs["env"] == {"FOO": "bar"}
    assert data["globalShortcut"] == "Ctrl+Space"
    assert sorted(wrapped_servers(path)) == ["filesystem", "github"]

    # Idempotent
    changes, backup = install(path, python="/usr/bin/python3")
    assert backup is None
    assert {c.action for c in changes} <= {"already wrapped", "skipped"}

    uninstall(path)
    assert json.loads(path.read_text()) == CONFIG


def test_only_and_dry_run(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(CONFIG))
    changes, backup = install(path, only=["github"], dry_run=True)
    assert [c.server for c in changes] == ["github"]
    assert backup is None
    assert json.loads(path.read_text()) == CONFIG
