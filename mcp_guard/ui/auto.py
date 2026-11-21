"""Non-interactive UI: decides by rule. Used for tests, CI and headless smoke checks."""

from __future__ import annotations

import threading
from typing import Callable

from ..models import ApprovalRequest, Verdict
from .base import ApprovalUI

# Return a verdict, or None to leave the request pending (useful for timeout tests).
Policy = Callable[[ApprovalRequest], "Verdict | None"]


class AutoUI(ApprovalUI):
    def __init__(self, policy: Policy, delay: float = 0.0) -> None:
        super().__init__()
        self.policy = policy
        self.delay = delay
        self.shown: list[ApprovalRequest] = []
        self.dismissed: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    @classmethod
    def constant(cls, verdict: Verdict, delay: float = 0.0) -> "AutoUI":
        return cls(lambda _req: verdict, delay)

    def show(self, request: ApprovalRequest, deadline: float) -> None:
        with self._lock:
            self.shown.append(request)
        verdict = self.policy(request)
        if verdict is None:
            return
        if self.delay > 0:
            timer = threading.Timer(self.delay, self.decide, args=(request.id, verdict))
            timer.daemon = True
            timer.start()
        else:
            self.decide(request.id, verdict)

    def dismiss(self, request_id: str, reason: str) -> None:
        with self._lock:
            self.dismissed.append((request_id, reason))
