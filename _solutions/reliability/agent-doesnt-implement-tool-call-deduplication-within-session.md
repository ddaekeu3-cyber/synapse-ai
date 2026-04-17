---
title: "Agent Doesn't Implement Tool Call Deduplication Within Session"
description: "Agents that allow the LLM to call the same tool with the same arguments multiple times within a session waste API quota, inflate costs, and introduce inconsistency when a tool's result changes between identical calls. Implement within-session tool call deduplication that caches results keyed on tool name and argument hash, returns the cached result for duplicate calls, and provides cache invalidation for tools whose results may change mid-session."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-call-deduplication-within-session
tags: [deduplication, tool-calls, session-cache, api-quota, consistency, within-session-caching]
symptoms:
  - "The same web search query executed three times in one session — same API call, same result"
  - "Embedding tool called twice with the same text, paying twice for the same computation"
  - "Database lookup tool called multiple times for the same record ID in a single conversation"
  - "LLM uses inconsistent facts because the same lookup returned different results at different times"
  - "No session-scoped cache for tool results — every call goes to the external service"
---

## Why This Happens

LLMs sometimes repeat tool calls they have already made, either because they forgot the earlier result in a long context or because the model reasons that it should re-verify. Without a session-scoped cache, every call hits the external API — wasting quota and potentially returning inconsistent results (if the underlying data changed between calls). Within-session deduplication stores the result of the first call and returns it for any subsequent call with the same tool name and arguments. This enforces result consistency within a session while also saving API calls. Tools that are explicitly marked as non-cacheable (real-time data, random generators) are exempt.

## Solution 1: Session Tool Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SessionToolCacheEntry:
    tool_name: str
    args_hash: str
    result: Any
    cached_at: float = field(default_factory=time.time)
    hit_count: int = 0
    ttl_seconds: Optional[float] = None   # None = session lifetime

    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return time.time() - self.cached_at > self.ttl_seconds
```

## Solution 2: Tool Call Hasher

```python
import hashlib
import json
from typing import Any, Dict


class ToolCallHasher:
    """
    Generates a stable cache key from tool name and arguments.
    Handles nested structures and normalizes argument ordering.
    """

    def hash(self, tool_name: str, args: Dict[str, Any]) -> str:
        canonical = json.dumps(
            {"tool": tool_name, "args": args},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]
```

## Solution 3: Session Tool Call Cache

```python
import time
from threading import Lock
from typing import Any, Dict, Optional, Set


class SessionToolCallCache:
    """
    Session-scoped cache for tool call results.
    Keyed on (tool_name, args_hash). Supports per-tool TTLs and
    explicit non-cacheable tool registration.
    """

    def __init__(
        self,
        max_entries: int = 500,
        default_ttl_seconds: Optional[float] = None,
    ):
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._cache: Dict[str, SessionToolCacheEntry] = {}
        self._non_cacheable: Set[str] = set()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def mark_non_cacheable(self, tool_name: str) -> None:
        self._non_cacheable.add(tool_name)

    def get(self, cache_key: str, tool_name: str) -> Optional[Any]:
        if tool_name in self._non_cacheable:
            return None
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired():
                del self._cache[cache_key]
                self._misses += 1
                return None
            entry.hit_count += 1
            self._hits += 1
            return entry.result

    def put(
        self,
        cache_key: str,
        tool_name: str,
        result: Any,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        if tool_name in self._non_cacheable:
            return
        with self._lock:
            if len(self._cache) >= self._max:
                # Evict oldest
                oldest_key = min(
                    self._cache, key=lambda k: self._cache[k].cached_at
                )
                del self._cache[oldest_key]
            self._cache[cache_key] = SessionToolCacheEntry(
                tool_name=tool_name,
                args_hash=cache_key,
                result=result,
                ttl_seconds=ttl_seconds or self._default_ttl,
            )

    def invalidate(self, tool_name: str) -> int:
        """Invalidate all cached entries for a given tool. Returns count removed."""
        with self._lock:
            keys_to_remove = [
                k for k, v in self._cache.items() if v.tool_name == tool_name
            ]
            for k in keys_to_remove:
                del self._cache[k]
        return len(keys_to_remove)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            entries = len(self._cache)
        return {
            "entries": entries,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 4: Deduplicating Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class DeduplicatingToolDispatcher:
    """
    Intercepts tool calls, checks the session cache before execution,
    and stores results after execution.
    """

    def __init__(
        self,
        cache: SessionToolCallCache,
        hasher: ToolCallHasher,
        tool_executors: Dict[str, Callable],
        per_tool_ttl: Dict[str, Optional[float]] = None,
    ):
        self._cache = cache
        self._hasher = hasher
        self._executors = tool_executors
        self._per_tool_ttl = per_tool_ttl or {}
        self._total_calls = 0
        self._deduplicated_calls = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> dict:
        self._total_calls += 1
        cache_key = self._hasher.hash(tool_name, args)

        # Cache hit
        cached = self._cache.get(cache_key, tool_name)
        if cached is not None:
            self._deduplicated_calls += 1
            return {
                "result": cached,
                "cache_hit": True,
                "cache_key": cache_key,
                "tool_name": tool_name,
            }

        # Cache miss: execute
        executor = self._executors.get(tool_name)
        if executor is None:
            return {
                "result": None,
                "cache_hit": False,
                "error": f"tool '{tool_name}' not registered",
            }

        start = time.time()
        try:
            result = await executor(**args)
            latency_ms = (time.time() - start) * 1000

            ttl = self._per_tool_ttl.get(tool_name)
            self._cache.put(cache_key, tool_name, result, ttl_seconds=ttl)

            return {
                "result": result,
                "cache_hit": False,
                "cache_key": cache_key,
                "latency_ms": round(latency_ms, 2),
            }
        except Exception as exc:
            return {
                "result": None,
                "cache_hit": False,
                "error": str(exc),
            }

    def deduplication_rate(self) -> float:
        return round(self._deduplicated_calls / max(self._total_calls, 1), 4)

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "deduplicated": self._deduplicated_calls,
            "deduplication_rate": self.deduplication_rate(),
            "cache": self._cache.stats(),
        }
```

## Solution 5: Cache Invalidation Trigger

```python
from typing import List


class ToolCacheInvalidationTrigger:
    """
    Invalidates cached results when a tool call causes a state change
    that makes other cached results stale.
    For example, updating a record should invalidate cached reads of that record.
    """

    def __init__(
        self,
        cache: SessionToolCallCache,
        invalidation_map: dict = None,
    ):
        self._cache = cache
        # write_tool -> [read_tools to invalidate]
        self._map = invalidation_map or {}

    def register_invalidation(self, write_tool: str, tools_to_invalidate: List[str]) -> None:
        self._map[write_tool] = tools_to_invalidate

    def on_tool_executed(self, tool_name: str) -> List[str]:
        """Call after a tool executes. Returns list of tools that were invalidated."""
        targets = self._map.get(tool_name, [])
        invalidated = []
        for target in targets:
            count = self._cache.invalidate(target)
            if count > 0:
                invalidated.append(target)
        return invalidated
```

## Solution 6: Deduplication Dashboard

```python
import time


class ToolDeduplicationDashboard:
    """
    Reports deduplication savings for cost and quota monitoring.
    """

    def __init__(self, dispatcher: DeduplicatingToolDispatcher):
        self._dispatcher = dispatcher

    def render(self) -> dict:
        stats = self._dispatcher.stats()
        return {
            "generated_at": time.time(),
            "deduplication_stats": stats,
            "api_calls_saved": stats["deduplicated"],
        }
```

## Comparison

| Approach | Session Cache | Non-Cacheable Exemption | Cache Invalidation | Dispatcher Integration | Dashboard |
|---|---|---|---|---|---|
| SessionToolCallCache | Yes (TTL + LRU) | Yes (mark_non_cacheable) | Yes (by tool name) | No | No |
| ToolCallHasher | No | No | No | No | No |
| DeduplicatingToolDispatcher | Via cache | Via cache | No | Yes | Stats |
| ToolCacheInvalidationTrigger | Via cache | No | Yes (write→read) | No | No |
| ToolDeduplicationDashboard | No | No | No | No | Yes |

**Best for production**: Register real-time tools (stock prices, random number generators, live sensor data) as non-cacheable via `mark_non_cacheable()` — these tools must always execute fresh. Set `default_ttl_seconds=None` (session lifetime) for lookup tools that return stable data (user profiles, product catalogs) and a short TTL (60–300s) for tools that return slowly-changing data (inventory counts, trending topics). Use `ToolCacheInvalidationTrigger` to invalidate cached reads when write operations occur — if `update_user_profile` is called, invalidate the cache for `get_user_profile`. Monitor `deduplication_rate` per tool: above 30% for a search tool indicates the model is looping and rerunning the same query, which is a reasoning quality issue worth addressing in the system prompt.
