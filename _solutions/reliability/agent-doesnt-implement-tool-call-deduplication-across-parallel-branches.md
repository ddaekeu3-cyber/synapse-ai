---
title: "Agent Doesn't Implement Tool Call Deduplication Across Parallel Branches"
description: "Agents that fan out multiple parallel reasoning branches — each independently deciding to call the same tool — trigger redundant upstream calls when branches converge. Two branches that both fetch the same user profile, lookup the same price, or query the same record produce N identical API calls instead of one. Implement cross-branch tool call deduplication that detects identical in-flight or completed calls and returns shared results to all waiting branches."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-tool-call-deduplication-across-parallel-branches
tags: [deduplication, parallel-branches, tool-call-sharing, fan-out, redundant-calls, branch-coordination]
symptoms:
  - "Parallel reasoning branches each call the same tool independently — N calls for N branches"
  - "Same API endpoint hit 3x simultaneously because 3 branches independently decided to fetch the same data"
  - "No coordination between branches dispatching tool calls for the same logical query"
  - "Upstream rate limits triggered by burst of identical calls from branch fan-out"
  - "Tool result from branch A is re-fetched by branch B even though branch A already has it"
---

## Why This Happens

Parallel agent branches — spawned by fan-out for multi-perspective reasoning, parallel search, or independent subtask execution — make tool call decisions independently. Each branch decides it needs the current EUR/USD rate, fetches it, and all three fetch calls go out concurrently. The root cause is that branches share no call state. Cross-branch deduplication requires a shared registry that tracks in-flight and recently completed calls by a content-based key. When branch B requests a call that branch A already dispatched, B subscribes to A's future rather than dispatching a new request.

## Solution 1: Branch Call Registry Key

```python
import hashlib
import json
from typing import Any, Dict


class BranchCallRegistryKey:
    """
    Generates a stable, content-based key for a tool call
    that is consistent across all branches.
    """

    @staticmethod
    def generate(tool_name: str, args: Dict[str, Any]) -> str:
        canonical = json.dumps(
            {"tool": tool_name, "args": args}, sort_keys=True, default=str
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]
```

## Solution 2: Shared Call State

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SharedCallStatus(str, Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SharedCallState:
    key: str
    tool_name: str
    status: SharedCallStatus = SharedCallStatus.IN_FLIGHT
    future: Optional[asyncio.Future] = None
    result: Any = None
    error: Optional[Exception] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    subscriber_count: int = 1
    ttl_seconds: float = 60.0

    def is_expired(self) -> bool:
        if self.completed_at is None:
            return False
        return time.time() - self.completed_at > self.ttl_seconds
```

## Solution 3: Cross-Branch Call Registry

```python
import asyncio
import time
from typing import Dict, Optional


class CrossBranchCallRegistry:
    """
    Shared registry that tracks in-flight and recently completed tool calls
    across all parallel branches. Provides subscribe-or-dispatch semantics:
    the first caller for a key dispatches; subsequent callers subscribe.
    """

    def __init__(self, result_ttl_seconds: float = 30.0):
        self._states: Dict[str, SharedCallState] = {}
        self._lock = asyncio.Lock()
        self._ttl = result_ttl_seconds
        self._dispatches = 0
        self._deduped = 0

    async def get_or_register(
        self, key: str, tool_name: str
    ) -> tuple[SharedCallState, bool]:
        """
        Returns (state, is_new_dispatch).
        is_new_dispatch=True → caller should execute and resolve the future.
        is_new_dispatch=False → caller should await state.future.
        """
        async with self._lock:
            self._evict_expired()

            existing = self._states.get(key)
            if existing:
                if existing.status == SharedCallStatus.COMPLETED and not existing.is_expired():
                    existing.subscriber_count += 1
                    self._deduped += 1
                    return existing, False
                if existing.status == SharedCallStatus.IN_FLIGHT:
                    existing.subscriber_count += 1
                    self._deduped += 1
                    return existing, False

            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            state = SharedCallState(
                key=key,
                tool_name=tool_name,
                future=future,
                ttl_seconds=self._ttl,
            )
            self._states[key] = state
            self._dispatches += 1
            return state, True

    async def resolve(self, key: str, result) -> None:
        async with self._lock:
            state = self._states.get(key)
        if state and not state.future.done():
            state.status = SharedCallStatus.COMPLETED
            state.result = result
            state.completed_at = time.time()
            state.future.set_result(result)

    async def reject(self, key: str, exc: Exception) -> None:
        async with self._lock:
            state = self._states.get(key)
        if state and not state.future.done():
            state.status = SharedCallStatus.FAILED
            state.error = exc
            state.completed_at = time.time()
            state.future.set_exception(exc)

    def _evict_expired(self) -> None:
        expired = [k for k, s in self._states.items() if s.is_expired()]
        for k in expired:
            del self._states[k]

    def stats(self) -> dict:
        total = self._dispatches + self._deduped
        return {
            "unique_dispatches": self._dispatches,
            "deduplicated_calls": self._deduped,
            "total_call_attempts": total,
            "dedup_rate": round(self._deduped / max(total, 1), 4),
            "active_states": len(self._states),
        }
```

## Solution 4: Deduplicating Branch Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class DeduplicatingBranchDispatcher:
    """
    Wraps tool dispatch with cross-branch deduplication.
    Concurrent branches calling the same tool with identical args
    share one upstream call; all receive the result when it resolves.
    """

    def __init__(
        self,
        registry: CrossBranchCallRegistry,
        key_gen: BranchCallRegistryKey,
    ):
        self._registry = registry
        self._key_gen = key_gen

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        handler: Callable,
        branch_id: str = "",
    ) -> Any:
        key = self._key_gen.generate(tool_name, args)
        state, is_new = await self._registry.get_or_register(key, tool_name)

        if not is_new:
            # Return immediately if already completed
            if state.status == SharedCallState and state.result is not None:
                return state.result
            # Wait for the in-flight call to complete
            return await state.future

        # This branch is the designated dispatcher
        try:
            result = await handler(**args)
            await self._registry.resolve(key, result)
            return result
        except Exception as exc:
            await self._registry.reject(key, exc)
            raise
```

## Solution 5: Branch Result Cache

```python
import time
from typing import Any, Dict, Optional, Tuple


class BranchResultCache:
    """
    Short-lived cache for completed cross-branch call results.
    Prevents re-registration for the same key within a session
    when branches complete and new branches start.
    """

    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 500):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: Dict[str, Tuple[Any, float]] = {}
        self._hits = 0

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._store) >= self._max:
            oldest = min(self._store, key=lambda k: self._store[k][1])
            del self._store[oldest]
        self._store[key] = (value, time.time() + self._ttl)

    def stats(self) -> dict:
        return {"cache_hits": self._hits, "entries": len(self._store)}
```

## Solution 6: Branch Deduplication Dashboard

```python
import time


class BranchDeduplicationDashboard:
    """
    Combines registry stats and cache stats into an operational
    snapshot for cross-branch coordination efficiency.
    """

    def __init__(
        self,
        registry: CrossBranchCallRegistry,
        cache: BranchResultCache,
        dispatcher: DeduplicatingBranchDispatcher,
    ):
        self._registry = registry
        self._cache = cache

    def render(self) -> dict:
        reg_stats = self._registry.stats()
        cache_stats = self._cache.stats()
        return {
            "generated_at": time.time(),
            "registry": reg_stats,
            "cache": cache_stats,
            "efficiency": {
                "dedup_rate": reg_stats["dedup_rate"],
                "upstream_calls_avoided": reg_stats["deduplicated_calls"],
            },
        }
```

## Comparison

| Approach | In-Flight Dedup | Completed Result Reuse | Branch Subscribe | Short-TTL Cache | Dashboard |
|---|---|---|---|---|---|
| CrossBranchCallRegistry | Yes | Yes (TTL) | Yes (Future) | No | No |
| DeduplicatingBranchDispatcher | Via registry | Via registry | Via registry | No | No |
| BranchResultCache | No | Yes | No | Yes | No |
| BranchDeduplicationDashboard | No | No | No | No | Yes |

**Best for production**: Apply deduplication only to read-only, idempotent tool calls — never to calls that create records, send messages, or consume quotas. Set `result_ttl_seconds=30` to serve completed results to late-arriving branches without hitting upstream again, while ensuring stale results are not served to genuinely new requests. Monitor `dedup_rate`: a rate above 0.40 in a parallel fan-out workflow indicates significant overlap between branches and is a strong signal to consolidate the parallel calls into a shared prefetch before the fan-out rather than deduplicating after. The registry's `subscriber_count` per state shows how many branches waited — counts above 5 suggest over-aggressive fan-out for that particular tool call.
