"""Execution budgets and read-only activity snapshots for synchronous requests."""
from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(TimeoutError):
    """The current unit exhausted its shared wall-clock allowance."""


@dataclass(frozen=True)
class UnitBudget:
    seconds: float
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("Unit time budget must be a positive finite number of seconds.")
        object.__setattr__(self, "deadline", time.monotonic() + self.seconds)

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded(f"Unit time budget of {self.seconds:g}s exhausted.")
        return remaining


class ActivityRegistry:
    """Observe request handlers; this class never starts or schedules work."""

    def __init__(self, retained: int = 100):
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._resources: set[str] = set()
        self._retained = retained

    def start(self, request_id: str, phase: str) -> None:
        with self._lock:
            if request_id in self._items:
                raise ValueError("Request ID already used; do not resubmit the same operation.")
            completed = [key for key, item in self._items.items() if item["status"] != "running"]
            for key in completed[:max(0, len(self._items) - self._retained + 1)]:
                del self._items[key]
            self._items[request_id] = {
                "request_id": request_id, "phase": phase, "status": "running",
                "started_at": time.time(), "events": deque(maxlen=5),
            }

    def claim(self, request_id: str, resource: str) -> None:
        """Protect the same paper's artifacts from simultaneous requests."""
        with self._lock:
            item = self._items[request_id]
            if item.get("resource") == resource:
                return
            if resource in self._resources:
                raise ValueError("This paper is already being processed by another request.")
            self._resources.add(resource)
            item["resource"] = resource
            item["paper_id"] = resource

    def event(self, request_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._items[request_id]["events"].append(copy.deepcopy(event))

    def finish(self, request_id: str, error: str | None = None) -> None:
        with self._lock:
            item = self._items[request_id]
            item.update(status="failed" if error else "completed", finished_at=time.time(), error=error)
            self._resources.discard(item.get("resource"))

    def snapshot(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return {"request_id": request_id, "status": "unknown", "running": False, "events": []}
            result = {key: copy.deepcopy(value) for key, value in item.items() if key != "resource"}
            result["events"] = list(result["events"])
            result["running"] = result["status"] == "running"
            result["elapsed_seconds"] = max(0, result.get("finished_at", time.time()) - result["started_at"])
            return result
