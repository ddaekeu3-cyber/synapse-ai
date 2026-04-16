---
title: "Agent Doesn't Implement Idempotent Tool Call Replay After Partial Failure"
description: "Agents that retry failed tool calls without idempotency guarantees cause duplicate side effects: a payment tool retried after a network timeout charges the user twice, a record-creation tool retried creates two records, and a message-sending tool retried delivers the message twice. Implement idempotent tool call replay that assigns a stable idempotency key to each invocation and passes it to the tool so the backend can deduplicate duplicate calls."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-idempotent-tool-call-replay-after-partial-failure
tags: [idempotency, retry, tool-replay, duplicate-prevention, partial-failure, side-effects]
symptoms:
  - "Payment tools retried after timeout result in duplicate charges"
  - "Record creation tools retried create multiple identical records"
  - "No stable idempotency key passed to mutating tool calls"
  - "Agent retries tool calls on network error without checking whether the first call succeeded"
  - "Tool call logs show duplicate entries for the same logical operation"
---

## Why This Happens

Network failures are ambiguous: a timeout does not mean the request failed — it means the response was lost. If a tool executed successfully but its response never reached the agent, a naive retry re-executes the tool, causing duplicate side effects. Idempotency keys solve this at the protocol level: the tool or its backend treats all calls with the same key as the same operation and returns the cached result of the first execution rather than executing again. The agent must generate a stable key per logical operation — stable across retries but unique per distinct intent — and pass it with every mutating tool call.

## Solution 1: Idempotency Key Generator

```python
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class IdempotencyKey:
    key: str
    tool_name: str
    session_id: str
    logical_operation_id: str
    created_at: float


class IdempotencyKeyGenerator:
    """
    Generates stable idempotency keys for tool calls.
    Keys are deterministic for the same (session, operation, tool) triple
    so that retries reuse the same key, while distinct operations get distinct keys.
    """

    def generate(
        self,
        tool_name: str,
        session_id: str,
        logical_operation_id: str,
    ) -> IdempotencyKey:
        raw = f"{tool_name}:{session_id}:{logical_operation_id}"
        key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        return IdempotencyKey(
            key=key,
            tool_name=tool_name,
            session_id=session_id,
            logical_operation_id=logical_operation_id,
            created_at=time.time(),
        )

    def generate_unique(self, tool_name: str, session_id: str) -> IdempotencyKey:
        """
        Generates a unique key for a new distinct operation
        (not a retry of an existing one).
        """
        operation_id = uuid.uuid4().hex
        return self.generate(tool_name, session_id, operation_id)
```

## Solution 2: Idempotency Key Store

```python
import time
from threading import Lock
from typing import Any, Dict, Optional


class IdempotencyKeyStore:
    """
    Records the result of a tool call against its idempotency key.
    On replay, returns the cached result rather than re-executing.
    TTL prevents the store from growing unbounded.
    """

    def __init__(self, ttl_seconds: float = 3600.0, max_entries: int = 10000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            self._evict_expired()
            return key in self._store

    def record_result(self, key: str, result: Any, tool_name: str = "") -> None:
        with self._lock:
            self._evict_expired()
            if len(self._store) >= self._max:
                oldest = min(self._store, key=lambda k: self._store[k]["recorded_at"])
                del self._store[oldest]
            self._store[key] = {
                "result": result,
                "tool_name": tool_name,
                "recorded_at": time.time(),
            }

    def get_result(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.time() - entry["recorded_at"] > self._ttl:
                del self._store[key]
                return None
            return entry["result"]

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["recorded_at"] > self._ttl]
        for k in expired:
            del self._store[k]

    def stats(self) -> dict:
        with self._lock:
            return {"stored_keys": len(self._store), "ttl_seconds": self._ttl}
```

## Solution 3: Idempotent Tool Call Wrapper

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class IdempotentCallResult:
    result: Any
    was_replay: bool
    idempotency_key: str
    tool_name: str


class IdempotentToolCallWrapper:
    """
    Wraps a tool callable with idempotency: checks the store before calling,
    records the result after a successful call, and returns cached results on replay.
    """

    def __init__(
        self,
        key_store: IdempotencyKeyStore,
        key_generator: IdempotencyKeyGenerator,
    ):
        self._store = key_store
        self._gen = key_generator

    async def call(
        self,
        tool_fn: Callable,
        tool_name: str,
        tool_args: dict,
        idempotency_key: Optional[IdempotencyKey] = None,
        session_id: str = "",
        *args: Any,
        **kwargs: Any,
    ) -> IdempotentCallResult:
        if idempotency_key is None:
            idempotency_key = self._gen.generate_unique(tool_name, session_id)

        key_str = idempotency_key.key

        cached = self._store.get_result(key_str)
        if cached is not None:
            return IdempotentCallResult(
                result=cached,
                was_replay=True,
                idempotency_key=key_str,
                tool_name=tool_name,
            )

        # Inject the idempotency key into args if the tool accepts it
        if "idempotency_key" in tool_fn.__code__.co_varnames:
            tool_args = {**tool_args, "idempotency_key": key_str}

        result = await tool_fn(**tool_args)

        self._store.record_result(key_str, result, tool_name)

        return IdempotentCallResult(
            result=result,
            was_replay=False,
            idempotency_key=key_str,
            tool_name=tool_name,
        )
```

## Solution 4: Replay-Aware Retry Executor

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class RetryWithIdempotencyConfig:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    retryable_exceptions: tuple = (TimeoutError, ConnectionError, OSError)


class ReplayAwareRetryExecutor:
    """
    Retries a tool call using a stable idempotency key so every attempt
    is a replay from the backend's perspective — no duplicate side effects.
    """

    def __init__(
        self,
        wrapper: IdempotentToolCallWrapper,
        config: RetryWithIdempotencyConfig,
        key_generator: IdempotencyKeyGenerator,
    ):
        self._wrapper = wrapper
        self._config = config
        self._gen = key_generator

    async def execute(
        self,
        tool_fn: Callable,
        tool_name: str,
        tool_args: dict,
        session_id: str,
        logical_operation_id: Optional[str] = None,
    ) -> IdempotentCallResult:
        import uuid
        op_id = logical_operation_id or uuid.uuid4().hex
        key = self._gen.generate(tool_name, session_id, op_id)

        delay = self._config.base_delay_seconds
        last_exc: Optional[Exception] = None

        for attempt in range(self._config.max_attempts):
            try:
                return await self._wrapper.call(
                    tool_fn=tool_fn,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    idempotency_key=key,
                    session_id=session_id,
                )
            except self._config.retryable_exceptions as exc:
                last_exc = exc
                if attempt < self._config.max_attempts - 1:
                    await asyncio.sleep(min(delay, self._config.max_delay_seconds))
                    delay *= 2

        raise RuntimeError(
            f"Tool '{tool_name}' failed after {self._config.max_attempts} attempts: {last_exc}"
        )
```

## Solution 5: Mutating Tool Classifier

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Set


class ToolMutability(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    UNKNOWN = "unknown"


class MutatingToolClassifier:
    """
    Classifies tools as read-only or mutating based on registered names.
    Only mutating tools need idempotency keys — applying them to read-only
    tools wastes key store space and adds unnecessary overhead.
    """

    def __init__(self):
        self._mutating: Set[str] = set()
        self._read_only: Set[str] = set()

    def register_mutating(self, *tool_names: str) -> None:
        for name in tool_names:
            self._mutating.add(name)

    def register_read_only(self, *tool_names: str) -> None:
        for name in tool_names:
            self._read_only.add(name)

    def classify(self, tool_name: str) -> ToolMutability:
        if tool_name in self._mutating:
            return ToolMutability.MUTATING
        if tool_name in self._read_only:
            return ToolMutability.READ_ONLY
        return ToolMutability.UNKNOWN

    def requires_idempotency(self, tool_name: str) -> bool:
        cls = self.classify(tool_name)
        return cls in (ToolMutability.MUTATING, ToolMutability.UNKNOWN)
```

## Solution 6: Idempotency Replay Dashboard

```python
import time
from typing import List


class IdempotencyReplayDashboard:
    """
    Tracks replay rates, duplicate prevention counts, and key store health
    to verify that idempotency is working and reducing duplicate side effects.
    """

    def __init__(self, key_store: IdempotencyKeyStore):
        self._store = key_store
        self._calls: List[dict] = []
        self._recorded_at: List[float] = []

    def record_call(self, result: IdempotentCallResult) -> None:
        self._calls.append({
            "tool_name": result.tool_name,
            "was_replay": result.was_replay,
        })
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            c for c, ts in zip(self._calls, self._recorded_at) if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}

        replays = [c for c in recent if c["was_replay"]]
        by_tool: dict = {}
        for c in recent:
            t = c["tool_name"]
            if t not in by_tool:
                by_tool[t] = {"total": 0, "replays": 0}
            by_tool[t]["total"] += 1
            if c["was_replay"]:
                by_tool[t]["replays"] += 1

        return {
            "window_seconds": window_seconds,
            "total_calls": len(recent),
            "replay_calls": len(replays),
            "replay_rate_pct": round(len(replays) / max(len(recent), 1) * 100, 2),
            "duplicate_side_effects_prevented": len(replays),
            "key_store": self._store.stats(),
            "by_tool": by_tool,
        }
```

## Comparison

| Approach | Key Generation | Duplicate Detection | Retry Integration | Tool Classification | Audit |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (deterministic) | No | No | No | No |
| IdempotencyKeyStore | No | Yes (TTL-backed) | No | No | No |
| IdempotentToolCallWrapper | Via generator | Via store | No | No | No |
| ReplayAwareRetryExecutor | Via generator | Via wrapper | Yes | No | No |
| MutatingToolClassifier | No | No | No | Yes | No |
| IdempotencyReplayDashboard | No | No | No | No | Yes |

**Best for production**: Register all payment, record-creation, notification, and state-mutation tools as mutating in `MutatingToolClassifier` — this ensures idempotency keys are applied exactly where side effects occur and skipped for search/read tools where they add no value. Use `logical_operation_id` derived from the agent's task plan step ID rather than a random UUID: this makes retries from agent-level restarts (not just network retries) also idempotent, since the same step ID produces the same key. Set `ttl_seconds=3600` for the key store — short enough to not consume memory indefinitely, long enough to cover any realistic retry window. Monitor `replay_rate_pct` in `IdempotencyReplayDashboard`: a rate above 5% indicates frequent network failures or agent restart loops that should be investigated independently.
