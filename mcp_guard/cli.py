"""Command-line entry point: ``mcp-guard <command>`` (or ``python -m mcp_guard``)."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__
from .config import Settings, guard_home, read_runtime, read_token


def _cmd_daemon(args: argparse.Namespace) -> int:
    from .client import daemon_health
    from .daemon import GuardDaemon, configure_logging
    from .ui import create_ui

    configure_logging("daemon", args.verbose)
    if daemon_health():
        print("MCP Guard daemon is already running.", file=sys.stderr)
        return 0
    settings = Settings.load()
    if args.timeout:
        settings.decision_timeout = args.timeout
    ui_name = args.ui or settings.ui
    try:
        ui = create_ui(ui_name)
    except Exception as exc:  # e.g. no display / tkinter missing
        print(f"Could not start the '{ui_name}' UI ({exc}). Try --ui console.", file=sys.stderr)
        return 1
    GuardDaemon(settings, ui, port=args.port).run()
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    from .client import daemon_health, spawn_daemon

    if daemon_health():
        print("MCP Guard daemon is already running.")
        return 0
    settings = Settings.load()
    spawn_daemon(args.ui or settings.ui)
    for _ in range(50):
        health = daemon_health()
        if health:
            print(f"MCP Guard daemon started (pid {health['pid']}).")
            return 0
        time.sleep(0.2)
    print(f"Daemon did not come up. See {guard_home() / 'logs' / 'daemon.log'}", file=sys.stderr)
    return 1


def _cmd_stop(_args: argparse.Namespace) -> int:
    runtime, token = read_runtime(), read_token()
    if not runtime or not token:
        print("MCP Guard daemon is not running.")
        return 0
    req = urllib.request.Request(f"http://127.0.0.1:{runtime['port']}/v1/shutdown", method="POST", data=b"",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except OSError:
        print("Daemon not reachable (already stopped?).")
        return 0
    print("MCP Guard daemon stopping.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from .client import daemon_health
    from .installer import default_config_path, wrapped_servers

    health = daemon_health()
    config = Path(args.config) if args.config else default_config_path()
    print(f"MCP Guard {__version__}")
    print(f"  home       : {guard_home()}")
    print(f"  daemon     : {'running (pid %s, %s pending)' % (health['pid'], health['pending']) if health else 'not running'}")
    print(f"  Claude cfg : {config}{'' if config.exists() else ' (not found)'}")
    print(f"  protected  : {', '.join(wrapped_servers(config)) or 'none'}")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    from .installer import default_config_path, install

    path = Path(args.config) if args.config else default_config_path()
    try:
        changes, backup = install(path, only=args.only, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    for c in changes:
        print(f"  {c.server:24} {c.action}{'  ' + c.detail if c.detail else ''}")
    if not changes:
        print("  no MCP servers found in config")
    if args.dry_run:
        print("(dry run, nothing written)")
    elif backup:
        print(f"Backup: {backup}\nRestart Claude Desktop to apply.")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from .installer import default_config_path, uninstall

    path = Path(args.config) if args.config else default_config_path()
    try:
        changes, backup = uninstall(path, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    for c in changes:
        print(f"  {c.server:24} restored  {c.detail}")
    if not changes:
        print("  nothing to restore")
    elif backup:
        print(f"Backup: {backup}\nRestart Claude Desktop to apply.")
    return 0


def _cmd_proxy(args: argparse.Namespace) -> int:
    from .daemon import configure_logging
    from .stdio_proxy import run_proxy

    configure_logging(f"proxy-{args.name}", args.verbose)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run_proxy(args.name, command)


def _cmd_gateway(args: argparse.Namespace) -> int:
    from .daemon import configure_logging
    from .http_gateway import generate_secret, run_gateway

    configure_logging(f"gateway-{args.name}", args.verbose)
    secret = generate_secret() if args.secret_path else None
    return run_gateway(args.upstream, args.name, args.host, args.port, secret)


def _cmd_log(args: argparse.Namespace) -> int:
    from .config import audit_path

    path = audit_path()
    if not path.exists():
        print("No decisions recorded yet.")
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()[-args.n:]
    for line in lines:
        e = json.loads(line)
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
        print(f"{ts}  {e['verdict'].upper():5}  {e['reason']:18} {e['server']}/{e['target']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-guard", description="Approve or block every MCP request Claude makes.")
    p.add_argument("--version", action="version", version=f"mcp-guard {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    uis = ["tk", "console", "auto-allow", "auto-block"]

    d = sub.add_parser("daemon", help="run the approval daemon in the foreground")
    d.add_argument("--ui", choices=uis, help="approval UI (default from config: tk)")
    d.add_argument("--port", type=int, help="API port (default: random free port)")
    d.add_argument("--timeout", type=float, help="seconds before an unanswered request is blocked")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=_cmd_daemon)

    s = sub.add_parser("start", help="start the daemon in the background")
    s.add_argument("--ui", choices=uis)
    s.set_defaults(func=_cmd_start)
    sub.add_parser("stop", help="stop the background daemon").set_defaults(func=_cmd_stop)

    st = sub.add_parser("status", help="show daemon and Claude Desktop status")
    st.add_argument("--config", help="path to claude_desktop_config.json")
    st.set_defaults(func=_cmd_status)

    i = sub.add_parser("install", help="protect the MCP servers in claude_desktop_config.json")
    i.add_argument("--config", help="path to claude_desktop_config.json")
    i.add_argument("--only", nargs="+", metavar="NAME", help="only wrap these servers")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=_cmd_install)

    u = sub.add_parser("uninstall", help="restore the original claude_desktop_config.json entries")
    u.add_argument("--config")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(func=_cmd_uninstall)

    px = sub.add_parser("proxy", help="(used by Claude Desktop) stdio proxy for one MCP server")
    px.add_argument("--name", required=True, help="server name shown in the popup")
    px.add_argument("-v", "--verbose", action="store_true")
    px.add_argument("command", nargs=argparse.REMAINDER, help="-- <server command> [args...]")
    px.set_defaults(func=_cmd_proxy)

    g = sub.add_parser("gateway", help="approval gateway in front of a remote (HTTP) MCP server")
    g.add_argument("--upstream", required=True, help="real MCP server URL, e.g. https://example.com/mcp")
    g.add_argument("--name", default="remote", help="server name shown in the popup")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--secret-path", action="store_true",
                   help="serve under a random path prefix so only people with the URL can reach it")
    g.add_argument("-v", "--verbose", action="store_true")
    g.set_defaults(func=_cmd_gateway)

    lg = sub.add_parser("log", help="show recent decisions from the audit log")
    lg.add_argument("-n", type=int, default=20)
    lg.set_defaults(func=_cmd_log)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
