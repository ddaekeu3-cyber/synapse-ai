---
title: "Agent Doesn't Implement Idempotency Keys for State-Mutating Tool Calls"
description: "Agents that retry failed state-mutating tool calls without idempotency keys cause duplicate side effects: a payment processed twice, a record inserted twice, an email sent twice. Implement idempotency key generation and tracking that generates a stable key per logical operation, passes it to downstream APIs, and deduplicates at the agent layer for APIs that don't natively support idempotency."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-idempotency-keys-for-state-mutating-tool-calls
tags: [idempotency, duplicate-prevention, state-mutation, retry-safety, at-most-once, safe-retries]
symptoms:
  - "Payment tool retried on timeout causes a double charge"
  - "Database insert tool retried after network failure creates duplicate records"
  - "Email tool called twice because the first call timed out and the retry succeeded"
  - "No idempotency key passed to downstream APIs — every retry is a new operation"
  - "Cannot safely retry any state-mutating tool call without risk of duplication"
---

## Why This Happens

Retrying a failed API call is safe for reads but dangerous for writes. A payment API called twice charges twice unless the first call included an idempotency key that the API uses to detect and deduplicate the second call. Most payment, email, and database APIs support idempotency keys, but agents must generate them correctly: the key must be stable across retries of the same logical operation (not randomly generated on each attempt) and scoped to the operation's identity (session + tool + arguments).

## Solution 1: Idempotency Key Generator

```python
import hashlib
import json
from typing import Any, Dict, Optional


class IdempotencyKeyGenerator:
    """
    Generates stable idempotency keys for tool calls.
    The key is deterministic: the same session + tool + arguments always produce the same key.
    An optional nonce allows multiple distinct operations with the same arguments.
    """

    @staticmethod
    def generate(
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        nonce: Optional[str] = None,
    ) -> str:
        payload = {
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }
        if nonce:
            payload["nonce"] = nonce
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    @staticmethod
    def generate_with_timestamp_window(
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        window_minutes: int = 5,
    ) -> str:
        """
        Generates a key that is stable within a time window.
        Calls with the same arguments within `window_minutes` get the same key.
        After the window, a new key is generated.
        """
        import time
        window_bucket = int(time.time() / (window_minutes * 60))
        payload = {
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "window": window_bucket,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]
```

## Solution 2: Idempotency Record Store

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class OperationStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IdempotencyRecord:
    idempotency_key: str
    tool_name: str
    session_id: str
    status: OperationStatus = OperationStatus.IN_PROGRESS
    result: Any = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    ttl_seconds: float = 86400.0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def is_complete(self) -> bool:
        return self.status in (OperationStatus.COMPLETED, OperationStatus.FAILED)


class IdempotencyRecordStore:
    """
    Stores idempotency records keyed by idempotency key.
    In-memory implementation — replace with Redis or database for distributed agents.
    """

    def __init__(self):
        self._records: Dict[str, IdempotencyRecord] = {}

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        record = self._records.get(key)
        if record and record.is_expired():
            del self._records[key]
            return None
        return record

    def create(self, record: IdempotencyRecord) -> None:
        self._records[record.idempotency_key] = record

    def complete(self, key: str, result: Any) -> None:
        record = self._records.get(key)
        if record:
            record.status = OperationStatus.COMPLETED
            record.result = result
            record.completed_at = time.time()

    def fail(self, key: str, error: str) -> None:
        record = self._records.get(key)
        if record:
            record.status = OperationStatus.FAILED
            record.error = error
            record.completed_at = time.time()

    def prune_expired(self) -> int:
        expired = [k for k, r in self._records.items() if r.is_expired()]
        for k in expired:
            del self._records[k]
        return len(expired)
```

## Solution 3: Idempotent Tool Executor

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class IdempotentToolExecutor:
    """
    Wraps tool calls with idempotency semantics.
    On the first call for a key: execute and store result.
    On repeat calls for the same key: return the stored result without re-executing.
    If a call is in-progress (concurrent duplicate): wait for it to complete.
    """

    def __init__(self, store: IdempotencyRecordStore):
        self._store = store
        self._in_progress_events: Dict[str, asyncio.Event] = {}

    async def execute(
        self,
        idempotency_key: str,
        tool_name: str,
        session_id: str,
        tool_fn: Callable,
        arguments: Dict[str, Any],
    ) -> Any:
        # Check existing record
        existing = self._store.get(idempotency_key)
        if existing:
            if existing.status == OperationStatus.COMPLETED:
                return existing.result
            if existing.status == OperationStatus.FAILED:
                raise RuntimeError(f"Operation permanently failed: {existing.error}")
            # IN_PROGRESS — wait for it
            event = self._in_progress_events.get(idempotency_key)
            if event:
                await event.wait()
                record = self._store.get(idempotency_key)
                if record and record.status == OperationStatus.COMPLETED:
                    return record.result
                raise RuntimeError("Concurrent operation failed")

        # New operation — create record and execute
        record = IdempotencyRecord(
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            session_id=session_id,
        )
        self._store.create(record)
        event = asyncio.Event()
        self._in_progress_events[idempotency_key] = event

        try:
            result = await tool_fn(**arguments)
            self._store.complete(idempotency_key, result)
            return result
        except Exception as exc:
            self._store.fail(idempotency_key, str(exc)[:200])
            raise
        finally:
            event.set()
            self._in_progress_events.pop(idempotency_key, None)
```

## Solution 4: Idempotency-Aware Tool Dispatcher

```python
from typing import Any, Callable, Dict, Optional, Set


# Tools that must use idempotency keys — all state-mutating operations
MUTATING_TOOLS: Set[str] = {
    "charge_payment", "send_email", "send_sms", "create_record",
    "update_record", "delete_record", "transfer_funds", "place_order",
    "schedule_job", "send_notification", "post_message",
}


class IdempotencyAwareToolDispatcher:
    """
    Wraps the tool dispatch layer with automatic idempotency key management.
    Mutating tools get idempotency keys; read-only tools execute directly.
    """

    def __init__(
        self,
        executor: IdempotentToolExecutor,
        key_generator: IdempotencyKeyGenerator,
        mutating_tools: Set[str] = None,
    ):
        self._executor = executor
        self._keygen = key_generator
        self._mutating = mutating_tools or MUTATING_TOOLS

    async def dispatch(
        self,
        tool_name: str,
        session_id: str,
        arguments: Dict[str, Any],
        tool_fn: Callable,
        explicit_idempotency_key: Optional[str] = None,
    ) -> Any:
        if tool_name not in self._mutating:
            # Read-only — execute directly
            return await tool_fn(**arguments)

        key = explicit_idempotency_key or self._keygen.generate(
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
        )

        return await self._executor.execute(
            idempotency_key=key,
            tool_name=tool_name,
            session_id=session_id,
            tool_fn=tool_fn,
            arguments=arguments,
        )
```

## Solution 5: Downstream API Key Injector

```python
from typing import Any, Callable, Dict


class DownstreamAPIKeyInjector:
    """
    Injects idempotency keys into downstream API requests that support them.
    Different APIs use different header/parameter names for idempotency keys.
    """

    API_KEY_FIELD_MAP: Dict[str, str] = {
        "stripe": "idempotency_key",
        "braintree": "transaction_id",
        "sendgrid": "x-idempotency-key",
        "twilio": "idempotency_key",
        "generic": "idempotency_key",
    }

    def inject(
        self,
        api_type: str,
        request_kwargs: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        field_name = self.API_KEY_FIELD_MAP.get(api_type, "idempotency_key")
        injected = dict(request_kwargs)
        if "headers" in injected:
            injected["headers"] = dict(injected["headers"])
            injected["headers"][field_name] = idempotency_key
        else:
            injected[field_name] = idempotency_key
        return injected
```

## Solution 6: Idempotency Audit Reporter

```python
import time


class IdempotencyAuditReporter:
    """
    Reports on idempotency key usage: deduplication rate, in-progress operations,
    and failed operations that may require manual resolution.
    """

    def __init__(self, store: IdempotencyRecordStore):
        self._store = store
        self._deduplicated = 0
        self._executed = 0

    def record_execution(self) -> None:
        self._executed += 1

    def record_deduplication(self) -> None:
        self._deduplicated += 1

    def summary(self) -> dict:
        in_progress = [
            r for r in self._store._records.values()
            if r.status == OperationStatus.IN_PROGRESS
        ]
        failed = [
            r for r in self._store._records.values()
            if r.status == OperationStatus.FAILED
        ]
        total = self._executed + self._deduplicated
        return {
            "total_operations": total,
            "unique_executed": self._executed,
            "deduplicated": self._deduplicated,
            "deduplication_rate": round(self._deduplicated / max(total, 1), 4),
            "in_progress_count": len(in_progress),
            "failed_operations": len(failed),
            "stored_records": len(self._store._records),
        }
```

## Comparison

| Approach | Key Generation | Deduplication | Concurrent Wait | API Injection | Audit |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (deterministic) | No | No | No | No |
| IdempotencyRecordStore | No | Via records | No | No | No |
| IdempotentToolExecutor | No | Yes (store lookup) | Yes (asyncio.Event) | No | No |
| IdempotencyAwareToolDispatcher | Via generator | Via executor | Via executor | No | No |
| DownstreamAPIKeyInjector | No | No | No | Yes (per-API) | No |

**Best for production**: Use `IdempotencyKeyGenerator.generate()` with stable session + tool + arguments as the key — this ensures the same logical operation always produces the same key across retries. For time-windowed operations (e.g., "send daily report"), use `generate_with_timestamp_window()` with a 24-hour window to prevent accidental double-sends while allowing legitimate retries outside the window. Replace the in-memory store with Redis using `SET key value NX EX 86400` for distributed multi-process agents — this ensures deduplication works across all agent instances, not just within a single process.
