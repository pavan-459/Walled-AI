"""Terminal fallback UI for machines without a display (servers, SSH sessions)."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time

from ..models import ApprovalRequest, Verdict
from .base import ApprovalUI


class ConsoleUI(ApprovalUI):
    """Prompts in the terminal, one request at a time, in arrival order."""

    def __init__(self) -> None:
        super().__init__()
        self._queue: "queue.Queue[tuple[ApprovalRequest, float]]" = queue.Queue()
        self._dismissed: set[str] = set()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, name="console-ui", daemon=True)
        self._worker.start()

    def show(self, request: ApprovalRequest, deadline: float) -> None:
        self._queue.put((request, deadline))

    def dismiss(self, request_id: str, reason: str) -> None:
        with self._lock:
            self._dismissed.add(request_id)

    def _loop(self) -> None:
        while True:
            request, deadline = self._queue.get()
            with self._lock:
                if request.id in self._dismissed:
                    continue
            remaining = int(deadline - time.time())
            args = json.dumps(request.arguments, indent=2, ensure_ascii=False, default=str)
            print(
                "\n=== MCP Guard: approval required ===\n"
                f"  client : {request.client_label}\n"
                f"  server : {request.server_name}\n"
                f"  method : {request.method}\n"
                f"  target : {request.target}\n"
                f"  args   : {args}\n"
                f"  (auto-block in {remaining}s, {self._queue.qsize()} more waiting)",
                flush=True,
            )
            answer = ""
            while answer not in ("a", "b"):
                sys.stdout.write("Allow or Block? [a/b] ")
                sys.stdout.flush()
                line = sys.stdin.readline()
                if not line:  # stdin closed: we can't ask, so block
                    answer = "b"
                    break
                answer = line.strip().lower()[:1]
            with self._lock:
                if request.id in self._dismissed:
                    print("  (request already resolved: timed out or cancelled)")
                    continue
            self.decide(request.id, Verdict.ALLOW if answer == "a" else Verdict.BLOCK)
