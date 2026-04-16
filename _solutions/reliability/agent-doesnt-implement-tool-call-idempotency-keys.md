---
title: "Agent Doesn't Implement Tool Call Idempotency Keys"
description: "Agents that retry failed tool calls without idempotency keys risk executing state-changing operations multiple times: a payment processed twice, a message sent twice, a record inserted twice — because the first call succeeded but the response was lost in transit. Implement idempotency keys that are generated per tool invocation, passed to the tool on every attempt, and used by the tool (or an idempotency layer) to deduplicate repeated calls with identical keys."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-tool-call-idempotency-keys
tags: [idempotency, tool-retry, duplicate-prevention, state-change-safety, exactly-once, retry-safety]
symptoms:
  - "Payment or write operations execute twice when a retry follows a network timeout"
  - "Duplicate database records appear after a tool call retry during a transient failure"
  - "Messages or notifications are sent multiple times from a single user action"
  - "No idempotency key is generated or passed on tool calls that modify state"
  - "Cannot distinguish between a first attempt and a retry at the tool execution layer"
---

## Why This Happens

Network calls are not atomic from the caller's perspective. A tool call can succeed on the server side while the success response is lost in transit — from the agent's view, the call timed out and needs a retry. Without an idempotency key, the retry is a new invocation that the tool processes independently, potentially applying the state change a second time. Idempotency keys bind a logical operation to a unique identifier: the tool execution layer stores the result of the first successful call and returns the cached result for any subsequent call with the same key, guaranteeing that the operation is applied exactly once regardless of how many times the agent retries.

## Solution 1: Idempotency Key Generator

```python
import hashlib
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class KeyScope(str, Enum):
    SESSION = "session"         # unique per session (same session = same key)
    REQUEST = "request"         # unique per request attempt
    CONTENT = "content"         # derived from tool name + args (deterministic)


@dataclass
class IdempotencyKey:
    key: str
    tool_name: str
    scope: KeyScope
    created_at: float
    session_id: str = ""
    attempt: int = 1


class IdempotencyKeyGenerator:
    """
    Generates idempotency keys for tool invocations.
    CONTENT scope produces the same key for identical (tool, args) pairs
    within a session, enabling safe retries for deterministic tools.
    REQUEST scope produces a fresh key per attempt for non-deterministic tools.
    """

    @staticmethod
    def generate(
        tool_name: str,
        args: Dict[str, Any],
        session_id: str = "",
        scope: KeyScope = KeyScope.CONTENT,
        attempt: int = 1,
    ) -> IdempotencyKey:
        if scope == KeyScope.CONTENT:
            import json
            payload = f"{session_id}:{tool_name}:{json.dumps(args, sort_keys=True)}"
            key = "idem-" + hashlib.sha256(payload.encode()).hexdigest()[:24]
        elif scope == KeyScope.SESSION:
            key = f"idem-{session_id}-{tool_name}-{str(uuid.uuid4())[:8]}"
        else:  # REQUEST
            key = f"idem-{str(uuid.uuid4())}"

        return IdempotencyKey(
            key=key,
            tool_name=tool_name,
            scope=scope,
            created_at=time.time(),
            session_id=session_id,
            attempt=attempt,
        )
```

## Solution 2: Idempotency Store

```python
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class IdempotencyStore:
    """
    Stores the result of completed tool calls keyed by idempotency key.
    On repeated calls with the same key, returns the cached result
    without re-executing the tool.
    """

    def __init__(
        self,
        ttl_seconds: float = 86400.0,   # 24h default retention
        path: Optional[str] = None,
    ):
        self._ttl = ttl_seconds
        self._store: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        if self._path:
            self._load()

    def exists(self, key: str) -> bool:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            if time.time() > entry["expires_at"]:
                del self._store[key]
                return False
            return True

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None or time.time() > entry["expires_at"]:
                return None
            return entry["result"]

    def put(self, key: str, result: Any, tool_name: str = "") -> None:
        with self._lock:
            self._store[key] = {
                "result": result,
                "tool_name": tool_name,
                "stored_at": time.time(),
                "expires_at": time.time() + self._ttl,
            }
            if self._path:
                self._persist()

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps(self._store, indent=2, default=str))
        except OSError:
            pass

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            self._store = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
```

## Solution 3: Idempotent Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class IdempotentToolExecutor:
    """
    Wraps tool execution with idempotency. If a result for the given
    key already exists in the store, returns it immediately without
    calling the tool function again.
    """

    def __init__(self, store: IdempotencyStore):
        self._store = store
        self._cache_hits = 0
        self._cache_misses = 0

    async def execute(
        self,
        key: IdempotencyKey,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        if self._store.exists(key.key):
            self._cache_hits += 1
            cached = self._store.get(key.key)
            return {
                "result": cached,
                "idempotency_key": key.key,
                "cache_hit": True,
                "attempt": key.attempt,
            }

        self._cache_misses += 1
        start = time.time()
        try:
            result = await tool_fn(*args, **kwargs)
            latency_ms = round((time.time() - start) * 1000, 2)
            self._store.put(key.key, result, key.tool_name)
            return {
                "result": result,
                "idempotency_key": key.key,
                "cache_hit": False,
                "latency_ms": latency_ms,
                "attempt": key.attempt,
            }
        except Exception as exc:
            # Do not cache failures — allow retry
            raise IdempotencyExecutionError(key.key, key.tool_name, str(exc)) from exc

    def stats(self) -> dict:
        total = self._cache_hits + self._cache_misses
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": round(self._cache_hits / max(total, 1), 4),
        }


class IdempotencyExecutionError(Exception):
    def __init__(self, key: str, tool_name: str, reason: str):
        super().__init__(f"tool '{tool_name}' failed (key={key}): {reason}")
        self.key = key
        self.tool_name = tool_name
        self.reason = reason
```

## Solution 4: Retry-Safe Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class RetrySafeToolDispatcher:
    """
    Dispatches tool calls with automatic retry and idempotency.
    Generates a CONTENT-scoped key on the first attempt and reuses it
    on all retries, ensuring exactly-once semantics.
    """

    def __init__(
        self,
        executor: IdempotentToolExecutor,
        key_generator: IdempotencyKeyGenerator,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
    ):
        self._executor = executor
        self._keygen = key_generator
        self._max_retries = max_retries
        self._backoff = backoff_base_seconds

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        session_id: str = "",
        scope: KeyScope = KeyScope.CONTENT,
    ) -> Any:
        key = self._keygen.generate(
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            scope=scope,
            attempt=1,
        )

        last_error = None
        for attempt in range(1, self._max_retries + 2):
            key.attempt = attempt
            try:
                outcome = await self._executor.execute(key, tool_fn, **args)
                return outcome["result"]
            except IdempotencyExecutionError as exc:
                last_error = exc
                if attempt <= self._max_retries:
                    delay = self._backoff * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        raise last_error
```

## Solution 5: Idempotency Audit Log

```python
import time
from collections import Counter
from threading import Lock
from typing import List


class IdempotencyAuditLog:
    """
    Records idempotency cache hits and misses for audit and debugging.
    Surfaces which tools have the highest retry rates.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._lock = Lock()
        self._max = max_records

    def record(self, key: IdempotencyKey, cache_hit: bool) -> None:
        with self._lock:
            if len(self._records) >= self._max:
                self._records.pop(0)
            self._records.append({
                "ts": time.time(),
                "key": key.key,
                "tool_name": key.tool_name,
                "scope": key.scope.value,
                "attempt": key.attempt,
                "cache_hit": cache_hit,
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}

        hits = [r for r in recent if r["cache_hit"]]
        retries = [r for r in recent if r["attempt"] > 1]
        tool_retry_counts = Counter(r["tool_name"] for r in retries)

        return {
            "window_seconds": window_seconds,
            "calls": len(recent),
            "cache_hits": len(hits),
            "retries": len(retries),
            "retry_rate": round(len(retries) / max(len(recent), 1), 4),
            "top_retried_tools": tool_retry_counts.most_common(5),
        }
```

## Solution 6: Idempotency Dashboard

```python
import time


class IdempotencyDashboard:
    """
    Combines executor stats, store state, and audit log into a single view.
    """

    def __init__(
        self,
        executor: IdempotentToolExecutor,
        store: IdempotencyStore,
        audit_log: IdempotencyAuditLog,
    ):
        self._executor = executor
        self._store = store
        self._audit = audit_log

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "executor_stats": self._executor.stats(),
            "store_size": len(self._store._store),
            "audit": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Key Generation | Result Caching | Retry Integration | Audit | Dashboard |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (3 scopes) | No | No | No | No |
| IdempotencyStore | No | Yes (TTL-based) | No | No | No |
| IdempotentToolExecutor | No | Via store | No | No | No |
| RetrySafeToolDispatcher | Via generator | Via executor | Yes (backoff) | No | No |
| IdempotencyAuditLog | No | No | No | Yes | No |
| IdempotencyDashboard | No | No | No | No | Yes |

**Best for production**: Use `KeyScope.CONTENT` for all state-changing tools — this generates the same key for identical (tool, args, session) triples, making retries safe without any coordination overhead. Set `ttl_seconds=86400` (24 hours) to cover the longest reasonable session duration while preventing unbounded store growth. Alert when `retry_rate` in `IdempotencyAuditLog` exceeds 5% for a specific tool — this indicates that tool is experiencing frequent timeouts or transient failures and the upstream dependency should be investigated before the retry volume causes load amplification.
