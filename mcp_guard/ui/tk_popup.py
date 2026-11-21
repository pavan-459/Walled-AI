"""Always-on-top approval popups built with Tkinter (ships with Python, no extra deps).

Tk is not thread-safe, so the daemon's asyncio thread never touches widgets directly:
``show``/``dismiss`` push commands onto a queue that the Tk main loop drains every
50 ms. Each pending request gets its own window, cascaded so simultaneous requests
are all visible and can be answered in any order.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from datetime import datetime
from typing import Any

from ..models import ApprovalRequest, Verdict
from .base import ApprovalUI

POLL_MS = 50
MAX_ARG_CHARS = 20_000

COLORS = {
    "header_bg": "#1f2937",
    "header_fg": "#f9fafb",
    "accent": "#f59e0b",
    "allow": "#15803d",
    "block": "#b91c1c",
    "muted": "#6b7280",
}


class TkUI(ApprovalUI):
    needs_main_thread = True

    def __init__(self) -> None:
        super().__init__()
        self._commands: "queue.Queue[tuple[str, Any, Any]]" = queue.Queue()
        self._popups: dict[str, "_Popup"] = {}
        self._root: Any = None

    # Called from the daemon thread -------------------------------------------------
    def show(self, request: ApprovalRequest, deadline: float) -> None:
        self._commands.put(("show", request, deadline))

    def dismiss(self, request_id: str, reason: str) -> None:
        self._commands.put(("dismiss", request_id, reason))

    # Main thread -------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        import tkinter as tk

        _enable_dpi_awareness()
        root = tk.Tk()
        root.withdraw()  # no main window: the daemon lives in the background
        root.title("MCP Guard")
        self._root = root

        def poll() -> None:
            if stop.is_set():
                root.quit()
                return
            self._drain(root)
            root.after(POLL_MS, poll)

        root.after(POLL_MS, poll)
        root.mainloop()
        for popup in list(self._popups.values()):
            popup.close()
        self._popups.clear()
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _drain(self, root: Any) -> None:
        while True:
            try:
                kind, a, b = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "show":
                    self._open(root, a, b)
                elif kind == "dismiss":
                    popup = self._popups.pop(a, None)
                    if popup is not None:
                        popup.close()
                        self._refresh_titles()
            except Exception as exc:  # a broken window must never take the daemon down
                print(f"[mcp-guard] UI error: {exc}", file=sys.stderr)
                if kind == "show":
                    self.decide(a.id, Verdict.BLOCK)

    def _open(self, root: Any, request: ApprovalRequest, deadline: float) -> None:
        if request.id in self._popups:
            return
        popup = _Popup(root, request, deadline, index=len(self._popups), on_decide=self._on_decide)
        self._popups[request.id] = popup
        self._refresh_titles()

    def _on_decide(self, request_id: str, verdict: Verdict) -> None:
        popup = self._popups.pop(request_id, None)
        if popup is not None:
            popup.close()
        self._refresh_titles()
        self.decide(request_id, verdict)

    def _refresh_titles(self) -> None:
        total = len(self._popups)
        for popup in self._popups.values():
            popup.set_pending_count(total)


class _Popup:
    def __init__(self, root: Any, request: ApprovalRequest, deadline: float, index: int, on_decide: Any) -> None:
        import tkinter as tk

        self.request = request
        self.deadline = deadline
        self.on_decide = on_decide
        self.closed = False

        win = tk.Toplevel(root)
        self.win = win
        win.title("MCP Guard: approval required")
        win.configure(padx=0, pady=0)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", lambda: self._decide(Verdict.BLOCK))
        win.bind("<Escape>", lambda _e: self._decide(Verdict.BLOCK))

        # Header
        header = tk.Frame(win, bg=COLORS["header_bg"], padx=16, pady=12)
        header.pack(fill="x")
        tk.Label(
            header, text="Claude wants to use an MCP tool", bg=COLORS["header_bg"], fg=COLORS["header_fg"],
            font=("TkDefaultFont", 13, "bold"), anchor="w",
        ).pack(fill="x")
        tk.Label(
            header, text="Review the request below. Nothing runs until you choose.", bg=COLORS["header_bg"],
            fg="#d1d5db", anchor="w",
        ).pack(fill="x")

        # Details
        body = tk.Frame(win, padx=16, pady=10)
        body.pack(fill="both", expand=True)
        rows = [
            ("Client", request.client_label),
            ("MCP server", request.server_name + (f"  ({request.server_info})" if request.server_info else "")),
            ("Action", request.method),
            ("Tool" if request.tool_name else "Target", request.target),
            ("Requested", datetime.fromtimestamp(request.created_at).strftime("%H:%M:%S")),
        ]
        for r, (label, value) in enumerate(rows):
            tk.Label(body, text=label, fg=COLORS["muted"], anchor="w").grid(row=r, column=0, sticky="w", padx=(0, 12))
            font = ("TkDefaultFont", 11, "bold") if label in ("Tool", "Target") else None
            tk.Label(body, text=value, anchor="w", font=font, wraplength=440, justify="left").grid(
                row=r, column=1, sticky="w"
            )
        tk.Label(body, text="Arguments", fg=COLORS["muted"], anchor="w").grid(
            row=len(rows), column=0, sticky="nw", pady=(8, 0)
        )

        text_frame = tk.Frame(body)
        text_frame.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="nsew", pady=(2, 0))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(len(rows) + 1, weight=1)
        args_text = tk.Text(text_frame, height=10, width=68, wrap="word", font="TkFixedFont", relief="solid", bd=1)
        scroll = tk.Scrollbar(text_frame, command=args_text.yview)
        args_text.configure(yscrollcommand=scroll.set)
        args_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        args_text.insert("1.0", _pretty(request.arguments))
        args_text.configure(state="disabled")

        # Footer: countdown + buttons
        footer = tk.Frame(win, padx=16, pady=12)
        footer.pack(fill="x")
        self.countdown = tk.Label(footer, text="", fg=COLORS["muted"], anchor="w")
        self.countdown.pack(side="left")
        allow_btn = tk.Button(
            footer, text="Allow", width=10, command=lambda: self._decide(Verdict.ALLOW), **_button_colors("allow")
        )
        block_btn = tk.Button(
            footer, text="Block", width=10, command=lambda: self._decide(Verdict.BLOCK), **_button_colors("block")
        )
        allow_btn.pack(side="right", padx=(8, 0))
        block_btn.pack(side="right")

        # Position: centred, cascaded for simultaneous requests
        win.update_idletasks()
        w, h = max(win.winfo_reqwidth(), 560), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        offset = (index % 8) * 32
        x = max(0, (sw - w) // 2 + offset)
        y = max(0, (sh - h) // 3 + offset)
        win.geometry(f"{w}x{h}+{x}+{y}")

        self._raise()
        block_btn.focus_set()  # safe default: keyboard focus on Block, never Allow
        win.bell()
        win.after(250, self._raise)  # some window managers ignore the first raise
        self._tick()

    def _raise(self) -> None:
        if self.closed:
            return
        try:
            self.win.attributes("-topmost", True)
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except Exception:
            pass

    def _tick(self) -> None:
        if self.closed:
            return
        remaining = int(self.deadline - time.time())
        text = f"Auto-blocks in {remaining}s" if remaining > 0 else "Timing out…"
        self.countdown.configure(text=text, fg=COLORS["block"] if remaining <= 15 else COLORS["muted"])
        self.win.after(1000, self._tick)

    def set_pending_count(self, total: int) -> None:
        if not self.closed:
            suffix = f" ({total} pending)" if total > 1 else ""
            self.win.title(f"MCP Guard: approval required{suffix}")

    def _decide(self, verdict: Verdict) -> None:
        if self.closed:
            return
        self.on_decide(self.request.id, verdict)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except Exception:
            pass


def _pretty(value: Any) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if len(text) > MAX_ARG_CHARS:
        text = text[:MAX_ARG_CHARS] + f"\n… ({len(text) - MAX_ARG_CHARS} more characters not shown)"
    return text


def _button_colors(kind: str) -> dict[str, str]:
    # macOS native (Aqua) buttons ignore bg but honour fg, so white text would be
    # invisible on a white button. Keep the native look there so labels stay readable.
    if sys.platform == "darwin":
        return {}
    color = COLORS[kind]
    return {"bg": color, "fg": "white", "activebackground": color, "activeforeground": "white"}


def _enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
