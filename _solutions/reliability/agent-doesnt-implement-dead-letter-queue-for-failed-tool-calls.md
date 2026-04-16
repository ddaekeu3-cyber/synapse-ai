---
title: "Agent Doesn't Implement Dead Letter Queue for Failed Tool Calls"
description: "Agents that discard tool call failures after exhausting retries lose the work permanently — there is no record of what failed, no way to replay it after the underlying issue is fixed, and no operator visibility into which tasks were silently dropped. Implement a dead letter queue that captures failed tool calls with their full context, supports manual or automatic replay after recovery, and surfaces failure patterns for root cause analysis."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dead-letter-queue-for-failed-tool-calls
tags: [dead-letter-queue, failed-tool-calls, retry, replay, fault-tolerance, error-recovery]
symptoms:
  - "Failed tool calls after max retries are silently discarded — no record of what was lost"
  - "After fixing a downstream outage, there is no way to replay the work that failed during it"
  - "No operator visibility into which tool calls are failing and why"
  - "Tasks that fail during an incident cannot be recovered without full re-execution by the user"
  - "Failure count metrics exist but the actual failed payloads are not preserved"
---

## Why This Happens

Retry logic handles transient failures but has a finite limit. When retries are exhausted, most agents raise an exception that propagates up and either aborts the task or is swallowed by a broad exception handler. The failed tool call and its arguments are lost. A dead letter queue (DLQ) intercepts at this boundary: instead of discarding the call, it serializes the full invocation context — tool name, arguments, error history, session metadata — and stores it durably for later inspection and replay. This converts silent data loss into a recoverable failure.

## Solution 1: Dead Letter Entry

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FailureRecord:
    attempt_number: int
    error_type: str
    error_message: str
    failed_at: float = field(default_factory=time.time)


@dataclass
class DeadLetterEntry:
    entry_id: str
    tool_name: str
    args: Dict[str, Any]
    session_id: str
    task_id: str
    failure_history: List[FailureRecord]
    enqueued_at: float = field(default_factory=time.time)
    replay_count: int = 0
    last_replayed_at: Optional[float] = None
    resolved: bool = False
    resolution_note: str = ""

    @classmethod
    def create(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        failure_history: List[FailureRecord],
        session_id: str = "",
        task_id: str = "",
    ) -> "DeadLetterEntry":
        return cls(
            entry_id=uuid.uuid4().hex,
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            task_id=task_id,
            failure_history=failure_history,
        )

    @property
    def final_error(self) -> Optional[str]:
        if self.failure_history:
            return self.failure_history[-1].error_message
        return None
```

## Solution 2: Dead Letter Queue Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class DeadLetterQueueStore:
    """
    Persists dead letter entries to a local JSON file.
    Replace with Redis or a database for multi-instance deployments.
    """

    def __init__(self, path: str = "/tmp/agent_dlq.json", max_entries: int = 5000):
        self._path = Path(path)
        self._max = max_entries
        self._lock = Lock()

    def enqueue(self, entry: DeadLetterEntry) -> None:
        with self._lock:
            entries = self._load()
            if len(entries) >= self._max:
                # Drop oldest resolved entries first, then oldest unresolved
                resolved = [e for e in entries.values() if e["resolved"]]
                if resolved:
                    oldest_resolved = min(resolved, key=lambda e: e["enqueued_at"])
                    del entries[oldest_resolved["entry_id"]]
                else:
                    oldest = min(entries.values(), key=lambda e: e["enqueued_at"])
                    del entries[oldest["entry_id"]]
            entries[entry.entry_id] = self._serialize(entry)
            self._save(entries)

    def get(self, entry_id: str) -> Optional[DeadLetterEntry]:
        with self._lock:
            entries = self._load()
            data = entries.get(entry_id)
            return self._deserialize(data) if data else None

    def list_unresolved(self, tool_name: Optional[str] = None) -> List[DeadLetterEntry]:
        with self._lock:
            entries = self._load()
            result = [
                self._deserialize(e) for e in entries.values()
                if not e["resolved"] and (tool_name is None or e["tool_name"] == tool_name)
            ]
            return sorted(result, key=lambda e: e.enqueued_at)

    def mark_resolved(self, entry_id: str, note: str = "") -> bool:
        with self._lock:
            entries = self._load()
            if entry_id not in entries:
                return False
            entries[entry_id]["resolved"] = True
            entries[entry_id]["resolution_note"] = note
            self._save(entries)
            return True

    def record_replay(self, entry_id: str) -> bool:
        with self._lock:
            entries = self._load()
            if entry_id not in entries:
                return False
            entries[entry_id]["replay_count"] += 1
            entries[entry_id]["last_replayed_at"] = time.time()
            self._save(entries)
            return True

    def _load(self) -> Dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, entries: dict) -> None:
        self._path.write_text(json.dumps(entries, indent=2))

    @staticmethod
    def _serialize(entry: DeadLetterEntry) -> dict:
        return {
            "entry_id": entry.entry_id,
            "tool_name": entry.tool_name,
            "args": entry.args,
            "session_id": entry.session_id,
            "task_id": entry.task_id,
            "failure_history": [
                {"attempt_number": f.attempt_number, "error_type": f.error_type,
                 "error_message": f.error_message, "failed_at": f.failed_at}
                for f in entry.failure_history
            ],
            "enqueued_at": entry.enqueued_at,
            "replay_count": entry.replay_count,
            "last_replayed_at": entry.last_replayed_at,
            "resolved": entry.resolved,
            "resolution_note": entry.resolution_note,
        }

    @staticmethod
    def _deserialize(data: dict) -> DeadLetterEntry:
        return DeadLetterEntry(
            entry_id=data["entry_id"],
            tool_name=data["tool_name"],
            args=data["args"],
            session_id=data.get("session_id", ""),
            task_id=data.get("task_id", ""),
            failure_history=[
                FailureRecord(
                    attempt_number=f["attempt_number"],
                    error_type=f["error_type"],
                    error_message=f["error_message"],
                    failed_at=f["failed_at"],
                )
                for f in data.get("failure_history", [])
            ],
            enqueued_at=data["enqueued_at"],
            replay_count=data.get("replay_count", 0),
            last_replayed_at=data.get("last_replayed_at"),
            resolved=data.get("resolved", False),
            resolution_note=data.get("resolution_note", ""),
        )
```

## Solution 3: DLQ-Backed Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, List, Optional


class DLQBackedToolExecutor:
    """
    Executes tool calls with retry. On exhaustion, enqueues the failed
    call to the dead letter queue rather than discarding it.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        session_id: str = "",
        task_id: str = "",
    ):
        self._dlq = dlq_store
        self._max = max_attempts
        self._base_delay = base_delay_seconds
        self._session_id = session_id
        self._task_id = task_id

    async def execute(
        self,
        tool_fn: Callable,
        tool_name: str,
        args: dict,
    ) -> Any:
        history: List[FailureRecord] = []
        delay = self._base_delay

        for attempt in range(1, self._max + 1):
            try:
                return await tool_fn(**args)
            except Exception as exc:
                history.append(FailureRecord(
                    attempt_number=attempt,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ))
                if attempt < self._max:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)

        # All retries exhausted — enqueue to DLQ
        entry = DeadLetterEntry.create(
            tool_name=tool_name,
            args=args,
            failure_history=history,
            session_id=self._session_id,
            task_id=self._task_id,
        )
        self._dlq.enqueue(entry)
        raise DeadLetterQueuedError(tool_name=tool_name, entry_id=entry.entry_id)


class DeadLetterQueuedError(Exception):
    def __init__(self, tool_name: str, entry_id: str):
        super().__init__(
            f"Tool '{tool_name}' failed after all retries. "
            f"Dead letter entry: {entry_id}"
        )
        self.tool_name = tool_name
        self.entry_id = entry_id
```

## Solution 4: DLQ Replay Engine

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class DLQReplayEngine:
    """
    Replays dead letter entries by re-executing the original tool call.
    Marks entries resolved on success or re-enqueues on failure.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        tool_registry: Dict[str, Callable],
    ):
        self._dlq = dlq_store
        self._tools = tool_registry

    async def replay(self, entry_id: str) -> dict:
        entry = self._dlq.get(entry_id)
        if not entry:
            return {"status": "not_found", "entry_id": entry_id}

        if entry.resolved:
            return {"status": "already_resolved", "entry_id": entry_id}

        tool_fn = self._tools.get(entry.tool_name)
        if not tool_fn:
            return {"status": "tool_not_found", "tool_name": entry.tool_name}

        self._dlq.record_replay(entry_id)
        try:
            result = await tool_fn(**entry.args)
            self._dlq.mark_resolved(entry_id, note="replayed successfully")
            return {"status": "success", "entry_id": entry_id, "result": result}
        except Exception as exc:
            return {"status": "failed", "entry_id": entry_id, "error": str(exc)}

    async def replay_all(self, tool_name: Optional[str] = None) -> List[dict]:
        entries = self._dlq.list_unresolved(tool_name=tool_name)
        results = []
        for entry in entries:
            result = await self.replay(entry.entry_id)
            results.append(result)
        return results
```

## Solution 5: DLQ Failure Pattern Analyzer

```python
from typing import Dict, List, Optional


class DLQFailurePatternAnalyzer:
    """
    Analyzes dead letter queue contents to surface which tools
    fail most often, which error types dominate, and whether
    failures cluster in time (suggesting an outage vs. recurring bug).
    """

    def __init__(self, dlq_store: DeadLetterQueueStore):
        self._dlq = dlq_store

    def analyze(self) -> dict:
        entries = self._dlq.list_unresolved()
        if not entries:
            return {"unresolved_entries": 0}

        by_tool: Dict[str, int] = {}
        by_error: Dict[str, int] = {}

        for entry in entries:
            by_tool[entry.tool_name] = by_tool.get(entry.tool_name, 0) + 1
            if entry.final_error:
                err_key = entry.failure_history[-1].error_type
                by_error[err_key] = by_error.get(err_key, 0) + 1

        enqueue_times = sorted(e.enqueued_at for e in entries)
        time_cluster = None
        if len(enqueue_times) >= 3:
            span = enqueue_times[-1] - enqueue_times[0]
            time_cluster = round(span / 60, 1)  # minutes

        return {
            "unresolved_entries": len(entries),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: x[1], reverse=True)),
            "by_error_type": dict(sorted(by_error.items(), key=lambda x: x[1], reverse=True)),
            "failure_time_span_minutes": time_cluster,
            "oldest_entry_age_minutes": round(
                (entries[0].enqueued_at and (__import__("time").time() - entries[0].enqueued_at) / 60), 1
            ) if entries else None,
        }
```

## Solution 6: Dead Letter Queue Dashboard

```python
import time


class DeadLetterQueueDashboard:
    """
    Combines queue depth, failure pattern analysis, and replay
    statistics into a single operational view for on-call engineers.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        analyzer: DLQFailurePatternAnalyzer,
    ):
        self._dlq = dlq_store
        self._analyzer = analyzer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "queue_depth": len(self._dlq.list_unresolved()),
            "failure_patterns": self._analyzer.analyze(),
        }
```

## Comparison

| Approach | Failure Persistence | Retry Integration | Replay Support | Pattern Analysis | Dashboard |
|---|---|---|---|---|---|
| DeadLetterQueueStore | Yes (file/DB) | No | Via mark_resolved | No | No |
| DLQBackedToolExecutor | Via store | Yes | No | No | No |
| DLQReplayEngine | No | No | Yes (single + bulk) | No | No |
| DLQFailurePatternAnalyzer | No | No | No | Yes | No |
| DeadLetterQueueDashboard | No | No | No | No | Yes |

**Best for production**: Persist DLQ entries to Redis or a database rather than a local file — local files are lost on container restart, defeating the purpose of the DLQ. Emit a `dead_letter_enqueued` structured log event every time `DLQBackedToolExecutor` enqueues an entry: this feeds dashboards and on-call alerts without requiring engineers to poll the DLQ. Run `DLQReplayEngine.replay_all()` automatically 30 minutes after an incident is resolved — by that point the downstream service has recovered and queued work can be replayed without human intervention. Monitor `failure_time_span_minutes` in `DLQFailurePatternAnalyzer`: a tight cluster (all failures within 5 minutes) indicates an acute outage, while spread-out failures indicate a persistent bug that replay will not fix.
