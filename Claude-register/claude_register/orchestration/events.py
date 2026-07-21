"""Thread-safe, bounded history for browser-safe run events."""

from __future__ import annotations

import copy
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any


_SUMMARY_KEYS = (
    "total",
    "success",
    "partial",
    "failed",
    "running",
    "pending",
    "kyc_pass",
    "kyc_required",
    "kyc_dead",
    "kyc_unknown",
)


def _task_summary_contribution(task: dict[str, Any]) -> dict[str, int]:
    contribution = dict.fromkeys(_SUMMARY_KEYS, 0)
    contribution["total"] = 1
    status = str(task.get("status", ""))
    if status in ("success", "partial", "failed", "running", "pending"):
        contribution[status] = 1

    kyc_status = str(task.get("kyc_status", ""))
    if kyc_status in ("not_required", "approved"):
        contribution["kyc_pass"] = 1
    elif kyc_status in ("pending", "denied"):
        contribution["kyc_required"] = 1

    completed_with_session = bool(task.get("has_session")) and status in (
        "success",
        "partial",
    )
    if completed_with_session and kyc_status == "dead":
        contribution["kyc_dead"] = 1
    elif completed_with_session and kyc_status not in (
        "not_required",
        "approved",
        "pending",
        "denied",
        "dead",
    ):
        contribution["kyc_unknown"] = 1
    return contribution


@dataclass(frozen=True)
class RunEvent:
    """One replayable SSE event containing only public data."""

    event_id: int
    event_type: str
    data: dict[str, Any]


class RunEventBus:
    """Publish and replay a bounded sequence of run events."""

    def __init__(self, max_history: int = 4096) -> None:
        if max_history < 1:
            raise ValueError("max_history must be positive")
        self._condition = threading.Condition()
        self._events: deque[RunEvent] = deque(maxlen=max_history)
        self._next_event_id = 1

    @property
    def cursor(self) -> int:
        """Return the most recently allocated event ID."""
        with self._condition:
            return self._next_event_id - 1

    def reset(self) -> None:
        """Discard replay history while preserving globally increasing IDs."""
        with self._condition:
            self._events.clear()
            self._condition.notify_all()

    def publish(self, event_type: str, data: dict[str, Any]) -> RunEvent:
        """Append one event and wake connected SSE consumers."""
        with self._condition:
            event = RunEvent(
                event_id=self._next_event_id,
                event_type=event_type,
                data=copy.deepcopy(data),
            )
            self._next_event_id += 1
            self._events.append(event)
            self._condition.notify_all()
            return event

    def _events_after_locked(self, event_id: int) -> list[RunEvent] | None:
        current = self._next_event_id - 1
        if event_id > current:
            return None
        if not self._events:
            return [] if event_id == current else None
        oldest = self._events[0].event_id
        if event_id < oldest - 1:
            return None
        return [event for event in self._events if event.event_id > event_id]

    def events_after(self, event_id: int) -> list[RunEvent] | None:
        """Return newer events, or None when the requested history was evicted."""
        with self._condition:
            return self._events_after_locked(event_id)

    def wait_after(self, event_id: int, timeout: float) -> list[RunEvent] | None:
        """Wait for a newer event, returning None if the cursor cannot be replayed."""
        with self._condition:
            available = self._events_after_locked(event_id)
            if available is None or available:
                return available
            self._condition.wait(max(0.0, timeout))
            return self._events_after_locked(event_id)


class RunSummaryTracker:
    """Maintain public run counters in constant time per task update."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contributions: dict[str, dict[str, int]] = {}
        self._summary = dict.fromkeys(_SUMMARY_KEYS, 0)

    def _replace_locked(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id", ""))
        if not task_id:
            raise ValueError("task_id is required")
        previous = self._contributions.get(task_id)
        if previous:
            for key in _SUMMARY_KEYS:
                self._summary[key] -= previous[key]
        current = _task_summary_contribution(task)
        self._contributions[task_id] = current
        for key in _SUMMARY_KEYS:
            self._summary[key] += current[key]

    def reset(self, tasks: list[dict[str, Any]]) -> dict[str, int]:
        """Replace all tracked task states and return their summary."""
        with self._lock:
            self._contributions.clear()
            self._summary = dict.fromkeys(_SUMMARY_KEYS, 0)
            for task in tasks:
                self._replace_locked(task)
            return dict(self._summary)

    def update(self, task: dict[str, Any]) -> dict[str, int]:
        """Replace one task's contribution and return the current summary."""
        with self._lock:
            self._replace_locked(task)
            return dict(self._summary)

    def snapshot(self) -> dict[str, int]:
        """Return the current public counters."""
        with self._lock:
            return dict(self._summary)
