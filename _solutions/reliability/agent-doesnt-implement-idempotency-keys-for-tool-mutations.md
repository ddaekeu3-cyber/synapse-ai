---
title: "Agent Doesn't Implement Idempotency Keys for Tool Mutations"
description: "Agents that retry failed mutating tool calls without idempotency keys risk duplicate execution: a payment is charged twice, a database record is created twice, a message is sent twice. The original call may have succeeded on the server side while the response was lost in transit. Implement idempotency keys that are generated per tool-call intent, persisted across retry attempts, and passed to downstream APIs so that retried calls are recognized as duplicates and not re-executed."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-idempotency-keys-for-tool-mutations
tags: [idempotency, mutation-safety, retry-safety, duplicate-prevention, at-most-once, tool-mutations]
symptoms:
  - "Payment tool retried after timeout charges the customer twice"
  - "Create-record tool retried after network error creates a duplicate database entry"
  - "Send-message tool called again on retry sends the same message twice"
  - "No idempotency key passed to external APIs that support them"
  - "Retry logic treats all tool failures identically regardless of whether the call may have succeeded"
---

## Why This Happens

Network failures are ambiguous: a timeout means the response was not received, not that the operation did not execute. Mutating operations — payments, record creation, message sending, state transitions — may have completed on the server before the connection dropped. Retrying without an idempotency key causes the server to execute the operation again. Idempotency keys solve this by associating a unique identifier with a specific operation intent. If the server has already processed a request with that key, it returns the cached result rather than executing again.

## Solution 1: Idempotency Key Generator

```python
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
import time


@dataclass
class IdempotencyKey:
    key: str
    tool_name: str
    args_hash: str
    session_id: str
    attempt: int = 1
    generated_at: float = field(default_factory=time.time)


class IdempotencyKeyGenerator:
    """
    Generates idempotency keys for mutating tool calls.
    Keys are stable across retry attempts for the same operation
    but unique across different operations and sessions.
    """

    @staticmethod
    def _args_hash(args: Any) -> str:
        import json
        try:
            canonical = json.dumps(args, sort_keys=True)
        except (TypeError, ValueError):
            canonical = str(args)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def generate(
        self,
        tool_name: str,
        args: Any,
        session_id: str,
        operation_nonce: Optional[str] = None,
    ) -> IdempotencyKey:
        """
        operation_nonce: a caller-supplied value that distinguishes
        two logically different calls to the same tool with the same args
        within a session (e.g., turn index). If omitted, a UUID is used.
        """
        nonce = operation_nonce or str(uuid.uuid4())[:8]
        args_hash = self._args_hash(args)
        raw = f"{session_id}:{tool_name}:{args_hash}:{nonce}"
        key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        return IdempotencyKey(
            key=key,
            tool_name=tool_name,
            args_hash=args_hash,
            session_id=session_id,
        )
```

## Solution 2: Idempotency Key Store

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Any, Dict, Optional


class IdempotencyStatus(str, Enum):
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class IdempotencyRecord:
    key: str
    tool_name: str
    status: IdempotencyStatus
    result: Any = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    attempt_count: int = 1

    def is_terminal(self) -> bool:
        return self.status in (IdempotencyStatus.SUCCEEDED, IdempotencyStatus.FAILED)


class IdempotencyKeyStore:
    """
    Tracks the status and result of idempotent tool calls.
    Prevents duplicate execution by returning cached results on retry.
    """

    def __init__(self, ttl_seconds: float = 3600.0):
        self._ttl = ttl_seconds
        self._records: Dict[str, IdempotencyRecord] = {}
        self._lock = Lock()

    def start(self, idem_key: IdempotencyKey) -> Optional[IdempotencyRecord]:
        """
        Marks key as in-flight. Returns existing record if key was already seen.
        Returns None if this is the first time the key is registered.
        """
        with self._lock:
            existing = self._records.get(idem_key.key)
            if existing:
                existing.attempt_count += 1
                return existing
            self._records[idem_key.key] = IdempotencyRecord(
                key=idem_key.key,
                tool_name=idem_key.tool_name,
                status=IdempotencyStatus.IN_FLIGHT,
            )
            return None

    def complete(self, key: str, result: Any) -> None:
        with self._lock:
            record = self._records.get(key)
            if record:
                record.status = IdempotencyStatus.SUCCEEDED
                record.result = result
                record.completed_at = time.time()

    def fail(self, key: str, error: str) -> None:
        with self._lock:
            record = self._records.get(key)
            if record:
                record.status = IdempotencyStatus.FAILED
                record.error = error
                record.completed_at = time.time()

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        with self._lock:
            return self._records.get(key)

    def evict_expired(self) -> int:
        cutoff = time.time() - self._ttl
        with self._lock:
            expired = [k for k, r in self._records.items() if r.created_at < cutoff]
            for k in expired:
                del self._records[k]
        return len(expired)
```

## Solution 3: Idempotent Tool Executor

```python
import asyncio
from typing import Any, Callable, Optional


class DuplicateToolCallError(Exception):
    def __init__(self, key: str, status: IdempotencyStatus):
        super().__init__(f"idempotency key '{key}' already in status '{status.value}'")
        self.key = key
        self.status = status


class IdempotentToolExecutor:
    """
    Wraps a mutating tool call with idempotency enforcement.
    On first call: executes and stores result.
    On retry with same key: returns cached result without re-executing.
    On retry of in-flight call: waits and returns result when available.
    """

    def __init__(
        self,
        store: IdempotencyKeyStore,
        in_flight_wait_seconds: float = 30.0,
    ):
        self._store = store
        self._wait = in_flight_wait_seconds

    async def execute(
        self,
        idem_key: IdempotencyKey,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        existing = self._store.start(idem_key)

        if existing and existing.status == IdempotencyStatus.SUCCEEDED:
            return existing.result

        if existing and existing.status == IdempotencyStatus.FAILED:
            raise RuntimeError(f"previous attempt failed: {existing.error}")

        if existing and existing.status == IdempotencyStatus.IN_FLIGHT:
            # Wait for the in-flight call to complete
            deadline = asyncio.get_event_loop().time() + self._wait
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.5)
                record = self._store.get(idem_key.key)
                if record and record.is_terminal():
                    if record.status == IdempotencyStatus.SUCCEEDED:
                        return record.result
                    raise RuntimeError(f"concurrent attempt failed: {record.error}")
            raise TimeoutError(f"in-flight idempotency key '{idem_key.key}' did not resolve")

        try:
            result = await fn(*args, **kwargs)
            self._store.complete(idem_key.key, result)
            return result
        except Exception as exc:
            self._store.fail(idem_key.key, str(exc)[:200])
            raise
```

## Solution 4: Idempotency-Aware Tool Dispatcher

```python
from typing import Any, Callable, Optional, Set


_MUTATING_TOOLS: Set[str] = set()


def register_mutating_tool(tool_name: str) -> None:
    """Register a tool as mutating so idempotency keys are applied."""
    _MUTATING_TOOLS.add(tool_name)


class IdempotencyAwareToolDispatcher:
    """
    Applies idempotency enforcement to registered mutating tools.
    Read-only tools are dispatched directly without key overhead.
    """

    def __init__(
        self,
        executor: IdempotentToolExecutor,
        generator: IdempotencyKeyGenerator,
        mutating_tools: Optional[Set[str]] = None,
    ):
        self._executor = executor
        self._generator = generator
        self._mutating = mutating_tools or _MUTATING_TOOLS

    async def dispatch(
        self,
        tool_name: str,
        args: Any,
        fn: Callable,
        session_id: str,
        operation_nonce: Optional[str] = None,
    ) -> Any:
        if tool_name not in self._mutating:
            return await fn()

        idem_key = self._generator.generate(
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            operation_nonce=operation_nonce,
        )
        return await self._executor.execute(idem_key, fn)
```

## Solution 5: Idempotency Key Injector for HTTP

```python
from typing import Any, Callable, Dict, Optional


class IdempotencyHTTPHeaderInjector:
    """
    Injects the idempotency key as an HTTP header for APIs that support
    Idempotency-Key or X-Idempotency-Key headers (Stripe, Braintree, etc.).
    """

    _HEADER_NAME_MAP = {
        "stripe": "Idempotency-Key",
        "braintree": "X-Idempotency-Key",
        "default": "Idempotency-Key",
    }

    def inject_headers(
        self,
        headers: Dict[str, str],
        idem_key: IdempotencyKey,
        provider: str = "default",
    ) -> Dict[str, str]:
        header_name = self._HEADER_NAME_MAP.get(provider, "Idempotency-Key")
        return {**headers, header_name: idem_key.key}
```

## Solution 6: Idempotency Coverage Auditor

```python
from typing import Dict, List, Set


class IdempotencyCoverageAuditor:
    """
    Audits which mutating tools in the catalog have idempotency coverage.
    Surfaces tools that perform mutations but are not registered for
    idempotency key enforcement.
    """

    def __init__(
        self,
        all_tools: List[str],
        mutating_tools: Set[str],
        protected_tools: Set[str],
    ):
        self._all = set(all_tools)
        self._mutating = mutating_tools
        self._protected = protected_tools

    def audit(self) -> dict:
        unprotected = self._mutating - self._protected
        non_mutating_protected = self._protected - self._mutating

        return {
            "total_tools": len(self._all),
            "mutating_tools": len(self._mutating),
            "idempotency_protected": len(self._protected),
            "unprotected_mutating": sorted(unprotected),
            "over_protected": sorted(non_mutating_protected),
            "coverage_pct": round(
                len(self._protected & self._mutating) / max(len(self._mutating), 1) * 100, 1
            ),
        }
```

## Comparison

| Approach | Key Generation | Duplicate Detection | In-Flight Wait | HTTP Header Injection | Coverage Audit |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (stable across retries) | No | No | No | No |
| IdempotencyKeyStore | No | Yes | No | No | No |
| IdempotentToolExecutor | No | Via store | Yes | No | No |
| IdempotencyAwareToolDispatcher | Via generator | Via executor | Via executor | No | No |
| IdempotencyHTTPHeaderInjector | No | No | No | Yes | No |
| IdempotencyCoverageAuditor | No | No | No | No | Yes |

**Best for production**: Generate `operation_nonce` from the turn index and tool-call position within the turn — this makes idempotency keys deterministic and reproducible across agent restarts, not just within a single process. Set `ttl_seconds=3600` so keys expire after an hour; payment idempotency windows are typically 24 hours, so match your TTL to the downstream provider's deduplication window. Always pass idempotency keys for: payment charges, database record creation, external API writes, email/SMS sends. Use `IdempotencyCoverageAuditor` as a CI check to prevent new mutating tools from being added without coverage.
