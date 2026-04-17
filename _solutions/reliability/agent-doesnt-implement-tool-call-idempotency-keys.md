---
title: "Agent Doesn't Implement Tool Call Idempotency Keys"
description: "Agents that retry tool calls without idempotency keys cause duplicate side effects: a payment tool retried after a network timeout charges the user twice, a record creation tool retried after an ambiguous response creates duplicate entries. Implement idempotency keys that allow tools to detect and return the result of a previous identical call without re-executing the side effect."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-tool-call-idempotency-keys
tags: [idempotency, retry-safety, duplicate-prevention, side-effects, tool-reliability, payment-safety]
symptoms:
  - "Retried tool calls cause duplicate records, payments, or notifications"
  - "Network timeout followed by retry results in double execution of the tool"
  - "No mechanism for tools to detect that a call was already processed"
  - "Idempotency is left entirely to each tool implementation with no framework support"
  - "Agent cannot determine if a previous call succeeded but the response was lost"
---

## Why This Happens

Tool calls with side effects (write to database, charge payment, send notification) are not safe to retry without idempotency controls. When a network timeout occurs, the agent does not know if the tool completed — it may have executed successfully and the response was lost, or it may not have executed at all. Without an idempotency key, retrying the call is a gamble: if the tool did complete, the retry causes a duplicate. Idempotency keys allow the tool (or a caching layer between agent and tool) to detect "I already processed this exact call" and return the stored result instead of re-executing.

## Solution 1: Idempotency Key Generator

```python
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class IdempotencyKey:
    key: str
    tool_name: str
    args_hash: str
    conversation_id: str
    created_at: float
    ttl_seconds: int = 86400   # keys expire after 24 hours

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds


class IdempotencyKeyGenerator:
    """
    Generates stable, deterministic idempotency keys for tool calls.
    Keys are based on tool name, normalized args, and conversation ID.
    A random nonce can be added for calls where args alone are not unique.
    """

    @staticmethod
    def _normalize_args(args: Dict[str, Any]) -> str:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def generate(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        conversation_id: str = "",
        nonce: Optional[str] = None,
    ) -> IdempotencyKey:
        args_str = cls._normalize_args(args)
        args_hash = hashlib.sha256(args_str.encode()).hexdigest()[:16]
        raw = f"{tool_name}:{args_hash}:{conversation_id}"
        if nonce:
            raw += f":{nonce}"
        key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        return IdempotencyKey(
            key=key,
            tool_name=tool_name,
            args_hash=args_hash,
            conversation_id=conversation_id,
            created_at=time.time(),
        )

    @classmethod
    def generate_with_nonce(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        conversation_id: str = "",
    ) -> IdempotencyKey:
        """Use when same args may be intentionally called multiple times."""
        return cls.generate(tool_name, args, conversation_id, nonce=os.urandom(8).hex())
```

## Solution 2: Idempotency Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional


class IdempotencyStore:
    """
    Stores results of completed tool calls keyed by idempotency key.
    A cache hit means the tool already ran — return the stored result.
    """

    def __init__(self, path: str = "/tmp/agent_idempotency.json", max_entries: int = 100_000):
        self._path = Path(path)
        self._max = max_entries
        self._lock = Lock()
        self._store: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._store = json.loads(self._path.read_text())
            except Exception:
                self._store = {}

    def _flush(self) -> None:
        self._path.write_text(json.dumps(self._store, indent=2))

    def get(self, ikey: IdempotencyKey) -> Optional[dict]:
        with self._lock:
            entry = self._store.get(ikey.key)
            if entry is None:
                return None
            if time.time() > entry.get("expires_at", float("inf")):
                del self._store[ikey.key]
                return None
            return entry

    def store_result(self, ikey: IdempotencyKey, result: Any, error: Optional[str] = None) -> None:
        with self._lock:
            if len(self._store) >= self._max:
                # Evict oldest entry
                oldest_key = min(self._store, key=lambda k: self._store[k].get("stored_at", 0))
                del self._store[oldest_key]
            self._store[ikey.key] = {
                "key": ikey.key,
                "tool_name": ikey.tool_name,
                "result": result,
                "error": error,
                "stored_at": time.time(),
                "expires_at": time.time() + ikey.ttl_seconds,
                "is_error": error is not None,
            }
            self._flush()

    def is_pending(self, ikey: IdempotencyKey) -> bool:
        with self._lock:
            entry = self._store.get(ikey.key)
            return entry is not None and entry.get("status") == "pending"

    def mark_pending(self, ikey: IdempotencyKey) -> None:
        with self._lock:
            self._store[ikey.key] = {
                "key": ikey.key,
                "tool_name": ikey.tool_name,
                "status": "pending",
                "started_at": time.time(),
                "expires_at": time.time() + ikey.ttl_seconds,
            }

    def size(self) -> int:
        with self._lock:
            return len(self._store)
```

## Solution 3: Idempotent Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class IdempotentToolExecutor:
    """
    Wraps tool execution with idempotency key lookup.
    On cache hit: returns stored result without calling the tool.
    On cache miss: calls the tool, stores the result, returns it.
    On pending (concurrent duplicate): waits for the first call to complete.
    """

    def __init__(
        self,
        store: IdempotencyStore,
        generator: IdempotencyKeyGenerator,
        pending_wait_seconds: float = 30.0,
        pending_poll_interval: float = 0.5,
    ):
        self._store = store
        self._generator = generator
        self._pending_wait = pending_wait_seconds
        self._poll_interval = pending_poll_interval
        self._cache_hits = 0
        self._cache_misses = 0
        self._pending_waits = 0

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        fn: Callable,
        conversation_id: str = "",
        force_new: bool = False,   # bypass idempotency for intentional re-runs
    ) -> dict:
        if force_new:
            ikey = IdempotencyKeyGenerator.generate_with_nonce(tool_name, args, conversation_id)
        else:
            ikey = self._generator.generate(tool_name, args, conversation_id)

        # Check for existing result
        cached = self._store.get(ikey)
        if cached and cached.get("status") != "pending":
            self._cache_hits += 1
            if cached.get("is_error"):
                raise IdempotentReplayError(tool_name, ikey.key, cached.get("error", ""))
            return {"result": cached["result"], "idempotency_key": ikey.key, "replayed": True}

        # Wait if another call is pending with same key
        if self._store.is_pending(ikey):
            self._pending_waits += 1
            deadline = time.time() + self._pending_wait
            while time.time() < deadline:
                await asyncio.sleep(self._poll_interval)
                cached = self._store.get(ikey)
                if cached and cached.get("status") != "pending":
                    return {"result": cached["result"], "idempotency_key": ikey.key, "replayed": True}
            raise IdempotencyPendingTimeoutError(tool_name, ikey.key)

        # Execute and store result
        self._store.mark_pending(ikey)
        self._cache_misses += 1
        try:
            result = await fn(**args)
            self._store.store_result(ikey, result)
            return {"result": result, "idempotency_key": ikey.key, "replayed": False}
        except Exception as exc:
            self._store.store_result(ikey, None, error=str(exc))
            raise

    def stats(self) -> dict:
        total = self._cache_hits + self._cache_misses
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "pending_waits": self._pending_waits,
            "hit_rate": round(self._cache_hits / max(total, 1), 4),
            "store_size": self._store.size(),
        }


class IdempotentReplayError(Exception):
    def __init__(self, tool_name: str, key: str, original_error: str):
        super().__init__(f"replaying stored error for tool '{tool_name}' (key={key[:8]}...): {original_error}")


class IdempotencyPendingTimeoutError(Exception):
    def __init__(self, tool_name: str, key: str):
        super().__init__(f"timed out waiting for pending idempotent call to tool '{tool_name}' (key={key[:8]}...)")
```

## Solution 4: Per-Tool Idempotency Policy

```python
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class IdempotencyPolicy(str, Enum):
    REQUIRED = "required"       # always use idempotency keys
    OPTIONAL = "optional"       # use keys only if caller provides them
    DISABLED = "disabled"       # never use keys (read-only tools)
    FORCE_NEW = "force_new"     # always generate new nonce-based key


@dataclass
class ToolIdempotencyConfig:
    policy: IdempotencyPolicy = IdempotencyPolicy.REQUIRED
    ttl_seconds: int = 86400
    store_errors: bool = True   # whether to cache error results too


class ToolIdempotencyPolicyRegistry:
    """
    Defines per-tool idempotency policies.
    Read-only tools (search, lookup) use DISABLED.
    Write tools (create, charge, send) use REQUIRED.
    """

    def __init__(self):
        self._policies: Dict[str, ToolIdempotencyConfig] = {}
        self._default = ToolIdempotencyConfig(policy=IdempotencyPolicy.REQUIRED)

    def register(self, tool_name: str, config: ToolIdempotencyConfig) -> None:
        self._policies[tool_name] = config

    def get(self, tool_name: str) -> ToolIdempotencyConfig:
        return self._policies.get(tool_name, self._default)

    def is_idempotency_required(self, tool_name: str) -> bool:
        policy = self.get(tool_name).policy
        return policy in (IdempotencyPolicy.REQUIRED, IdempotencyPolicy.FORCE_NEW)
```

## Solution 5: Idempotency Key Expiry Cleaner

```python
import time
from threading import Lock


class IdempotencyKeyExpiryCleaner:
    """
    Removes expired idempotency keys from the store on a schedule.
    Prevents unbounded store growth over long agent uptimes.
    """

    def __init__(self, store: IdempotencyStore):
        self._store = store
        self._cleaned_count = 0

    def clean(self) -> int:
        now = time.time()
        with self._store._lock:
            expired = [
                k for k, v in self._store._store.items()
                if now > v.get("expires_at", float("inf"))
            ]
            for k in expired:
                del self._store._store[k]
            if expired:
                self._store._flush()
        self._cleaned_count += len(expired)
        return len(expired)

    def stats(self) -> dict:
        return {
            "total_cleaned": self._cleaned_count,
            "current_store_size": self._store.size(),
        }
```

## Solution 6: Idempotency Dashboard

```python
import time


class IdempotencyDashboard:
    """
    Combines executor stats, store size, and policy registry
    into a single operational view.
    """

    def __init__(
        self,
        executor: IdempotentToolExecutor,
        cleaner: IdempotencyKeyExpiryCleaner,
        policy_registry: ToolIdempotencyPolicyRegistry,
    ):
        self._executor = executor
        self._cleaner = cleaner
        self._policies = policy_registry

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "executor_stats": self._executor.stats(),
            "cleaner_stats": self._cleaner.stats(),
            "tools_with_idempotency_required": [
                name for name in self._policies._policies
                if self._policies.is_idempotency_required(name)
            ],
        }
```

## Comparison

| Approach | Key Generation | Result Caching | Concurrent Dedup | Policy Per-Tool | Expiry Cleanup |
|---|---|---|---|---|---|
| IdempotencyKeyGenerator | Yes (deterministic) | No | No | No | No |
| IdempotencyStore | No | Yes (persistent) | Via pending flag | No | No |
| IdempotentToolExecutor | Via generator | Via store | Yes (poll wait) | No | No |
| ToolIdempotencyPolicyRegistry | No | No | No | Yes | No |
| IdempotencyKeyExpiryCleaner | No | No | No | No | Yes |
| IdempotencyDashboard | No | No | No | No | Yes (aggregated) |

**Best for production**: Use `IdempotencyPolicy.REQUIRED` for all tools that write, send, or charge — and `DISABLED` for pure read tools. Set `ttl_seconds=3600` for most write tools: idempotency only needs to cover the retry window, not forever. Use Redis with atomic SET NX (set if not exists) instead of the file-based store for multi-instance deployments — the file store has a race condition if two instances check the key simultaneously before either marks it pending. Monitor `hit_rate` in the dashboard: a hit rate above 5% means the agent is producing many duplicate calls, which may indicate retry logic that is too aggressive or a prompt that loops incorrectly.
