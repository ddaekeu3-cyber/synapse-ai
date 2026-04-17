---
title: "Agent Doesn't Implement Idempotency Keys for Tool Calls"
description: "Agents that retry failed tool calls without idempotency keys risk executing side-effectful operations multiple times: a payment is charged twice, an email is sent three times, a database record is inserted in duplicate. Implement idempotency key generation and server-side deduplication so that retried tool calls are guaranteed to produce the same observable effect as the first successful call."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-idempotency-keys-for-tool-calls
tags: [idempotency, tool-calls, retry-safety, deduplication, side-effects, at-most-once]
symptoms:
  - "Payment tool retried after timeout — customer charged twice"
  - "Email tool called three times due to network flakiness — user receives duplicate messages"
  - "Database insert tool retried without idempotency — duplicate records created"
  - "No way to determine whether a failed tool call was actually executed before the error"
  - "Tool retry logic exists but idempotency is delegated to each tool individually with no enforcement"
---

## Why This Happens

Tool call retries are necessary for reliability but dangerous for side-effectful operations. The problem is that a timeout or network error does not tell the caller whether the operation was executed — it only tells the caller that no response was received. Without an idempotency key, the only safe action is to not retry. With an idempotency key, the server can detect that a request has already been processed and return the original result instead of executing again. Idempotency requires three components: a stable key derived from the operation's semantic identity (not a random UUID), a result store that maps keys to outcomes, and a lookup step before execution that short-circuits on existing results.

## Solution 1: Idempotency Key Generator

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class IdempotencyKey:
    key: str
    tool_name: str
    args_fingerprint: str
    session_id: str
    sequence_number: int

    def __str__(self) -> str:
        return self.key


class IdempotencyKeyGenerator:
    """
    Generates stable idempotency keys from tool name, arguments,
    session context, and an explicit sequence number.
    The key is deterministic: the same inputs always produce the same key.
    """

    def generate(
        self,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str,
        sequence_number: int,
    ) -> IdempotencyKey:
        canonical_args = json.dumps(args, sort_keys=True, separators=(",", ":"))
        args_fingerprint = hashlib.sha256(canonical_args.encode()).hexdigest()[:16]
        raw = f"{tool_name}:{args_fingerprint}:{session_id}:{sequence_number}"
        key = hashlib.sha256(raw.encode()).hexdigest()
        return IdempotencyKey(
            key=key,
            tool_name=tool_name,
            args_fingerprint=args_fingerprint,
            session_id=session_id,
            sequence_number=sequence_number,
        )
```

## Solution 2: Idempotency Result Store

```python
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional


class IdempotencyStatus(str, Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IdempotencyRecord:
    key: str
    tool_name: str
    status: IdempotencyStatus
    created_at: float
    completed_at: Optional[float] = None
    result: Any = None
    error: Optional[str] = None
    attempt_count: int = 1


class IdempotencyResultStore:
    """
    Persists idempotency records keyed on the idempotency key.
    Supports atomic claim-or-fetch: either claims a key for a new
    execution or returns the existing record.
    """

    def __init__(
        self,
        path: str = "/tmp/idempotency_store.json",
        ttl_seconds: float = 86400.0,
    ):
        self._path = Path(path)
        self._ttl = ttl_seconds
        self._lock = Lock()

    def claim_or_fetch(self, key: str, tool_name: str) -> tuple:
        """
        Returns (is_new, record).
        If is_new=True, the caller should execute and then call complete() or fail().
        If is_new=False, the existing record is returned (may still be IN_FLIGHT).
        """
        with self._lock:
            store = self._load()
            self._evict_expired(store)
            if key in store:
                data = store[key]
                record = self._deserialize(data)
                return False, record
            record = IdempotencyRecord(
                key=key,
                tool_name=tool_name,
                status=IdempotencyStatus.IN_FLIGHT,
                created_at=time.time(),
            )
            store[key] = self._serialize(record)
            self._save(store)
            return True, record

    def complete(self, key: str, result: Any) -> None:
        with self._lock:
            store = self._load()
            if key not in store:
                return
            store[key]["status"] = IdempotencyStatus.COMPLETED.value
            store[key]["completed_at"] = time.time()
            store[key]["result"] = result
            self._save(store)

    def fail(self, key: str, error: str) -> None:
        with self._lock:
            store = self._load()
            if key not in store:
                return
            store[key]["status"] = IdempotencyStatus.FAILED.value
            store[key]["completed_at"] = time.time()
            store[key]["error"] = error
            self._save(store)

    def _evict_expired(self, store: dict) -> None:
        cutoff = time.time() - self._ttl
        expired = [k for k, v in store.items() if v.get("created_at", 0) < cutoff]
        for k in expired:
            del store[k]

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, store: dict) -> None:
        self._path.write_text(json.dumps(store, indent=2))

    @staticmethod
    def _serialize(record: IdempotencyRecord) -> dict:
        return {
            "key": record.key,
            "tool_name": record.tool_name,
            "status": record.status.value,
            "created_at": record.created_at,
            "completed_at": record.completed_at,
            "result": record.result,
            "error": record.error,
            "attempt_count": record.attempt_count,
        }

    @staticmethod
    def _deserialize(data: dict) -> IdempotencyRecord:
        return IdempotencyRecord(
            key=data["key"],
            tool_name=data["tool_name"],
            status=IdempotencyStatus(data["status"]),
            created_at=data["created_at"],
            completed_at=data.get("completed_at"),
            result=data.get("result"),
            error=data.get("error"),
            attempt_count=data.get("attempt_count", 1),
        )
```

## Solution 3: Idempotent Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict


class IdempotentToolExecutor:
    """
    Wraps tool execution with idempotency guarantees.
    Before executing, claims the idempotency key. If already claimed,
    waits for the in-flight execution to complete and returns its result.
    """

    IN_FLIGHT_POLL_INTERVAL = 0.5
    IN_FLIGHT_MAX_WAIT = 30.0

    def __init__(
        self,
        key_generator: IdempotencyKeyGenerator,
        store: IdempotencyResultStore,
    ):
        self._generator = key_generator
        self._store = store

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        session_id: str,
        sequence_number: int,
    ) -> Any:
        idem_key = self._generator.generate(
            tool_name, args, session_id, sequence_number
        )
        is_new, record = self._store.claim_or_fetch(idem_key.key, tool_name)

        if not is_new:
            return await self._await_or_return(idem_key.key, record)

        # New claim — execute the tool
        try:
            result = await tool_fn(**args)
            self._store.complete(idem_key.key, result)
            return result
        except Exception as exc:
            self._store.fail(idem_key.key, str(exc))
            raise

    async def _await_or_return(self, key: str, record: IdempotencyRecord) -> Any:
        if record.status == IdempotencyStatus.COMPLETED:
            return record.result
        if record.status == IdempotencyStatus.FAILED:
            raise RuntimeError(f"Idempotent call previously failed: {record.error}")

        # IN_FLIGHT — poll until complete
        deadline = time.time() + self.IN_FLIGHT_MAX_WAIT
        while time.time() < deadline:
            await asyncio.sleep(self.IN_FLIGHT_POLL_INTERVAL)
            _, refreshed = self._store.claim_or_fetch(key, record.tool_name)
            if refreshed.status == IdempotencyStatus.COMPLETED:
                return refreshed.result
            if refreshed.status == IdempotencyStatus.FAILED:
                raise RuntimeError(f"Idempotent call failed: {refreshed.error}")
        raise TimeoutError(f"In-flight idempotent call timed out: {key}")
```

## Solution 4: Sequence Number Tracker

```python
from threading import Lock
from typing import Dict


class SessionSequenceTracker:
    """
    Maintains a per-session, per-tool sequence counter so that
    idempotency keys are unique across distinct calls even when
    arguments are identical (e.g. sending the same email twice intentionally).
    """

    def __init__(self):
        self._counters: Dict[str, int] = {}
        self._lock = Lock()

    def next_sequence(self, session_id: str, tool_name: str) -> int:
        key = f"{session_id}:{tool_name}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            return self._counters[key]

    def reset_session(self, session_id: str) -> None:
        with self._lock:
            to_delete = [k for k in self._counters if k.startswith(f"{session_id}:")]
            for k in to_delete:
                del self._counters[k]
```

## Solution 5: Idempotency-Aware Tool Dispatcher

```python
from typing import Any, Callable, Dict


class IdempotencyAwareToolDispatcher:
    """
    Integrates idempotency key generation, sequence tracking, and
    idempotent execution into a single dispatch interface.
    Only applies idempotency to tools registered as side-effectful.
    """

    def __init__(
        self,
        executor: IdempotentToolExecutor,
        sequence_tracker: SessionSequenceTracker,
        side_effectful_tools: set = None,
    ):
        self._executor = executor
        self._tracker = sequence_tracker
        self._side_effectful = side_effectful_tools or set()

    def register_side_effectful(self, tool_name: str) -> None:
        self._side_effectful.add(tool_name)

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        session_id: str,
    ) -> Any:
        if tool_name not in self._side_effectful:
            # Idempotency-exempt: pure read tools, search, etc.
            return await tool_fn(**args)

        seq = self._tracker.next_sequence(session_id, tool_name)
        return await self._executor.execute(
            tool_name=tool_name,
            args=args,
            tool_fn=tool_fn,
            session_id=session_id,
            sequence_number=seq,
        )
```

## Solution 6: Idempotency Coverage Report

```python
import time
from typing import List


class IdempotencyCoverageReport:
    """
    Audits which tool calls used idempotency keys and reports
    coverage gaps — tools that are side-effectful but not registered.
    """

    def __init__(
        self,
        dispatcher: IdempotencyAwareToolDispatcher,
        store: IdempotencyResultStore,
    ):
        self._dispatcher = dispatcher
        self._store = store

    def render(self, observed_tools: List[str]) -> dict:
        covered = self._dispatcher._side_effectful
        uncovered = [t for t in observed_tools if t not in covered]
        store_data = self._store._load()
        completed = sum(
            1 for v in store_data.values()
            if v.get("status") == IdempotencyStatus.COMPLETED.value
        )
        failed = sum(
            1 for v in store_data.values()
            if v.get("status") == IdempotencyStatus.FAILED.value
        )
        in_flight = sum(
            1 for v in store_data.values()
            if v.get("status") == IdempotencyStatus.IN_FLIGHT.value
        )
        return {
            "generated_at": time.time(),
            "registered_side_effectful_tools": sorted(covered),
            "observed_unregistered_tools": uncovered,
            "coverage_gap": len(uncovered) > 0,
            "store_summary": {
                "total_records": len(store_data),
                "completed": completed,
                "failed": failed,
                "in_flight": in_flight,
            },
        }
```

## Comparison

| Approach | Key Generation | Result Persistence | Dedup on Retry | Sequence Tracking | Coverage Audit |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (deterministic) | No | No | No | No |
| IdempotencyResultStore | No | Yes (file/Redis) | Yes (claim-or-fetch) | No | No |
| IdempotentToolExecutor | Via generator | Via store | Yes | No | No |
| SessionSequenceTracker | No | No | No | Yes (per-session) | No |
| IdempotencyAwareToolDispatcher | Via executor | Via executor | Via executor | Via tracker | No |
| IdempotencyCoverageReport | No | No | No | No | Yes |

**Best for production**: Use Redis with `SET NX EX` as the idempotency store in multi-instance deployments — file-based stores are not safe under concurrent access from multiple agent replicas. Register all tools that send messages, create records, charge payments, or modify external state as side-effectful; leave read tools, search tools, and computation tools unregistered to avoid unnecessary overhead. Set `ttl_seconds=86400` so idempotency records expire after 24 hours — long enough to cover any plausible retry window, short enough to prevent unbounded storage growth. Never use random UUIDs as idempotency keys; always derive them deterministically from the operation's semantic inputs so that retries from different agent instances produce the same key.
