---
title: "Agent Doesn't Implement Dead Letter Queue for Failed Tool Calls"
description: "Agents that discard failed tool calls after exhausting retries lose the information needed to diagnose systematic failures, replay missed operations after a fix, and meet audit requirements for incomplete actions. Implement a dead letter queue that captures persistently failed tool calls with full context, supports replay after fixes, and surfaces failure patterns for operational review."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-dead-letter-queue-for-failed-tool-calls
tags: [dead-letter-queue, failed-tool-calls, replay, fault-tolerance, error-recovery, dlq]
symptoms:
  - "Failed tool calls are silently discarded after retry exhaustion with no record"
  - "Cannot replay a batch of failed operations after fixing the underlying issue"
  - "No visibility into which tool calls are failing persistently vs. transiently"
  - "Audit logs show tool call attempts but not the full argument payload of failures"
  - "Systematic tool failures go undetected until a user complaint surfaces them"
---

## Why This Happens

Retry logic in most agents terminates with an exception that propagates to the caller and is either logged at the response layer or silently dropped. The failed call's full context — arguments, session ID, error history, attempt count — is never persisted. This means systematic failures (a broken API endpoint, a schema change in a downstream service) cannot be detected by pattern analysis, and individual failed calls cannot be replayed once the underlying issue is fixed. A dead letter queue captures the full failure context at the point of final retry exhaustion and stores it durably for replay and analysis.

## Solution 1: Dead Letter Entry

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallAttempt:
    attempt_number: int
    started_at: float
    error_type: str
    error_message: str
    duration_ms: float


@dataclass
class DeadLetterEntry:
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    original_call_id: str = ""
    failed_at: float = field(default_factory=time.time)
    attempts: List[ToolCallAttempt] = field(default_factory=list)
    final_error: str = ""
    replayed: bool = False
    replay_succeeded: Optional[bool] = None
    replayed_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def total_attempts(self) -> int:
        return len(self.attempts)

    def first_error_type(self) -> str:
        return self.attempts[0].error_type if self.attempts else ""
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
    Persists dead letter entries to a local JSONL file.
    Supports querying by tool name, time window, and replay status.
    """

    def __init__(self, path: str = "/tmp/agent_dlq.jsonl"):
        self._path = Path(path)
        self._lock = threading.Lock()

    def enqueue(self, entry: DeadLetterEntry) -> None:
        record = {
            "entry_id": entry.entry_id,
            "tool_name": entry.tool_name,
            "arguments": entry.arguments,
            "session_id": entry.session_id,
            "original_call_id": entry.original_call_id,
            "failed_at": entry.failed_at,
            "final_error": entry.final_error,
            "total_attempts": entry.total_attempts(),
            "first_error_type": entry.first_error_type(),
            "attempts": [
                {
                    "attempt_number": a.attempt_number,
                    "error_type": a.error_type,
                    "error_message": a.error_message,
                    "duration_ms": a.duration_ms,
                }
                for a in entry.attempts
            ],
            "replayed": entry.replayed,
            "replay_succeeded": entry.replay_succeeded,
            "metadata": entry.metadata,
        }
        with self._lock:
            with open(self._path, "a") as f:
                f.write(json.dumps(record) + "\n")

    def load_pending(
        self,
        tool_name: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> List[dict]:
        if not self._path.exists():
            return []
        entries = []
        with self._lock:
            with open(self._path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("replayed"):
                        continue
                    if tool_name and entry.get("tool_name") != tool_name:
                        continue
                    if since and entry.get("failed_at", 0) < since:
                        continue
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
        return entries

    def mark_replayed(self, entry_id: str, succeeded: bool) -> None:
        # In production: use a database with update support
        # For file-based store: append a replay record
        record = {
            "entry_id": entry_id,
            "replayed": True,
            "replay_succeeded": succeeded,
            "replayed_at": time.time(),
        }
        with self._lock:
            with open(self._path, "a") as f:
                f.write(json.dumps({"_replay_update": record}) + "\n")
```

## Solution 3: DLQ-Backed Tool Call Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class DLQBackedToolCallExecutor:
    """
    Executes tool calls with retry logic and routes persistently
    failed calls to the dead letter queue store.
    """

    def __init__(
        self,
        dlq_store: DeadLetterQueueStore,
        max_retries: int = 3,
        base_delay_seconds: float = 1.0,
    ):
        self._dlq = dlq_store
        self._max_retries = max_retries
        self._base_delay = base_delay_seconds
        self._dlq_count = 0

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        execute_fn: Callable,
        session_id: str = "",
        call_id: str = "",
    ) -> Any:
        attempts: List[ToolCallAttempt] = []
        last_error = ""

        for attempt_n in range(1, self._max_retries + 2):
            start = time.time()
            try:
                result = await execute_fn(tool_name, arguments)
                return result
            except Exception as exc:
                duration_ms = round((time.time() - start) * 1000, 2)
                last_error = str(exc)
                attempts.append(ToolCallAttempt(
                    attempt_number=attempt_n,
                    started_at=start,
                    error_type=type(exc).__name__,
                    error_message=last_error,
                    duration_ms=duration_ms,
                ))
                if attempt_n <= self._max_retries:
                    delay = self._base_delay * (2 ** (attempt_n - 1))
                    await asyncio.sleep(delay)

        # All retries exhausted — send to DLQ
        entry = DeadLetterEntry(
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
            original_call_id=call_id,
            attempts=attempts,
            final_error=last_error,
        )
        self._dlq.enqueue(entry)
        self._dlq_count += 1
        raise RuntimeError(
            f"Tool '{tool_name}' failed after {len(attempts)} attempts and was sent to DLQ. "
            f"Entry ID: {entry.entry_id}"
        )

    def dlq_count(self) -> int:
        return self._dlq_count
```

## Solution 4: DLQ Replay Manager

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class DLQReplayManager:
    """
    Replays pending dead letter entries after a fix has been deployed.
    Records replay outcomes and marks entries as resolved.
    """

    def __init__(
        self,
        store: DeadLetterQueueStore,
        execute_fn: Callable,
    ):
        self._store = store
        self._execute_fn = execute_fn

    async def replay_all(
        self,
        tool_name: str = None,
        since: float = None,
        limit: int = 50,
    ) -> dict:
        pending = self._store.load_pending(
            tool_name=tool_name,
            since=since,
            limit=limit,
        )

        results = {"replayed": 0, "succeeded": 0, "failed": 0, "entries": []}

        for entry_data in pending:
            tool = entry_data["tool_name"]
            args = entry_data["arguments"]
            entry_id = entry_data["entry_id"]

            try:
                await self._execute_fn(tool, args)
                succeeded = True
            except Exception:
                succeeded = False

            self._store.mark_replayed(entry_id, succeeded)
            results["replayed"] += 1
            if succeeded:
                results["succeeded"] += 1
            else:
                results["failed"] += 1
            results["entries"].append({
                "entry_id": entry_id,
                "tool_name": tool,
                "succeeded": succeeded,
            })

        return results
```

## Solution 5: DLQ Pattern Analyzer

```python
import time
from collections import Counter
from typing import List, Optional


class DLQPatternAnalyzer:
    """
    Analyzes dead letter entries to surface systematic failure patterns:
    which tools fail most often, which error types dominate, and
    whether failure rate is increasing over time.
    """

    def analyze(
        self,
        entries: List[dict],
        window_seconds: float = 86400.0,
    ) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in entries if e.get("failed_at", 0) >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "failures": 0}

        tool_counts: Counter = Counter(e["tool_name"] for e in recent)
        error_counts: Counter = Counter(
            e.get("first_error_type", "Unknown") for e in recent
        )
        avg_attempts = sum(e.get("total_attempts", 1) for e in recent) / len(recent)

        return {
            "window_seconds": window_seconds,
            "failures": len(recent),
            "pending_replay": sum(1 for e in recent if not e.get("replayed")),
            "top_failing_tools": dict(tool_counts.most_common(5)),
            "top_error_types": dict(error_counts.most_common(5)),
            "avg_attempts_before_dlq": round(avg_attempts, 2),
        }
```

## Solution 6: DLQ Operations Dashboard

```python
import time


class DLQOperationsDashboard:
    """
    Combines DLQ depth, pattern analysis, and replay statistics
    into a single operational view for on-call engineers.
    """

    def __init__(
        self,
        store: DeadLetterQueueStore,
        executor: DLQBackedToolCallExecutor,
        analyzer: DLQPatternAnalyzer,
    ):
        self._store = store
        self._executor = executor
        self._analyzer = analyzer

    def render(self) -> dict:
        pending = self._store.load_pending(limit=5000)
        analysis = self._analyzer.analyze(pending)
        return {
            "generated_at": time.time(),
            "total_dlq_entries_sent": self._executor.dlq_count(),
            "pending_replay_count": analysis.get("pending_replay", 0),
            "pattern_analysis": analysis,
        }
```

## Comparison

| Approach | Failure Capture | Persistent Storage | Replay Support | Pattern Analysis | Dashboard |
|---|---|---|---|---|---|
| DeadLetterQueueStore | No | Yes (JSONL) | Via mark_replayed | No | No |
| DLQBackedToolCallExecutor | Yes (on retry exhaust) | Via store | No | No | No |
| DLQReplayManager | No | No | Yes (batch replay) | No | No |
| DLQPatternAnalyzer | No | No | No | Yes (tool+error) | No |
| DLQOperationsDashboard | No | No | No | Via analyzer | Yes |

**Best for production**: Use a Redis list or database table as the DLQ backend in multi-instance deployments — the file-based store is single-instance only. Store the full `arguments` dict in the DLQ entry so replay is possible without any additional context retrieval. Alert when `pending_replay_count` exceeds 20 — this indicates a systematic failure affecting multiple sessions and should be escalated to the on-call engineer immediately. Run `DLQReplayManager.replay_all()` after every fix deployment that addresses a known tool failure: this recovers missed operations and verifies that the fix actually works under production conditions before the incident is closed.
