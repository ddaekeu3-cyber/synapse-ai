---
title: "Agent Doesn't Implement Idempotency Keys for Tool Calls"
description: "Agents that retry failed tool calls without idempotency keys risk executing side-effectful operations multiple times — a payment is charged twice, an email is sent twice, a database row is inserted twice. Implement idempotency keys that uniquely identify each intended tool operation, allow safe retries by detecting duplicate execution attempts, and enable audit trails that distinguish original calls from retries."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-idempotency-keys-for-tool-calls
tags: [idempotency, idempotency-keys, safe-retry, duplicate-prevention, side-effects, at-most-once]
symptoms:
  - "Payment tool called twice on retry — customer charged twice for the same order"
  - "Email notification tool retried after timeout — user receives duplicate emails"
  - "No mechanism to distinguish 'retry of previous call' from 'new intended call'"
  - "Database insert tool creates duplicate rows when retried after a network error"
  - "Audit log shows two executions of the same operation with no indication one was a retry"
---

## Why This Happens

Retry logic is added to handle transient failures, but most retry implementations treat each attempt as a fresh, independent call. For idempotent operations (reads, queries), this is safe. For non-idempotent operations (writes, payments, notifications), retrying without coordination creates duplicate side effects. An idempotency key — a stable, unique identifier for a specific intended operation — lets the receiving system detect "I already processed this exact request" and return the original result instead of executing again.

## Solution 1: Idempotency Key Generator

```python
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class IdempotencyKey:
    key: str
    tool_name: str
    session_id: str
    generated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class IdempotencyKeyGenerator:
    """
    Generates stable idempotency keys for tool calls.
    Content-based keys: same session + tool + args = same key (deterministic retry detection).
    Time-based keys: session + tool + timestamp = unique per invocation (intentional re-execution).
    """

    @staticmethod
    def content_based(
        session_id: str,
        tool_name: str,
        args: Dict[str, Any],
        turn_number: int,
    ) -> IdempotencyKey:
        """
        Deterministic key: two calls with the same session, tool, args, and turn
        get the same key. Safe for retry detection.
        """
        import json
        payload = json.dumps({
            "session": session_id,
            "tool": tool_name,
            "args": args,
            "turn": turn_number,
        }, sort_keys=True)
        key = hashlib.sha256(payload.encode()).hexdigest()[:32]
        return IdempotencyKey(
            key=key,
            tool_name=tool_name,
            session_id=session_id,
            expires_at=time.time() + 86400,  # 24h expiry
        )

    @staticmethod
    def time_based(
        session_id: str,
        tool_name: str,
    ) -> IdempotencyKey:
        """
        UUID-based key: always unique, even for identical calls.
        Use when the same operation should be permitted multiple times.
        """
        key = f"{session_id}:{tool_name}:{uuid.uuid4().hex}"
        return IdempotencyKey(
            key=key,
            tool_name=tool_name,
            session_id=session_id,
            expires_at=time.time() + 3600,
        )
```

## Solution 2: Idempotency Record Store

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class IdempotencyRecordState(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IdempotencyRecord:
    key: str
    tool_name: str
    session_id: str
    state: IdempotencyRecordState
    result: Optional[Any] = None
    error: Optional[str] = None
    attempt_count: int = 1
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    expires_at: Optional[float] = None

    def is_terminal(self) -> bool:
        return self.state in (
            IdempotencyRecordState.COMPLETED,
            IdempotencyRecordState.FAILED,
        )


class IdempotencyRecordStore:
    """
    Persists idempotency records to detect duplicate tool call attempts.
    In production, back this with Redis or a database for cross-process deduplication.
    """

    def __init__(self) -> None:
        self._records: Dict[str, IdempotencyRecord] = {}

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            k for k, r in self._records.items()
            if r.expires_at and now > r.expires_at
        ]
        for k in expired:
            del self._records[k]

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        self._evict_expired()
        rec = self._records.get(key)
        if rec and rec.expires_at and time.time() > rec.expires_at:
            del self._records[key]
            return None
        return rec

    def create_in_progress(self, idem_key: IdempotencyKey) -> IdempotencyRecord:
        """Create a new in-progress record. Raises if key already exists."""
        if key := self.get(idem_key.key):
            raise ValueError(
                f"Idempotency key '{idem_key.key}' already exists with state '{key.state}'"
            )
        rec = IdempotencyRecord(
            key=idem_key.key,
            tool_name=idem_key.tool_name,
            session_id=idem_key.session_id,
            state=IdempotencyRecordState.IN_PROGRESS,
            expires_at=idem_key.expires_at,
        )
        self._records[idem_key.key] = rec
        return rec

    def complete(self, key: str, result: Any) -> None:
        rec = self._records.get(key)
        if rec:
            rec.state = IdempotencyRecordState.COMPLETED
            rec.result = result
            rec.completed_at = time.time()

    def fail(self, key: str, error: str) -> None:
        rec = self._records.get(key)
        if rec:
            rec.state = IdempotencyRecordState.FAILED
            rec.error = error
            rec.completed_at = time.time()

    def record_count(self) -> int:
        return len(self._records)
```

## Solution 3: Idempotent Tool Executor

```python
import asyncio
from typing import Any, Callable, Dict


class DuplicateToolCallError(RuntimeError):
    def __init__(self, key: str, existing_state: IdempotencyRecordState) -> None:
        super().__init__(f"Duplicate tool call detected (key={key}, state={existing_state.value})")
        self.idempotency_key = key
        self.existing_state = existing_state


class IdempotentToolExecutor:
    """
    Wraps tool calls with idempotency enforcement.
    - First call: executes tool and stores result.
    - Retry of same call: returns stored result without re-executing.
    - Concurrent duplicate: waits for first call to complete, returns its result.
    """

    def __init__(
        self,
        store: IdempotencyRecordStore,
        wait_for_in_progress_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._wait = wait_for_in_progress_seconds
        self._in_progress_events: Dict[str, asyncio.Event] = {}

    async def execute(
        self,
        idem_key: IdempotencyKey,
        tool_fn: Callable,
        args: Dict[str, Any],
    ) -> Any:
        existing = self._store.get(idem_key.key)

        if existing:
            if existing.state == IdempotencyRecordState.COMPLETED:
                return existing.result   # idempotent return
            if existing.state == IdempotencyRecordState.FAILED:
                raise RuntimeError(f"Previous attempt failed: {existing.error}")
            if existing.state == IdempotencyRecordState.IN_PROGRESS:
                # Wait for the concurrent in-progress call to finish
                event = self._in_progress_events.get(idem_key.key)
                if event:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=self._wait)
                    except asyncio.TimeoutError:
                        raise RuntimeError(
                            f"Timed out waiting for in-progress call '{idem_key.key}'"
                        )
                    return await self.execute(idem_key, tool_fn, args)

        # First attempt — register in-progress
        event = asyncio.Event()
        self._in_progress_events[idem_key.key] = event

        try:
            rec = self._store.create_in_progress(idem_key)
        except ValueError:
            # Another coroutine just created the record — retry
            del self._in_progress_events[idem_key.key]
            return await self.execute(idem_key, tool_fn, args)

        try:
            result = await tool_fn(**args)
            self._store.complete(idem_key.key, result)
            return result
        except Exception as exc:
            self._store.fail(idem_key.key, str(exc)[:300])
            raise
        finally:
            event.set()
            self._in_progress_events.pop(idem_key.key, None)
```

## Solution 4: Non-Idempotent Tool Registry

```python
from typing import FrozenSet, Set


class NonIdempotentToolRegistry:
    """
    Maintains the set of tools that require idempotency key enforcement.
    Tools marked as non-idempotent must be called through IdempotentToolExecutor.
    """

    DEFAULT_NON_IDEMPOTENT: FrozenSet[str] = frozenset({
        "send_email",
        "send_sms",
        "charge_payment",
        "create_record",
        "insert_row",
        "post_message",
        "webhook_trigger",
        "delete_record",
        "update_balance",
    })

    def __init__(self) -> None:
        self._non_idempotent: Set[str] = set(self.DEFAULT_NON_IDEMPOTENT)

    def register_non_idempotent(self, tool_name: str) -> None:
        self._non_idempotent.add(tool_name)

    def register_idempotent(self, tool_name: str) -> None:
        self._non_idempotent.discard(tool_name)

    def requires_idempotency_key(self, tool_name: str) -> bool:
        return tool_name in self._non_idempotent

    def all_non_idempotent(self) -> FrozenSet[str]:
        return frozenset(self._non_idempotent)
```

## Solution 5: Idempotency Audit Tracker

```python
import time
from collections import defaultdict
from typing import List


class IdempotencyAuditTracker:
    """
    Tracks idempotency outcomes — how many calls were deduplicated,
    how many were first-time executions, and duplicate detection rate.
    """

    def __init__(self, store: IdempotencyRecordStore) -> None:
        self._store = store
        self._dedup_count = 0
        self._first_exec_count = 0
        self._events: List[dict] = []

    def record_dedup(self, key: str, tool_name: str) -> None:
        self._dedup_count += 1
        self._events.append({
            "type": "deduplicated",
            "key": key,
            "tool": tool_name,
            "ts": time.time(),
        })

    def record_first_exec(self, key: str, tool_name: str) -> None:
        self._first_exec_count += 1
        self._events.append({
            "type": "first_execution",
            "key": key,
            "tool": tool_name,
            "ts": time.time(),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        dedup = sum(1 for e in recent if e["type"] == "deduplicated")
        first = sum(1 for e in recent if e["type"] == "first_execution")
        by_tool: dict = defaultdict(int)
        for e in recent:
            if e["type"] == "deduplicated":
                by_tool[e["tool"]] += 1

        return {
            "window_seconds": window_seconds,
            "first_executions": first,
            "deduplicated_calls": dedup,
            "dedup_rate": round(dedup / max(first + dedup, 1), 4),
            "most_deduped_tools": dict(sorted(by_tool.items(), key=lambda x: -x[1])[:5]),
            "active_records": self._store.record_count(),
        }
```

## Solution 6: Idempotency Dashboard

```python
import time


class IdempotencyDashboard:
    """
    Combines store stats, audit tracker, and registry state
    into a single reliability operational view.
    """

    def __init__(
        self,
        store: IdempotencyRecordStore,
        tracker: IdempotencyAuditTracker,
        registry: NonIdempotentToolRegistry,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._registry = registry

    def render(self) -> dict:
        summary = self._tracker.summary()
        return {
            "generated_at": time.time(),
            "idempotency_summary": summary,
            "non_idempotent_tools": sorted(self._registry.all_non_idempotent()),
            "active_records": self._store.record_count(),
        }
```

## Comparison

| Approach | Key Generation | Record Store | Duplicate Detection | Concurrent Safety | Audit Trail |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (content + time) | No | No | No | No |
| IdempotencyRecordStore | No | Yes | Yes | No | No |
| IdempotentToolExecutor | No | Via store | Yes | Yes (asyncio.Event) | No |
| NonIdempotentToolRegistry | No | No | No | No | No |
| IdempotencyAuditTracker | No | No | No | No | Yes |

**Best for production**: Use content-based keys for all retry scenarios — they guarantee that a retry of the same intended operation reuses the same key. Use time-based keys only when the same logical operation can legitimately occur twice in the same session (e.g., two separate "send email" instructions). Set key expiry to 24 hours — long enough to cover delayed retries but short enough that the store doesn't grow unbounded. For high-volume production deployments, replace the in-process `IdempotencyRecordStore` with Redis using `SET key value NX PX ttl_ms` — the `NX` flag provides atomic check-and-set that prevents race conditions across multiple agent workers.
