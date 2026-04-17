---
title: "Agent Doesn't Implement Idempotent Tool Execution with Deduplication Key"
description: "Agents that retry tool calls on failure without idempotency keys will duplicate side-effectful operations: a payment tool retried after a network timeout charges the user twice, an email tool retried after a slow response sends two messages. Implement idempotent tool execution that assigns a deterministic deduplication key to each tool call, stores execution outcomes, and returns the cached result on duplicate invocations without re-executing the operation."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-idempotent-tool-execution-with-deduplication-key
tags: [idempotency, deduplication-key, retry-safety, side-effects, tool-execution, exactly-once]
symptoms:
  - "A payment is charged twice after a network timeout causes a retry"
  - "An email notification is sent multiple times when the tool response is slow and the agent retries"
  - "No deduplication — retried tool calls re-execute the full operation unconditionally"
  - "The agent has no way to know whether a previous call succeeded before retrying"
  - "Idempotency is handled ad-hoc in some tools but not enforced uniformly at the dispatcher level"
---

## Why This Happens

Tool retries are a reliability mechanism for transient failures, but they are dangerous for tools with side effects. A network timeout on a payment tool call means the request may or may not have been processed — retrying without an idempotency key risks double-charging. Idempotency requires a deduplication key that is stable across retries (derived from the session, turn, and tool call identity), a store that maps keys to outcomes, and a check-before-execute pattern at the dispatcher level so that duplicate calls return the stored outcome rather than re-running the tool.

## Solution 1: Idempotency Key Generator

```python
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class IdempotencyKey:
    key: str
    session_id: str
    tool_name: str
    turn_index: int
    call_index: int      # for multiple calls to same tool in one turn

    @classmethod
    def generate(
        cls,
        session_id: str,
        tool_name: str,
        turn_index: int,
        call_index: int = 0,
        args_fingerprint: Optional[str] = None,
    ) -> "IdempotencyKey":
        components = f"{session_id}:{tool_name}:{turn_index}:{call_index}"
        if args_fingerprint:
            components += f":{args_fingerprint}"
        key = hashlib.sha256(components.encode()).hexdigest()[:24]
        return cls(
            key=key,
            session_id=session_id,
            tool_name=tool_name,
            turn_index=turn_index,
            call_index=call_index,
        )

    @classmethod
    def from_args(
        cls,
        session_id: str,
        tool_name: str,
        turn_index: int,
        args: Dict[str, Any],
        call_index: int = 0,
    ) -> "IdempotencyKey":
        import json
        args_str = json.dumps(args, sort_keys=True)
        args_fp = hashlib.sha256(args_str.encode()).hexdigest()[:8]
        return cls.generate(session_id, tool_name, turn_index, call_index, args_fp)
```

## Solution 2: Execution Outcome Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class OutcomeStatus(str, Enum):
    PENDING = "pending"       # execution started but not yet complete
    SUCCESS = "success"
    FAILURE = "failure"
    ABANDONED = "abandoned"   # pending too long — treated as unknown


@dataclass
class ExecutionOutcome:
    idempotency_key: str
    tool_name: str
    status: OutcomeStatus
    result: Any = None
    error: Optional[str] = None
    executed_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    attempt_count: int = 1

    def is_pending(self) -> bool:
        return self.status == OutcomeStatus.PENDING

    def is_stale_pending(self, max_age_seconds: float = 30.0) -> bool:
        return self.is_pending() and (time.time() - self.executed_at) > max_age_seconds

    def mark_success(self, result: Any) -> None:
        self.status = OutcomeStatus.SUCCESS
        self.result = result
        self.completed_at = time.time()

    def mark_failure(self, error: str) -> None:
        self.status = OutcomeStatus.FAILURE
        self.error = error
        self.completed_at = time.time()
```

## Solution 3: Idempotency Store

```python
import time
from threading import Lock
from typing import Dict, Optional


class IdempotencyStore:
    """
    Stores execution outcomes keyed by idempotency key.
    Supports pending state to handle concurrent duplicate requests.
    """

    def __init__(
        self,
        ttl_seconds: float = 3600.0,
        max_entries: int = 50000,
        stale_pending_seconds: float = 30.0,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._stale_pending = stale_pending_seconds
        self._store: Dict[str, ExecutionOutcome] = {}
        self._lock = Lock()

    def check(self, key: str) -> Optional[ExecutionOutcome]:
        """Returns existing outcome if found and not stale, else None."""
        with self._lock:
            outcome = self._store.get(key)
            if outcome is None:
                return None
            if outcome.is_stale_pending(self._stale_pending):
                # Stale pending — allow re-execution
                outcome.status = OutcomeStatus.ABANDONED
                del self._store[key]
                return None
            return outcome

    def set_pending(self, key: str, tool_name: str) -> ExecutionOutcome:
        """Atomically registers the key as pending before execution starts."""
        with self._lock:
            self._evict()
            outcome = ExecutionOutcome(
                idempotency_key=key,
                tool_name=tool_name,
                status=OutcomeStatus.PENDING,
            )
            self._store[key] = outcome
            return outcome

    def update(self, outcome: ExecutionOutcome) -> None:
        with self._lock:
            self._store[outcome.idempotency_key] = outcome

    def _evict(self) -> None:
        if len(self._store) < self._max:
            return
        cutoff = time.time() - self._ttl
        expired = [k for k, v in self._store.items() if v.executed_at < cutoff]
        for k in expired[:max(1, len(expired))]:
            del self._store[k]

    def size(self) -> int:
        with self._lock:
            return len(self._store)
```

## Solution 4: Idempotent Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class IdempotentToolDispatcher:
    """
    Wraps tool execution with idempotency checking.
    On the first call, executes the tool and stores the outcome.
    On duplicate calls with the same key, returns the stored outcome.
    """

    def __init__(
        self,
        store: IdempotencyStore,
        audit_logger: "IdempotencyAuditLogger",
    ):
        self._store = store
        self._logger = audit_logger

    async def dispatch(
        self,
        idempotency_key: IdempotencyKey,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        key_str = idempotency_key.key

        # Check for existing outcome
        existing = self._store.check(key_str)
        if existing is not None:
            if existing.is_pending():
                # Wait for the concurrent execution to complete
                return await self._wait_for_completion(key_str)
            self._logger.record_dedup(idempotency_key, existing)
            return {
                "result": existing.result,
                "idempotency_key": key_str,
                "deduplicated": True,
                "original_executed_at": existing.executed_at,
                "status": existing.status.value,
            }

        # Register as pending before executing
        outcome = self._store.set_pending(key_str, idempotency_key.tool_name)

        start = time.monotonic()
        try:
            result = await tool_fn(*args, **kwargs)
            outcome.mark_success(result)
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            self._store.update(outcome)
            self._logger.record_execution(idempotency_key, outcome, latency_ms)
            return {
                "result": result,
                "idempotency_key": key_str,
                "deduplicated": False,
                "latency_ms": latency_ms,
                "status": "success",
            }
        except Exception as exc:
            outcome.mark_failure(str(exc))
            self._store.update(outcome)
            self._logger.record_execution(idempotency_key, outcome, 0.0)
            raise

    async def _wait_for_completion(
        self,
        key_str: str,
        poll_interval: float = 0.05,
        max_wait: float = 30.0,
    ) -> dict:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            outcome = self._store.check(key_str)
            if outcome and not outcome.is_pending():
                return {
                    "result": outcome.result,
                    "idempotency_key": key_str,
                    "deduplicated": True,
                    "status": outcome.status.value,
                }
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"timed out waiting for concurrent execution of key {key_str}")
```

## Solution 5: Side-Effect Tool Registry

```python
from typing import Dict, Set


class SideEffectToolRegistry:
    """
    Classifies tools by whether they have side effects that require
    idempotency protection. Read-only tools do not need deduplication.
    """

    def __init__(self):
        self._side_effect_tools: Set[str] = set()
        self._read_only_tools: Set[str] = set()

    def register_side_effect(self, tool_name: str) -> None:
        self._side_effect_tools.add(tool_name)

    def register_read_only(self, tool_name: str) -> None:
        self._read_only_tools.add(tool_name)

    def requires_idempotency(self, tool_name: str) -> bool:
        if tool_name in self._read_only_tools:
            return False
        # Default to requiring idempotency for unknown tools (safe default)
        return True

    def side_effect_tools(self) -> list:
        return sorted(self._side_effect_tools)
```

## Solution 6: Idempotency Audit Logger

```python
import time
from typing import List


class IdempotencyAuditLogger:
    """
    Records tool execution and deduplication events for audit and analysis.
    """

    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: List[dict] = []

    def record_execution(
        self,
        key: IdempotencyKey,
        outcome: ExecutionOutcome,
        latency_ms: float,
    ) -> None:
        self._append({
            "event": "execution",
            "key": key.key,
            "tool_name": key.tool_name,
            "session_id": key.session_id,
            "turn_index": key.turn_index,
            "status": outcome.status.value,
            "latency_ms": latency_ms,
            "error": outcome.error,
        })

    def record_dedup(self, key: IdempotencyKey, outcome: ExecutionOutcome) -> None:
        self._append({
            "event": "deduplicated",
            "key": key.key,
            "tool_name": key.tool_name,
            "session_id": key.session_id,
            "original_status": outcome.status.value,
            "age_seconds": round(time.time() - outcome.executed_at, 1),
        })

    def _append(self, record: dict) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        record["ts"] = time.time()
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        executions = [r for r in recent if r["event"] == "execution"]
        dedups = [r for r in recent if r["event"] == "deduplicated"]
        return {
            "window_seconds": window_seconds,
            "executions": len(executions),
            "deduplicated": len(dedups),
            "dedup_rate": round(len(dedups) / max(len(executions) + len(dedups), 1), 4),
            "failures": sum(1 for r in executions if r.get("status") == "failure"),
        }
```

## Comparison

| Approach | Key Generation | Pending State | Dedup on Retry | Concurrent Safety | Side-Effect Classification |
|---|---|---|---|---|---|
| IdempotencyKey | Yes (SHA-256) | No | No | No | No |
| IdempotencyStore | No | Yes | Yes | Yes (Lock) | No |
| IdempotentToolDispatcher | No | Via store | Yes | Via store | No |
| SideEffectToolRegistry | No | No | No | No | Yes |
| IdempotencyAuditLogger | No | No | No | No | No |

**Best for production**: Use `IdempotencyKey.from_args()` only for tools where argument content determines uniqueness (e.g., "send email to X" should dedup on recipient + subject); use `IdempotencyKey.generate()` without args fingerprint for tools where the call position (turn + index) determines uniqueness (e.g., "charge payment" should dedup on the specific turn, not re-dedup if arguments happen to match a prior turn). Set `ttl_seconds=3600` — idempotency keys older than one hour are unlikely to be re-presented and holding them longer wastes memory. Monitor `dedup_rate` via the audit logger: a rate above 0.05 (5%) indicates the agent is retrying aggressively; investigate whether tool timeouts are too short or the LLM is generating duplicate calls within the same turn.
