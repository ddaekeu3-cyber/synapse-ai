---
title: "Agent Doesn't Implement Dead Letter Queue for Failed Tool Calls"
description: "Agents that discard failed tool calls after retry exhaustion lose the work silently: the user gets a degraded response, the failure is logged once, and the original intent is gone. Implement a dead letter queue that captures failed tool calls with full context, supports manual or automated replay, and provides visibility into what work was lost and why."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dead-letter-queue-for-failed-tool-calls
tags: [dead-letter-queue, dlq, failed-tool-calls, replay, fault-tolerance, work-preservation]
symptoms:
  - "Failed tool calls are logged and discarded — no way to retry them after the session ends"
  - "Cannot audit which tool calls failed over the past 24 hours and why"
  - "Transient failures (network blip, provider restart) permanently lose the work"
  - "No mechanism to replay a failed batch of tool calls after a dependency recovers"
  - "Post-incident review cannot reconstruct what operations were dropped"
---

## Why This Happens

The standard retry loop exhausts attempts and raises the final exception to the caller. If the caller catches it and continues (graceful degradation), the failed operation is gone. There is no record of what was attempted, no way to retry it later, and no visibility into the failure pattern. A dead letter queue captures the failed call at the point of final failure — preserving the tool name, arguments, error, and retry history — and stores it durably so it can be inspected, replayed, or escalated. This converts silent data loss into a visible, actionable queue.

## Solution 1: Dead Letter Entry

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DLQEntryStatus(str, Enum):
    PENDING = "pending"         # waiting for replay or manual action
    REPLAYING = "replaying"     # currently being retried
    RESOLVED = "resolved"       # replayed successfully
    ABANDONED = "abandoned"     # manually marked as unresolvable


@dataclass
class FailedAttempt:
    attempt_number: int
    error_type: str
    error_message: str
    attempted_at: float


@dataclass
class DLQEntry:
    entry_id: str
    tool_name: str
    args: Dict[str, Any]
    session_id: str
    failed_attempts: List[FailedAttempt]
    status: DLQEntryStatus = DLQEntryStatus.PENDING
    created_at: float = field(default_factory=time.time)
    last_updated_at: float = field(default_factory=time.time)
    replay_count: int = 0
    resolved_at: Optional[float] = None
    abandonment_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str,
        failed_attempts: List[FailedAttempt],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "DLQEntry":
        return cls(
            entry_id=uuid.uuid4().hex,
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            failed_attempts=failed_attempts,
            metadata=metadata or {},
        )

    def last_error(self) -> Optional[str]:
        if not self.failed_attempts:
            return None
        return self.failed_attempts[-1].error_message

    def age_seconds(self) -> float:
        return time.time() - self.created_at
```

## Solution 2: Dead Letter Queue Store

```python
import threading
import time
from typing import Callable, Dict, List, Optional


class DeadLetterQueueStore:
    """
    In-process DLQ store with filtering, status management, and eviction.
    For production, back this with a database or persistent queue.
    """

    def __init__(
        self,
        max_entries: int = 10_000,
        evict_after_seconds: float = 86400.0 * 7,  # 7 days
    ):
        self._entries: Dict[str, DLQEntry] = {}
        self._max = max_entries
        self._evict_after = evict_after_seconds
        self._lock = threading.Lock()

    def put(self, entry: DLQEntry) -> None:
        with self._lock:
            if len(self._entries) >= self._max:
                self._evict_oldest()
            self._entries[entry.entry_id] = entry

    def get(self, entry_id: str) -> Optional[DLQEntry]:
        return self._entries.get(entry_id)

    def list_pending(self, tool_name: Optional[str] = None) -> List[DLQEntry]:
        return [
            e for e in self._entries.values()
            if e.status == DLQEntryStatus.PENDING
            and (tool_name is None or e.tool_name == tool_name)
        ]

    def list_all(self, status: Optional[DLQEntryStatus] = None) -> List[DLQEntry]:
        return [
            e for e in self._entries.values()
            if status is None or e.status == status
        ]

    def mark_resolved(self, entry_id: str) -> None:
        entry = self._entries.get(entry_id)
        if entry:
            entry.status = DLQEntryStatus.RESOLVED
            entry.resolved_at = time.time()
            entry.last_updated_at = time.time()

    def mark_abandoned(self, entry_id: str, reason: str) -> None:
        entry = self._entries.get(entry_id)
        if entry:
            entry.status = DLQEntryStatus.ABANDONED
            entry.abandonment_reason = reason
            entry.last_updated_at = time.time()

    def _evict_oldest(self) -> None:
        cutoff = time.time() - self._evict_after
        stale = [
            eid for eid, e in self._entries.items()
            if e.created_at < cutoff
            and e.status in (DLQEntryStatus.RESOLVED, DLQEntryStatus.ABANDONED)
        ]
        for eid in stale[:max(1, len(stale) // 2)]:
            del self._entries[eid]

    def summary(self) -> dict:
        by_status: Dict[str, int] = {}
        by_tool: Dict[str, int] = {}
        for e in self._entries.values():
            by_status[e.status] = by_status.get(e.status, 0) + 1
            if e.status == DLQEntryStatus.PENDING:
                by_tool[e.tool_name] = by_tool.get(e.tool_name, 0) + 1
        return {
            "total": len(self._entries),
            "by_status": by_status,
            "pending_by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
        }
```

## Solution 3: DLQ-Integrated Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class DLQIntegratedToolExecutor:
    """
    Wraps tool calls with retry logic and deposits to the DLQ on final failure.
    Records each failed attempt with error details for replay context.
    """

    def __init__(
        self,
        dlq: DeadLetterQueueStore,
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ):
        self._dlq = dlq
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        session_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Any:
        failed_attempts: list = []

        for attempt in range(self._max_retries + 1):
            try:
                return await tool_fn(**args)
            except Exception as exc:
                failed_attempts.append(FailedAttempt(
                    attempt_number=attempt + 1,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:300],
                    attempted_at=time.time(),
                ))
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2 ** attempt))

        # All retries exhausted — deposit to DLQ
        entry = DLQEntry.create(
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            failed_attempts=failed_attempts,
            metadata=metadata or {},
        )
        self._dlq.put(entry)
        raise RuntimeError(
            f"Tool '{tool_name}' failed after {self._max_retries + 1} attempts "
            f"and was sent to DLQ (entry_id={entry.entry_id}). "
            f"Last error: {failed_attempts[-1].error_message}"
        )
```

## Solution 4: DLQ Replay Engine

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class DLQReplayEngine:
    """
    Replays pending DLQ entries for a given tool or all tools.
    Marks entries resolved on success or re-increments replay_count on failure.
    Supports rate-limited batch replay to avoid thundering-herd after an outage.
    """

    def __init__(
        self,
        dlq: DeadLetterQueueStore,
        tool_registry: Dict[str, Callable],
        max_concurrent: int = 4,
        replay_delay_seconds: float = 0.5,
    ):
        self._dlq = dlq
        self._registry = tool_registry
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._replay_delay = replay_delay_seconds

    async def replay_entry(self, entry: DLQEntry) -> bool:
        tool_fn = self._registry.get(entry.tool_name)
        if tool_fn is None:
            self._dlq.mark_abandoned(entry.entry_id, f"tool '{entry.tool_name}' not in registry")
            return False

        entry.status = DLQEntryStatus.REPLAYING
        entry.replay_count += 1
        entry.last_updated_at = time.time()

        async with self._semaphore:
            try:
                await tool_fn(**entry.args)
                self._dlq.mark_resolved(entry.entry_id)
                return True
            except Exception as exc:
                entry.status = DLQEntryStatus.PENDING
                entry.failed_attempts.append(FailedAttempt(
                    attempt_number=len(entry.failed_attempts) + 1,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:300],
                    attempted_at=time.time(),
                ))
                entry.last_updated_at = time.time()
                return False

    async def replay_all_pending(
        self, tool_name: Optional[str] = None, max_replay_count: int = 3
    ) -> dict:
        pending = [
            e for e in self._dlq.list_pending(tool_name)
            if e.replay_count < max_replay_count
        ]
        results = {"attempted": len(pending), "resolved": 0, "still_failed": 0}

        for entry in pending:
            success = await self.replay_entry(entry)
            if success:
                results["resolved"] += 1
            else:
                results["still_failed"] += 1
            await asyncio.sleep(self._replay_delay)

        return results
```

## Solution 5: DLQ Alert Manager

```python
import time
from typing import Callable, List, Optional


class DLQAlertManager:
    """
    Fires alerts when the DLQ accumulates too many entries or
    when specific high-priority tools have pending failures.
    """

    def __init__(
        self,
        dlq: DeadLetterQueueStore,
        pending_warning_threshold: int = 10,
        pending_critical_threshold: int = 50,
        age_warning_seconds: float = 3600.0,
        cooldown_seconds: float = 300.0,
    ):
        self._dlq = dlq
        self._warn_threshold = pending_warning_threshold
        self._crit_threshold = pending_critical_threshold
        self._age_warn = age_warning_seconds
        self._cooldown = cooldown_seconds
        self._last_fired: dict = {}
        self._handlers: List[Callable[[dict], None]] = []

    def add_handler(self, fn: Callable[[dict], None]) -> None:
        self._handlers.append(fn)

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def _fire(self, alert: dict) -> None:
        for h in self._handlers:
            try:
                h(alert)
            except Exception:
                pass

    def check(self) -> List[dict]:
        summary = self._dlq.summary()
        pending_count = summary["by_status"].get(DLQEntryStatus.PENDING, 0)
        alerts = []

        if pending_count >= self._crit_threshold and self._can_fire("dlq:critical"):
            alert = {
                "type": "dlq_critical",
                "severity": "critical",
                "pending": pending_count,
                "message": f"DLQ has {pending_count} pending entries (critical threshold {self._crit_threshold})",
            }
            alerts.append(alert)
            self._fire(alert)
        elif pending_count >= self._warn_threshold and self._can_fire("dlq:warning"):
            alert = {
                "type": "dlq_warning",
                "severity": "warning",
                "pending": pending_count,
                "message": f"DLQ has {pending_count} pending entries",
            }
            alerts.append(alert)
            self._fire(alert)

        # Check for old pending entries
        old_entries = [
            e for e in self._dlq.list_pending()
            if e.age_seconds() > self._age_warn
        ]
        if old_entries and self._can_fire("dlq:stale"):
            alert = {
                "type": "dlq_stale_entries",
                "severity": "warning",
                "count": len(old_entries),
                "oldest_seconds": round(max(e.age_seconds() for e in old_entries), 0),
                "message": f"{len(old_entries)} DLQ entries older than {self._age_warn}s without replay",
            }
            alerts.append(alert)
            self._fire(alert)

        return alerts
```

## Solution 6: DLQ Dashboard

```python
import time


class DLQDashboard:
    """Combines DLQ summary, pending entries, and alert state."""

    def __init__(
        self,
        dlq: DeadLetterQueueStore,
        alert_manager: DLQAlertManager,
    ):
        self._dlq = dlq
        self._alerts = alert_manager

    def render(self) -> dict:
        summary = self._dlq.summary()
        alerts = self._alerts.check()
        recent_pending = sorted(
            self._dlq.list_pending(),
            key=lambda e: e.created_at,
            reverse=True,
        )[:10]

        return {
            "generated_at": time.time(),
            "summary": summary,
            "recent_pending": [
                {
                    "entry_id": e.entry_id,
                    "tool_name": e.tool_name,
                    "session_id": e.session_id,
                    "age_seconds": round(e.age_seconds(), 0),
                    "replay_count": e.replay_count,
                    "last_error": e.last_error(),
                }
                for e in recent_pending
            ],
            "alerts": alerts,
            "healthy": len(alerts) == 0 and summary["by_status"].get(DLQEntryStatus.PENDING, 0) == 0,
        }
```

## Comparison

| Approach | Entry Capture | Status Management | Replay Support | Alerts | Dashboard |
|---|---|---|---|---|---|
| DeadLetterQueueStore | Yes | Yes (4 states) | No | No | No |
| DLQIntegratedToolExecutor | Yes (on final failure) | No | No | No | No |
| DLQReplayEngine | No | Via store | Yes (batch + rate-limited) | No | No |
| DLQAlertManager | No | No | No | Yes (threshold + age) | No |
| DLQDashboard | No | No | No | Via manager | Yes |

**Best for production**: Use `DLQIntegratedToolExecutor` for all non-critical tool calls — critical tools should still propagate exceptions immediately. Set `max_retries=3` with exponential backoff before DLQ deposit. After an infrastructure incident (provider outage, database restart), call `DLQReplayEngine.replay_all_pending()` with `max_replay_count=3` to drain the queue without re-depositing entries that have already been retried many times. Wire `DLQAlertManager` to PagerDuty with `pending_critical_threshold=50` — a DLQ growing past 50 entries indicates a systemic failure requiring immediate attention, not just a transient blip.
