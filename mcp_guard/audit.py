"""Append-only JSONL audit log of every decision the daemon makes."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .models import ApprovalRequest, Decision

log = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, path: Path, include_arguments: bool = True, max_chars: int = 2000) -> None:
        self.path = path
        self.include_arguments = include_arguments
        self.max_chars = max_chars
        self._lock = threading.Lock()

    def record(self, request: ApprovalRequest, decision: Decision) -> None:
        entry: dict[str, Any] = {
            "ts": decision.decided_at,
            "request_id": request.id,
            "source": request.source,
            "client": request.client_name,
            "server": request.server_name,
            "method": request.method,
            "target": request.target,
            "verdict": decision.verdict.value,
            "reason": decision.reason.value,
            "wait_s": round(decision.decided_at - request.created_at, 3),
        }
        if self.include_arguments:
            args = json.dumps(request.arguments, ensure_ascii=False, default=str)
            entry["arguments"] = args if len(args) <= self.max_chars else args[: self.max_chars] + "…"
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # auditing must never break request handling
            log.warning("could not write audit log: %s", exc)
