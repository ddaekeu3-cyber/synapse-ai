---
title: "Agent Doesn't Implement Dead Letter Queue for Failed Tool Invocations"
description: "Agents that discard failed tool invocations on error lose the ability to retry, inspect, or audit those failures: a tool call that fails due to a transient network error is gone, the task it was serving is silently dropped, and there is no mechanism to replay the call once the dependency recovers. Implement a dead letter queue that captures failed tool invocations with their full context, supports scheduled retry with exponential backoff, and provides an audit trail of all unprocessed failures."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-dead-letter-queue-for-failed-tool-invocations
tags: [dead-letter-queue, failed-tool-calls, retry-queue, fault-tolerance, error-recovery, tool-replay]
symptoms:
  - "Failed tool calls are logged but never retried — transient errors become permanent failures"
  - "No way to replay a tool invocation after the dependency that caused the failure recovers"
  - "Tool failure audit trail disappears on agent restart"
  - "Cannot distinguish a permanently failed invocation from one that needs one more retry"
  - "Tasks silently drop when a downstream tool fails mid-session"
---

## Why This Happens

Tool call failure handling is usually written as a try/except that logs the error and moves on. This is appropriate for immediately retried calls but not for failures that require waiting — the dependency is down, the rate limit needs to expire, or a human needs to intervene. A dead letter queue captures these failed invocations so they are not lost: it stores the full call context (tool name, arguments, session, failure reason), applies a retry schedule, and replays the call when the conditions are met. Without a DLQ, the agent has no durable record of what it tried and failed, making recovery from outages manual and error-prone.

## Solution 1: Dead Letter Entry

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DLQEntryStatus(str, Enum):
    PENDING = "pending"
    RETRYING = "retrying"
    EXHAUSTED = "exhausted"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


@dataclass
class DLQEntry:
    entry_id: str
    tool_name: str
    args: Dict[str, Any]
    session_id: str
    failure_reason: str
    first_failed_at: float
    retry_count: int = 0
    max_retries: int = 5
    next_retry_at: Optional[float] = None
    last_retry_at: Optional[float] = None
    status: DLQEntryStatus = DLQEntryStatus.PENDING
    error_history: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str,
        failure_reason: str,
        max_retries: int = 5,
        initial_delay_seconds: float = 10.0,
    ) -> "DLQEntry":
        return cls(
            entry_id=str(uuid.uuid4())[:12],
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            failure_reason=failure_reason,
            first_failed_at=time.time(),
            max_retries=max_retries,
            next_retry_at=time.time() + initial_delay_seconds,
            error_history=[failure_reason],
        )

    def is_ready_for_retry(self) -> bool:
        if self.status not in (DLQEntryStatus.PENDING, DLQEntryStatus.RETRYING):
            return False
        if self.retry_count >= self.max_retries:
            return False
        return self.next_retry_at is not None and time.time() >= self.next_retry_at

    def schedule_next_retry(self, base_delay: float = 10.0, backoff_factor: float = 2.0) -> None:
        delay = base_delay * (backoff_factor ** self.retry_count)
        delay = min(delay, 3600.0)   # cap at 1 hour
        self.next_retry_at = time.time() + delay
        self.status = DLQEntryStatus.PENDING
```

## Solution 2: Dead Letter Queue Store

```python
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional


class DeadLetterQueueStore:
    """
    Durable in-memory store for DLQ entries backed by a JSON file.
    Survives agent restarts and supports full audit access.
    """

    def __init__(self, path: str = "/tmp/agent_dlq.json"):
        self._path = Path(path)
        self._entries: Dict[str, DLQEntry] = {}
        self._lock = threading.Lock()
        self._load()

    def put(self, entry: DLQEntry) -> None:
        with self._lock:
            self._entries[entry.entry_id] = entry
            self._persist()

    def get(self, entry_id: str) -> Optional[DLQEntry]:
        with self._lock:
            return self._entries.get(entry_id)

    def update(self, entry: DLQEntry) -> None:
        with self._lock:
            self._entries[entry.entry_id] = entry
            self._persist()

    def ready_for_retry(self) -> List[DLQEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.is_ready_for_retry()]

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for e in self._entries.values()
                if e.status in (DLQEntryStatus.PENDING, DLQEntryStatus.RETRYING)
            )

    def all_entries(self) -> List[DLQEntry]:
        with self._lock:
            return list(self._entries.values())

    def _persist(self) -> None:
        data = {}
        for eid, entry in self._entries.items():
            data[eid] = {
                "entry_id": entry.entry_id,
                "tool_name": entry.tool_name,
                "args": entry.args,
                "session_id": entry.session_id,
                "failure_reason": entry.failure_reason,
                "first_failed_at": entry.first_failed_at,
                "retry_count": entry.retry_count,
                "max_retries": entry.max_retries,
                "next_retry_at": entry.next_retry_at,
                "last_retry_at": entry.last_retry_at,
                "status": entry.status.value,
                "error_history": entry.error_history,
                "metadata": entry.metadata,
            }
        try:
            self._path.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            for eid, d in raw.items():
                self._entries[eid] = DLQEntry(
                    entry_id=d["entry_id"],
                    tool_name=d["tool_name"],
                    args=d["args"],
                    session_id=d["session_id"],
                    failure_reason=d["failure_reason"],
                    first_failed_at=d["first_failed_at"],
                    retry_count=d.get("retry_count", 0),
                    max_retries=d.get("max_retries", 5),
                    next_retry_at=d.get("next_retry_at"),
                    last_retry_at=d.get("last_retry_at"),
                    status=DLQEntryStatus(d.get("status", "pending")),
                    error_history=d.get("error_history", []),
                    metadata=d.get("metadata", {}),
                )
        except (json.JSONDecodeError, KeyError, OSError):
            pass
```

## Solution 3: DLQ-Aware Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Optional


class DLQAwareToolDispatcher:
    """
    Wraps tool execution with dead letter queue integration.
    On failure, creates a DLQ entry rather than discarding the invocation.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        max_retries: int = 5,
        initial_delay_seconds: float = 10.0,
    ):
        self._store = dlq_store
        self._max_retries = max_retries
        self._initial_delay = initial_delay_seconds
        self._dlq_enqueued = 0

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: dict,
        session_id: str = "",
    ) -> Any:
        try:
            return await tool_fn(**args)
        except Exception as exc:
            entry = DLQEntry.create(
                tool_name=tool_name,
                args=args,
                session_id=session_id,
                failure_reason=str(exc),
                max_retries=self._max_retries,
                initial_delay_seconds=self._initial_delay,
            )
            self._store.put(entry)
            self._dlq_enqueued += 1
            raise DLQEnqueuedError(tool_name, entry.entry_id, str(exc)) from exc

    def enqueued_count(self) -> int:
        return self._dlq_enqueued


class DLQEnqueuedError(Exception):
    def __init__(self, tool_name: str, entry_id: str, reason: str):
        super().__init__(
            f"tool '{tool_name}' failed and was enqueued in DLQ (entry={entry_id}): {reason}"
        )
        self.tool_name = tool_name
        self.entry_id = entry_id
        self.reason = reason
```

## Solution 4: DLQ Retry Worker

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class DLQRetryWorker:
    """
    Polls the DLQ store at a configurable interval, retries ready entries,
    and updates their status based on the outcome.
    """

    def __init__(
        self,
        store: DeadLetterQueueStore,
        tool_registry: Dict[str, Callable],
        poll_interval_seconds: float = 15.0,
        backoff_base: float = 10.0,
        backoff_factor: float = 2.0,
    ):
        self._store = store
        self._tools = tool_registry
        self._interval = poll_interval_seconds
        self._backoff_base = backoff_base
        self._backoff_factor = backoff_factor
        self._running = False
        self._retries_succeeded = 0
        self._retries_failed = 0
        self._retries_exhausted = 0

    async def _retry_one(self, entry: DLQEntry) -> None:
        tool_fn = self._tools.get(entry.tool_name)
        if tool_fn is None:
            entry.status = DLQEntryStatus.ABANDONED
            entry.error_history.append(f"no handler registered for '{entry.tool_name}'")
            self._store.update(entry)
            return

        entry.status = DLQEntryStatus.RETRYING
        entry.retry_count += 1
        entry.last_retry_at = time.time()
        self._store.update(entry)

        try:
            await tool_fn(**entry.args)
            entry.status = DLQEntryStatus.RESOLVED
            self._retries_succeeded += 1
        except Exception as exc:
            error_msg = str(exc)
            entry.error_history.append(error_msg)
            if entry.retry_count >= entry.max_retries:
                entry.status = DLQEntryStatus.EXHAUSTED
                self._retries_exhausted += 1
            else:
                entry.schedule_next_retry(self._backoff_base, self._backoff_factor)
                self._retries_failed += 1
        finally:
            self._store.update(entry)

    async def poll_once(self) -> int:
        ready = self._store.ready_for_retry()
        for entry in ready:
            await self._retry_one(entry)
        return len(ready)

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self.poll_once()
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "retries_succeeded": self._retries_succeeded,
            "retries_failed": self._retries_failed,
            "retries_exhausted": self._retries_exhausted,
            "pending_in_store": self._store.pending_count(),
        }
```

## Solution 5: DLQ Audit Reporter

```python
import time
from collections import Counter
from typing import List


class DLQAuditReporter:
    """
    Produces an audit report of all DLQ entries grouped by status,
    tool name, and failure reason pattern.
    """

    def __init__(self, store: DeadLetterQueueStore):
        self._store = store

    def report(self) -> dict:
        entries = self._store.all_entries()
        if not entries:
            return {"total": 0}

        by_status = Counter(e.status.value for e in entries)
        by_tool = Counter(e.tool_name for e in entries)
        exhausted = [e for e in entries if e.status == DLQEntryStatus.EXHAUSTED]

        age_seconds = [time.time() - e.first_failed_at for e in entries]

        return {
            "total": len(entries),
            "by_status": dict(by_status),
            "by_tool": dict(by_tool.most_common(10)),
            "exhausted_count": len(exhausted),
            "exhausted_tools": [e.tool_name for e in exhausted[:10]],
            "oldest_entry_age_seconds": round(max(age_seconds), 1) if age_seconds else 0,
            "avg_retry_count": round(
                sum(e.retry_count for e in entries) / len(entries), 2
            ),
        }
```

## Solution 6: DLQ Dashboard

```python
import time


class DeadLetterQueueDashboard:
    """
    Combines DLQ audit, retry worker stats, and pending count
    into a single operational snapshot.
    """

    def __init__(
        self,
        store: DeadLetterQueueStore,
        retry_worker: DLQRetryWorker,
        audit_reporter: DLQAuditReporter,
    ):
        self._store = store
        self._worker = retry_worker
        self._auditor = audit_reporter

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "pending": self._store.pending_count(),
            "retry_worker": self._worker.stats(),
            "audit": self._auditor.report(),
        }
```

## Comparison

| Approach | Failure Capture | Durable Storage | Backoff Retry | Exhaustion Tracking | Audit |
|---|---|---|---|---|---|
| DLQEntry | Yes (dataclass) | No | Via schedule_next_retry | Yes (max_retries) | Via error_history |
| DeadLetterQueueStore | No | Yes (JSON file) | No | No | Yes |
| DLQAwareToolDispatcher | Yes (on exception) | Via store | No | No | No |
| DLQRetryWorker | No | Via store | Yes (exponential) | Yes | No |
| DLQAuditReporter | No | No | No | No | Yes |
| DeadLetterQueueDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Use Redis sorted sets (score = next_retry_at timestamp) as the DLQ backend in multi-instance deployments — this allows any worker instance to claim and retry entries without double-processing. Set `max_retries=5` with `backoff_factor=2.0` starting at 10 seconds, giving a retry window of roughly 5 minutes before exhaustion. Alert immediately when `exhausted_count` grows: exhausted entries represent permanently lost work that may need manual replay or user notification. Store `session_id` in every DLQ entry so that when a batch of entries exhausts simultaneously, they can be grouped by session to identify which users were affected.
