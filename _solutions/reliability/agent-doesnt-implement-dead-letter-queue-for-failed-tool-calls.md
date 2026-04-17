---
title: "Agent Doesn't Implement Dead Letter Queue for Failed Tool Calls"
description: "Agents that discard failed tool calls after exhausting retries lose work silently: a document processing tool that fails due to a transient upstream error drops the document with no record and no retry path. Implement a dead letter queue that captures persistently-failed tool calls with full context, enabling manual inspection, automated reprocessing when conditions recover, and alerting on accumulation patterns."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-dead-letter-queue-for-failed-tool-calls
tags: [dead-letter-queue, failed-tool-calls, error-recovery, reprocessing, fault-tolerance, message-durability]
symptoms:
  - "Failed tool calls disappear with no persistent record after retry exhaustion"
  - "No way to replay failed operations after a downstream service recovers"
  - "Silent data loss when tool calls fail during high-load periods"
  - "On-call engineers cannot inspect what work was dropped during an incident"
  - "Failure count metrics exist but the failed payloads themselves are unrecoverable"
---

## Why This Happens

Retry loops handle transient failures. What they do not handle is the case where retries are exhausted and the work must not be lost — it must be preserved for later reprocessing or human inspection. Without a dead letter queue (DLQ), the only options after retry exhaustion are: silently drop the work, raise an exception that surfaces to the user, or block indefinitely. A DLQ provides a fourth option: persist the failed call with its context and failure history, then move on. When the downstream service recovers, DLQ entries can be replayed automatically or manually.

## Solution 1: Dead Letter Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FailureRecord:
    attempt_number: int
    error_type: str
    error_message: str
    failed_at: float
    latency_ms: float


@dataclass
class DeadLetterEntry:
    entry_id: str
    tool_name: str
    args: Dict[str, Any]
    conversation_id: str
    failure_records: List[FailureRecord]
    enqueued_at: float = field(default_factory=time.time)
    reprocess_attempts: int = 0
    last_reprocess_at: Optional[float] = None
    resolved: bool = False
    resolution_note: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_attempts(self) -> int:
        return len(self.failure_records)

    @property
    def first_failure_at(self) -> float:
        return self.failure_records[0].failed_at if self.failure_records else self.enqueued_at

    @property
    def age_seconds(self) -> float:
        return time.time() - self.enqueued_at
```

## Solution 2: Dead Letter Queue Store

```python
import json
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class DeadLetterQueueStore:
    """
    Persists dead letter entries to a JSON-lines file.
    Supports listing, filtering, and resolving entries.
    """

    def __init__(self, path: str = "/tmp/agent_dlq.jsonl"):
        self._path = Path(path)
        self._lock = Lock()
        self._entries: Dict[str, DeadLetterEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            for line in self._path.read_text().splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                entry = self._deserialize(data)
                self._entries[entry.entry_id] = entry
        except Exception:
            pass

    def _serialize(self, entry: DeadLetterEntry) -> dict:
        return {
            "entry_id": entry.entry_id,
            "tool_name": entry.tool_name,
            "args": entry.args,
            "conversation_id": entry.conversation_id,
            "failure_records": [
                {
                    "attempt_number": f.attempt_number,
                    "error_type": f.error_type,
                    "error_message": f.error_message,
                    "failed_at": f.failed_at,
                    "latency_ms": f.latency_ms,
                }
                for f in entry.failure_records
            ],
            "enqueued_at": entry.enqueued_at,
            "reprocess_attempts": entry.reprocess_attempts,
            "last_reprocess_at": entry.last_reprocess_at,
            "resolved": entry.resolved,
            "resolution_note": entry.resolution_note,
            "metadata": entry.metadata,
        }

    def _deserialize(self, data: dict) -> DeadLetterEntry:
        return DeadLetterEntry(
            entry_id=data["entry_id"],
            tool_name=data["tool_name"],
            args=data["args"],
            conversation_id=data["conversation_id"],
            failure_records=[FailureRecord(**f) for f in data.get("failure_records", [])],
            enqueued_at=data.get("enqueued_at", time.time()),
            reprocess_attempts=data.get("reprocess_attempts", 0),
            last_reprocess_at=data.get("last_reprocess_at"),
            resolved=data.get("resolved", False),
            resolution_note=data.get("resolution_note", ""),
            metadata=data.get("metadata", {}),
        )

    def _flush(self) -> None:
        lines = [json.dumps(self._serialize(e)) for e in self._entries.values()]
        self._path.write_text("\n".join(lines) + "\n" if lines else "")

    def enqueue(self, entry: DeadLetterEntry) -> None:
        with self._lock:
            self._entries[entry.entry_id] = entry
            self._flush()

    def get(self, entry_id: str) -> Optional[DeadLetterEntry]:
        return self._entries.get(entry_id)

    def pending(self, tool_name: Optional[str] = None) -> List[DeadLetterEntry]:
        entries = [e for e in self._entries.values() if not e.resolved]
        if tool_name:
            entries = [e for e in entries if e.tool_name == tool_name]
        return sorted(entries, key=lambda e: e.enqueued_at)

    def resolve(self, entry_id: str, note: str = "") -> None:
        with self._lock:
            if entry_id in self._entries:
                self._entries[entry_id].resolved = True
                self._entries[entry_id].resolution_note = note
                self._flush()

    def size(self) -> dict:
        total = len(self._entries)
        pending = sum(1 for e in self._entries.values() if not e.resolved)
        return {"total": total, "pending": pending, "resolved": total - pending}
```

## Solution 3: DLQ-Backed Tool Executor

```python
import time
import uuid
from typing import Any, Callable, List, Optional


class DLQBackedToolExecutor:
    """
    Wraps tool execution with retry logic. On retry exhaustion,
    captures the failed call into the DLQ rather than discarding it.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ):
        self._dlq = dlq_store
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds
        self._dlq_enqueue_count = 0

    async def execute(
        self,
        tool_name: str,
        args: dict,
        fn: Callable,
        conversation_id: str = "",
        metadata: dict = None,
    ) -> Any:
        import asyncio
        failure_records: List[FailureRecord] = []

        for attempt in range(self._max_retries + 1):
            start = time.time()
            try:
                return await fn(**args)
            except Exception as exc:
                latency_ms = round((time.time() - start) * 1000, 2)
                failure_records.append(FailureRecord(
                    attempt_number=attempt + 1,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    failed_at=time.time(),
                    latency_ms=latency_ms,
                ))
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2 ** attempt))

        # All retries exhausted — enqueue to DLQ
        entry = DeadLetterEntry(
            entry_id=str(uuid.uuid4()),
            tool_name=tool_name,
            args=args,
            conversation_id=conversation_id,
            failure_records=failure_records,
            metadata=metadata or {},
        )
        self._dlq.enqueue(entry)
        self._dlq_enqueue_count += 1
        raise DeadLetterEnqueuedError(tool_name, entry.entry_id)

    def stats(self) -> dict:
        return {
            "dlq_enqueue_count": self._dlq_enqueue_count,
            "dlq_pending": self._dlq.size()["pending"],
        }


class DeadLetterEnqueuedError(Exception):
    def __init__(self, tool_name: str, entry_id: str):
        super().__init__(f"tool '{tool_name}' permanently failed, enqueued as DLQ entry {entry_id}")
        self.tool_name = tool_name
        self.entry_id = entry_id
```

## Solution 4: DLQ Reprocessor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class DLQReprocessor:
    """
    Periodically retries pending DLQ entries using the current tool dispatch function.
    Marks entries resolved on success; increments reprocess_attempts on failure.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        max_reprocess_attempts: int = 5,
        reprocess_interval_seconds: float = 300.0,
    ):
        self._dlq = dlq_store
        self._max_attempts = max_reprocess_attempts
        self._interval = reprocess_interval_seconds
        self._running = False
        self._reprocess_success = 0
        self._reprocess_failure = 0

    async def reprocess_once(
        self,
        dispatch_fn: Callable[[str, dict], Any],
        tool_filter: Optional[str] = None,
    ) -> dict:
        pending = self._dlq.pending(tool_name=tool_filter)
        eligible = [e for e in pending if e.reprocess_attempts < self._max_attempts]
        results = {"attempted": len(eligible), "succeeded": 0, "failed": 0}

        for entry in eligible:
            entry.reprocess_attempts += 1
            entry.last_reprocess_at = time.time()
            try:
                await dispatch_fn(entry.tool_name, entry.args)
                self._dlq.resolve(entry.entry_id, note="reprocessed successfully")
                self._reprocess_success += 1
                results["succeeded"] += 1
            except Exception:
                self._reprocess_failure += 1
                results["failed"] += 1

        return results

    async def run_loop(self, dispatch_fn: Callable[[str, dict], Any]) -> None:
        self._running = True
        while self._running:
            await self.reprocess_once(dispatch_fn)
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "reprocess_success": self._reprocess_success,
            "reprocess_failure": self._reprocess_failure,
        }
```

## Solution 5: DLQ Accumulation Alerter

```python
import time
from typing import List


class DLQAccumulationAlerter:
    """
    Alerts when DLQ pending count exceeds thresholds or when
    a single tool contributes disproportionately to failures.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        absolute_threshold: int = 100,
        per_tool_threshold: int = 20,
        age_alert_seconds: float = 3600.0,
    ):
        self._dlq = dlq_store
        self._abs_threshold = absolute_threshold
        self._tool_threshold = per_tool_threshold
        self._age_alert = age_alert_seconds

    def check(self) -> dict:
        pending = self._dlq.pending()
        by_tool: dict = {}
        old_entries = []

        for entry in pending:
            by_tool[entry.tool_name] = by_tool.get(entry.tool_name, 0) + 1
            if entry.age_seconds > self._age_alert:
                old_entries.append(entry.entry_id)

        alerts = []
        if len(pending) >= self._abs_threshold:
            alerts.append(f"DLQ size {len(pending)} exceeds threshold {self._abs_threshold}")

        for tool, count in by_tool.items():
            if count >= self._tool_threshold:
                alerts.append(f"tool '{tool}' has {count} DLQ entries (threshold {self._tool_threshold})")

        if old_entries:
            alerts.append(f"{len(old_entries)} DLQ entries older than {self._age_alert:.0f}s")

        return {
            "generated_at": time.time(),
            "pending_total": len(pending),
            "by_tool": by_tool,
            "old_entry_count": len(old_entries),
            "alerts": alerts,
            "alert": len(alerts) > 0,
        }
```

## Solution 6: DLQ Dashboard

```python
import time


class DeadLetterQueueDashboard:
    """
    Combines DLQ store stats, accumulation alerts, and reprocessor
    stats into a single operational view.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        executor: DLQBackedToolExecutor,
        reprocessor: DLQReprocessor,
        alerter: DLQAccumulationAlerter,
    ):
        self._dlq = dlq_store
        self._executor = executor
        self._reprocessor = reprocessor
        self._alerter = alerter

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "dlq_size": self._dlq.size(),
            "executor_stats": self._executor.stats(),
            "reprocessor_stats": self._reprocessor.stats(),
            "accumulation_check": self._alerter.check(),
        }
```

## Comparison

| Approach | Persistent Storage | Retry Logic | Reprocessing | Accumulation Alert | Dashboard |
|---|---|---|---|---|---|
| DeadLetterQueueStore | Yes (JSON-lines) | No | No | No | No |
| DLQBackedToolExecutor | Via store | Yes | No | No | No |
| DLQReprocessor | Via store | No | Yes | No | No |
| DLQAccumulationAlerter | Via store | No | No | Yes | No |
| DeadLetterQueueDashboard | No | No | No | No | Yes |

**Best for production**: Use Redis or a database as the DLQ backend in multi-instance deployments — a file-based DLQ on one instance is invisible to other instances. Set `max_reprocess_attempts=5` with exponential backoff in `DLQReprocessor` so that permanently broken payloads do not accumulate reprocess attempts indefinitely. Alert on `old_entry_count > 0` after the `age_alert_seconds` threshold — entries that have not been reprocessed within an hour indicate that automatic reprocessing is failing and manual intervention is needed. Include the full `args` in each `DeadLetterEntry` so that manual reprocessing does not require reconstructing the call from logs.
