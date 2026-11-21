"""Interface between the daemon and whatever asks the user for a decision."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable

from ..models import ApprovalRequest, Verdict

# Called by a UI when the user decides: decide(request_id, verdict). Must be thread-safe.
DecideFn = Callable[[str, Verdict], None]


class ApprovalUI(ABC):
    """A UI receives pending requests and reports the user's verdict through ``decide``.

    ``show`` and ``dismiss`` are called from the daemon's asyncio thread and must not
    block. UIs that need to own the main thread (Tk) set ``needs_main_thread`` and do
    their work inside ``run``.
    """

    needs_main_thread = False

    def __init__(self) -> None:
        self._decide: DecideFn | None = None

    def bind(self, decide: DecideFn) -> None:
        self._decide = decide

    def decide(self, request_id: str, verdict: Verdict) -> None:
        if self._decide is not None:
            self._decide(request_id, verdict)

    @abstractmethod
    def show(self, request: ApprovalRequest, deadline: float) -> None:
        """Present a new pending request. ``deadline`` is a time.time() timestamp."""

    @abstractmethod
    def dismiss(self, request_id: str, reason: str) -> None:
        """Remove a request from view (decided, timed out or cancelled). Idempotent."""

    def run(self, stop: threading.Event) -> None:
        """Main-thread loop for UIs with ``needs_main_thread``; return when ``stop`` is set."""
        stop.wait()

    def close(self) -> None:
        """Release resources. Called once when the daemon stops."""
