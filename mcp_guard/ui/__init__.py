"""Approval UIs. ``create_ui`` picks one by name."""

from __future__ import annotations

from ..models import Verdict
from .base import ApprovalUI


def create_ui(name: str) -> ApprovalUI:
    if name == "tk":
        from .tk_popup import TkUI

        return TkUI()
    if name == "console":
        from .console import ConsoleUI

        return ConsoleUI()
    if name in ("auto-allow", "auto-block"):
        from .auto import AutoUI

        return AutoUI.constant(Verdict.ALLOW if name == "auto-allow" else Verdict.BLOCK)
    raise ValueError(f"unknown UI '{name}' (expected tk, console, auto-allow or auto-block)")


__all__ = ["ApprovalUI", "create_ui"]
