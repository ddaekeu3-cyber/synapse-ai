---
title: "Agent Doesn't Implement Idempotent Tool Call Execution"
description: "Agents that retry failed tool calls without idempotency guarantees cause duplicate side effects: a payment is charged twice, an email is sent twice, a record is inserted twice. Implement idempotent tool call execution using deduplication keys, result caching for already-executed calls, and per-tool idempotency policies that distinguish safe-to-retry from must-deduplicate operations."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-idempotent-tool-call-execution
tags: [idempotency, duplicate-prevention, retry-safety, deduplication-key, side-effects, tool-call-safety]
symptoms:
  - "Retried tool calls cause duplicate database records or double-charged payments"
  - "Email or notification tools send multiple identical messages on transient failures"
  - "No distinction between read-only tools (safe to retry) and write tools (must deduplicate)"
  - "Tool call retry logic has no memory of what has already been executed"
  - "Idempotency keys are generated per-attempt instead of per-logical-call"
---

## Why This Happens

Retry logic is added to tool calls to handle transient failures, but without idempotency guarantees, each retry attempt is treated as a new operation by the downstream service. A write tool (send email, charge payment, insert record) must carry an idempotency key that is stable across retry attempts — derived from the logical call identity, not the attempt number. The agent must cache the result of a successfully executed call and return the cached result on subsequent attempts with the same key, preventing the downstream service from ever seeing the call twice.

## Solution 1: Tool Idempotency Policy

```python
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class IdempotencyClass(str, Enum):
    SAFE = "safe"                   # GET-like; no side effects; always re-execute
    IDEMPOTENT = "idempotent"       # PUT-like; safe to re-execute; dedup for efficiency
    NON_IDEMPOTENT = "non_idempotent"  # POST-like; must deduplicate strictly


@dataclass
class ToolIdempotencyPolicy:
    tool_name: str
    idempotency_class: IdempotencyClass
    key_fields: list               # argument fields that form the idempotency key
    result_ttl_seconds: float = 300.0   # how long to cache the result
    key_fn: Optional[Callable] = None   # custom key derivation function
```

## Solution 2: Idempotency Key Generator

```python
import hashlib
import json
from typing import Any, Dict, List, Optional


class IdempotencyKeyGenerator:
    """
    Derives a stable idempotency key from a tool call's name and
    a specified subset of its arguments. The key is the same across
    all retry attempts for the same logical call.
    """

    def generate(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        key_fields: Optional[List[str]] = None,
        session_id: str = "",
    ) -> str:
        if key_fields:
            subset = {k: arguments[k] for k in key_fields if k in arguments}
        else:
            subset = arguments

        payload = {
            "tool": tool_name,
            "session": session_id,
            "args": subset,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:24]

    def from_policy(
        self,
        policy: ToolIdempotencyPolicy,
        arguments: Dict[str, Any],
        session_id: str = "",
    ) -> str:
        if policy.key_fn:
            return policy.key_fn(arguments, session_id)
        return self.generate(
            tool_name=policy.tool_name,
            arguments=arguments,
            key_fields=policy.key_fields,
            session_id=session_id,
        )
```

## Solution 3: Idempotency Result Cache

```python
import time
from threading import Lock
from typing import Any, Dict, Optional


class IdempotencyResultCache:
    """
    Stores the result of a completed tool call keyed by idempotency key.
    Returns the cached result for duplicate calls within the TTL window,
    preventing re-execution of non-idempotent operations.
    """

    def __init__(self, max_entries: int = 50000):
        self._max = max_entries
        self._cache: Dict[str, dict] = {}
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.time() > entry["expires_at"]:
                del self._cache[key]
                return None
            return entry["result"]

    def set(self, key: str, result: Any, ttl_seconds: float) -> None:
        with self._lock:
            if len(self._cache) >= self._max:
                self._evict_oldest()
            self._cache[key] = {
                "result": result,
                "stored_at": time.time(),
                "expires_at": time.time() + ttl_seconds,
            }

    def _evict_oldest(self) -> None:
        if not self._cache:
            return
        oldest_key = min(self._cache, key=lambda k: self._cache[k]["stored_at"])
        del self._cache[oldest_key]

    def contains(self, key: str) -> bool:
        return self.get(key) is not None

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            active = sum(1 for e in self._cache.values() if e["expires_at"] > now)
            return {"total_entries": len(self._cache), "active_entries": active}
```

## Solution 4: Idempotent Tool Call Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class IdempotentToolCallExecutor:
    """
    Wraps a tool dispatch function with idempotency guarantees.
    Non-idempotent calls are deduplicated using the result cache.
    Safe calls are always re-executed. In-flight deduplication
    prevents concurrent retries from both executing simultaneously.
    """

    def __init__(
        self,
        cache: IdempotencyResultCache,
        key_generator: IdempotencyKeyGenerator,
    ):
        self._cache = cache
        self._key_gen = key_generator
        self._in_flight: Dict[str, asyncio.Lock] = {}
        self._dedup_hits = 0
        self._executions = 0

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        dispatch_fn: Callable,
        policy: ToolIdempotencyPolicy,
        session_id: str = "",
    ) -> dict:
        if policy.idempotency_class == IdempotencyClass.SAFE:
            self._executions += 1
            result = await dispatch_fn(tool_name, arguments)
            return {"result": result, "deduplicated": False, "idempotency_key": None}

        key = self._key_gen.from_policy(policy, arguments, session_id)

        # Check cache before acquiring lock
        cached = self._cache.get(key)
        if cached is not None:
            self._dedup_hits += 1
            return {"result": cached, "deduplicated": True, "idempotency_key": key}

        # Serialize concurrent attempts with the same key
        if key not in self._in_flight:
            self._in_flight[key] = asyncio.Lock()
        async with self._in_flight[key]:
            # Re-check after acquiring lock
            cached = self._cache.get(key)
            if cached is not None:
                self._dedup_hits += 1
                return {"result": cached, "deduplicated": True, "idempotency_key": key}

            self._executions += 1
            result = await dispatch_fn(tool_name, arguments)
            self._cache.set(key, result, policy.result_ttl_seconds)
            return {"result": result, "deduplicated": False, "idempotency_key": key}

    def stats(self) -> dict:
        total = self._executions + self._dedup_hits
        return {
            "total_calls": total,
            "executions": self._executions,
            "dedup_hits": self._dedup_hits,
            "dedup_rate": round(self._dedup_hits / max(total, 1), 4),
        }
```

## Solution 5: Idempotency Policy Registry

```python
from typing import Dict, Optional


class IdempotencyPolicyRegistry:
    """
    Manages per-tool idempotency policies. Returns a SAFE policy
    for unknown tools (fail-open) so the executor always has a policy.
    """

    def __init__(self):
        self._policies: Dict[str, ToolIdempotencyPolicy] = {}
        self._default = ToolIdempotencyPolicy(
            tool_name="__default__",
            idempotency_class=IdempotencyClass.SAFE,
            key_fields=[],
        )

    def register(self, policy: ToolIdempotencyPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolIdempotencyPolicy:
        return self._policies.get(tool_name, self._default)

    def register_many(self, policies: list) -> None:
        for p in policies:
            self.register(p)

    def all_policies(self) -> Dict[str, str]:
        return {name: p.idempotency_class.value for name, p in self._policies.items()}
```

## Solution 6: Idempotency Audit Logger

```python
import time
from typing import List


class IdempotencyAuditLogger:
    """
    Records idempotency decisions — executions and dedup hits —
    for audit trails and debugging duplicate-prevention behavior.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        idempotency_key: Optional[str],
        deduplicated: bool,
        session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "key": idempotency_key,
            "deduplicated": deduplicated,
            "session_id": session_id,
        })

    def dedup_events(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [r for r in self._records if r["ts"] >= cutoff and r["deduplicated"]]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        deduped = [r for r in recent if r["deduplicated"]]
        return {
            "window_seconds": window_seconds,
            "total_calls": len(recent),
            "dedup_hits": len(deduped),
            "dedup_rate": round(len(deduped) / max(len(recent), 1), 4),
        }
```

## Comparison

| Approach | Key Generation | Result Caching | In-Flight Dedup | Policy Registry | Audit |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (hash of fields) | No | No | No | No |
| IdempotencyResultCache | No | Yes (TTL) | No | No | No |
| IdempotentToolCallExecutor | Via generator | Via cache | Yes (asyncio.Lock) | No | No |
| IdempotencyPolicyRegistry | No | No | No | Yes | No |
| IdempotencyAuditLogger | No | No | No | No | Yes |

**Best for production**: Classify every write tool as `NON_IDEMPOTENT` by default and enumerate `key_fields` from the arguments that uniquely identify the logical operation — for a send-email tool, that is `(recipient, subject, template_id)`. Set `result_ttl_seconds` equal to the maximum retry window (e.g., 300s for a 5-minute retry budget) so cached results are available for all retries but do not accumulate indefinitely. The in-flight lock prevents thundering-herd retries from all executing simultaneously — critical when the LLM issues parallel tool calls and a transient failure causes all of them to retry at once.
