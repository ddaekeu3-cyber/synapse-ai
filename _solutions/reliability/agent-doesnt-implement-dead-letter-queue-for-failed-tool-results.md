---
title: "Agent Doesn't Implement Dead Letter Queue for Failed Tool Results"
description: "Agents that silently discard failed tool call results lose the context needed to retry, escalate, or diagnose the failure. When a tool call fails mid-execution — network error, validation failure, dependency outage — the error disappears from the agent's working memory and is never available for offline analysis. Implement a dead letter queue that captures failed tool results with full context, supports retry scheduling, and provides operational visibility into failure patterns."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dead-letter-queue-for-failed-tool-results
tags: [dead-letter-queue, failed-tool-results, retry-scheduling, failure-persistence, error-recovery, tool-failure-analysis]
symptoms:
  - "Failed tool calls disappear silently — no record of what was attempted or why it failed"
  - "Transient failures during peak load are never retried because the context is gone"
  - "No way to replay failed tool calls after a dependency recovers"
  - "Error patterns across sessions cannot be analyzed because failures are not persisted"
  - "On-call engineers see high error rates but cannot identify which tools are failing"
---

## Why This Happens

Tool call failures are treated as exceptions to be caught and handled immediately or propagated. The exception handling path discards the failed call context — the tool name, arguments, session state, and error details — once the exception is caught. A dead letter queue preserves this context by recording the failed call before discarding it, enabling delayed retry, offline analysis, and systematic monitoring of failure rates by tool, error type, and time window.

## Solution 1: Dead Letter Entry

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class DLQEntryStatus(str, Enum):
    PENDING = "pending"         # awaiting retry
    RETRYING = "retrying"       # retry in progress
    RESOLVED = "resolved"       # retry succeeded
    EXHAUSTED = "exhausted"     # max retries reached
    DISCARDED = "discarded"     # manually or TTL-discarded


@dataclass
class DeadLetterEntry:
    entry_id: str
    tool_name: str
    args: Any
    session_id: str
    error_type: str
    error_message: str
    failed_at: float = field(default_factory=time.time)
    attempt_count: int = 1
    max_attempts: int = 3
    next_retry_at: Optional[float] = None
    status: DLQEntryStatus = DLQEntryStatus.PENDING
    resolved_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_retryable(self) -> bool:
        return (
            self.status == DLQEntryStatus.PENDING
            and self.attempt_count <= self.max_attempts
            and (self.next_retry_at is None or time.time() >= self.next_retry_at)
        )

    def schedule_retry(self, delay_seconds: float) -> None:
        self.next_retry_at = time.time() + delay_seconds
        self.status = DLQEntryStatus.PENDING
```

## Solution 2: Dead Letter Queue

```python
import time
import uuid
from threading import Lock
from typing import Any, Dict, List, Optional


class DeadLetterQueue:
    """
    Persists failed tool call entries and provides retry scheduling,
    status tracking, and pattern analysis.
    """

    def __init__(self, max_entries: int = 10_000, ttl_seconds: float = 86400.0):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._entries: Dict[str, DeadLetterEntry] = {}
        self._lock = Lock()

    def enqueue(
        self,
        tool_name: str,
        args: Any,
        session_id: str,
        error_type: str,
        error_message: str,
        max_attempts: int = 3,
        metadata: Optional[dict] = None,
    ) -> DeadLetterEntry:
        entry = DeadLetterEntry(
            entry_id=str(uuid.uuid4())[:12],
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            error_type=error_type,
            error_message=error_message[:500],
            max_attempts=max_attempts,
            metadata=metadata or {},
        )
        with self._lock:
            if len(self._entries) >= self._max:
                self._evict_oldest()
            self._entries[entry.entry_id] = entry
        return entry

    def _evict_oldest(self) -> None:
        if not self._entries:
            return
        oldest_id = min(self._entries, key=lambda k: self._entries[k].failed_at)
        del self._entries[oldest_id]

    def pending_retries(self) -> List[DeadLetterEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.is_retryable()]

    def mark_resolved(self, entry_id: str) -> None:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry:
                entry.status = DLQEntryStatus.RESOLVED
                entry.resolved_at = time.time()

    def mark_exhausted(self, entry_id: str) -> None:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry:
                entry.status = DLQEntryStatus.EXHAUSTED

    def evict_expired(self) -> int:
        cutoff = time.time() - self._ttl
        with self._lock:
            expired = [k for k, e in self._entries.items() if e.failed_at < cutoff]
            for k in expired:
                del self._entries[k]
        return len(expired)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [e for e in self._entries.values() if e.failed_at >= cutoff]

        by_status: dict = {}
        by_tool: dict = {}
        for e in recent:
            by_status[e.status.value] = by_status.get(e.status.value, 0) + 1
            by_tool[e.tool_name] = by_tool.get(e.tool_name, 0) + 1

        return {
            "window_seconds": window_seconds,
            "total_entries": len(recent),
            "by_status": by_status,
            "top_failing_tools": sorted(
                [{"tool": k, "count": v} for k, v in by_tool.items()],
                key=lambda x: -x["count"],
            )[:5],
        }
```

## Solution 3: DLQ Retry Scheduler

```python
import asyncio
import time
from typing import Any, Callable, Optional


class DLQRetryScheduler:
    """
    Pulls pending entries from the DLQ and retries them with
    exponential backoff. Marks entries resolved or exhausted based
    on retry outcome.
    """

    def __init__(
        self,
        dlq: DeadLetterQueue,
        base_delay_seconds: float = 5.0,
        backoff_multiplier: float = 2.0,
        max_delay_seconds: float = 300.0,
    ):
        self._dlq = dlq
        self._base = base_delay_seconds
        self._multiplier = backoff_multiplier
        self._max_delay = max_delay_seconds

    def _next_delay(self, attempt: int) -> float:
        delay = self._base * (self._multiplier ** (attempt - 1))
        return min(delay, self._max_delay)

    async def retry_once(
        self,
        entry: DeadLetterEntry,
        dispatch_fn: Callable,
    ) -> bool:
        """Returns True if retry succeeded."""
        entry.status = DLQEntryStatus.RETRYING
        entry.attempt_count += 1
        try:
            await dispatch_fn(entry.tool_name, entry.args)
            self._dlq.mark_resolved(entry.entry_id)
            return True
        except Exception:
            if entry.attempt_count >= entry.max_attempts:
                self._dlq.mark_exhausted(entry.entry_id)
            else:
                delay = self._next_delay(entry.attempt_count)
                entry.schedule_retry(delay)
            return False

    async def process_pending(self, dispatch_fn: Callable) -> dict:
        pending = self._dlq.pending_retries()
        results = {"attempted": len(pending), "succeeded": 0, "failed": 0}
        for entry in pending:
            success = await self.retry_once(entry, dispatch_fn)
            if success:
                results["succeeded"] += 1
            else:
                results["failed"] += 1
        return results
```

## Solution 4: DLQ-Integrated Tool Executor

```python
import time
from typing import Any, Callable, Optional


class DLQIntegratedToolExecutor:
    """
    Wraps tool execution and automatically enqueues failures to the DLQ.
    """

    def __init__(
        self,
        dlq: DeadLetterQueue,
        max_attempts: int = 3,
    ):
        self._dlq = dlq
        self._max_attempts = max_attempts

    async def execute(
        self,
        tool_name: str,
        args: Any,
        fn: Callable,
        session_id: str,
        metadata: Optional[dict] = None,
    ) -> Any:
        try:
            return await fn()
        except Exception as exc:
            self._dlq.enqueue(
                tool_name=tool_name,
                args=args,
                session_id=session_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                max_attempts=self._max_attempts,
                metadata=metadata or {},
            )
            raise
```

## Solution 5: DLQ Pattern Analyzer

```python
from typing import Dict, List


class DLQPatternAnalyzer:
    """
    Identifies systematic failure patterns in the DLQ:
    - Tools that fail repeatedly across sessions (infrastructure issue)
    - Sessions with high DLQ entry rates (adversarial or buggy client)
    - Error types that cluster around specific time windows (outages)
    """

    def __init__(self, dlq: DeadLetterQueue):
        self._dlq = dlq

    def analyze(self, window_seconds: float = 3600.0) -> dict:
        import time
        cutoff = time.time() - window_seconds
        with self._dlq._lock:
            recent = [e for e in self._dlq._entries.values() if e.failed_at >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "entries": 0}

        by_error_type: Dict[str, int] = {}
        by_session: Dict[str, int] = {}
        for e in recent:
            by_error_type[e.error_type] = by_error_type.get(e.error_type, 0) + 1
            by_session[e.session_id] = by_session.get(e.session_id, 0) + 1

        high_rate_sessions = [
            {"session_id": sid, "failures": count}
            for sid, count in by_session.items()
            if count >= 5
        ]

        return {
            "window_seconds": window_seconds,
            "total_entries": len(recent),
            "exhausted_count": sum(1 for e in recent if e.status == DLQEntryStatus.EXHAUSTED),
            "top_error_types": sorted(
                [{"type": k, "count": v} for k, v in by_error_type.items()],
                key=lambda x: -x["count"],
            )[:5],
            "high_failure_sessions": sorted(high_rate_sessions, key=lambda x: -x["failures"])[:5],
        }
```

## Solution 6: DLQ Dashboard

```python
import time


class DeadLetterQueueDashboard:
    """
    Renders DLQ health, pending retry counts, pattern analysis,
    and resolution rates for operational visibility.
    """

    def __init__(
        self,
        dlq: DeadLetterQueue,
        analyzer: DLQPatternAnalyzer,
    ):
        self._dlq = dlq
        self._analyzer = analyzer

    def render(self) -> dict:
        pending = self._dlq.pending_retries()
        return {
            "generated_at": time.time(),
            "summary_1h": self._dlq.summary(3600.0),
            "summary_24h": self._dlq.summary(86400.0),
            "pending_retries": len(pending),
            "patterns_1h": self._analyzer.analyze(3600.0),
        }
```

## Comparison

| Approach | Failure Persistence | Retry Scheduling | Auto-Enqueue | Pattern Analysis | Dashboard |
|---|---|---|---|---|---|
| DeadLetterQueue | Yes | Via schedule_retry | No | Via summary | No |
| DLQRetryScheduler | No | Yes (exponential) | No | No | No |
| DLQIntegratedToolExecutor | Via DLQ | No | Yes | No | No |
| DLQPatternAnalyzer | Via DLQ | No | No | Yes | No |
| DeadLetterQueueDashboard | No | No | No | Via analyzer | Yes |

**Best for production**: Enqueue to the DLQ on every tool failure, including transient network errors — the DLQ is the source of truth for retry decisions. Set `max_attempts=3` with exponential backoff: 5s, 10s, 20s. Run `DLQRetryScheduler.process_pending()` on a background task every 30 seconds. Alert when `exhausted_count` in the 1-hour summary exceeds 10 — this means tools are failing persistently across all retry attempts, indicating an infrastructure issue that requires operator intervention rather than automatic recovery.
