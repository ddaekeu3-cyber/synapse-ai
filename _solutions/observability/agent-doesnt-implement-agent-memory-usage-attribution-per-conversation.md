---
title: "Agent Doesn't Implement Memory Usage Attribution Per Conversation"
description: "Agents that allocate memory for conversation state, tool result caches, and embedding stores without tracking allocation per conversation cannot identify which conversations are consuming disproportionate memory: a single runaway conversation with thousands of turns may consume gigabytes while hundreds of normal conversations use megabytes total. Implement per-conversation memory attribution that tracks allocations, surfaces outliers, and enforces per-conversation memory limits."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-memory-usage-attribution-per-conversation
tags: [memory-attribution, conversation-isolation, memory-limits, oom-prevention, memory-profiling, per-session-tracking]
symptoms:
  - "OOM kills occur but no data shows which conversation caused the spike"
  - "Memory grows monotonically with no way to attribute growth to specific sessions"
  - "No per-conversation memory limit — one runaway session can exhaust agent memory"
  - "Memory profiling tools show heap usage but not which conversation owns each object"
  - "Cannot evict the most memory-intensive conversations when memory pressure occurs"
---

## Why This Happens

Python's memory model does not natively attribute heap allocations to logical units like conversations. All allocations live in the same heap, and standard profilers report process-level usage. Per-conversation attribution requires explicit tracking: every time the agent stores something on behalf of a conversation — message history, tool results, embeddings, cached data — it must record the estimated size against the conversation ID. Without this discipline, memory growth is observable in aggregate but not attributable to individual sessions, making targeted eviction impossible.

## Solution 1: Memory Allocation Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class MemoryCategory(str, Enum):
    MESSAGE_HISTORY = "message_history"
    TOOL_RESULT_CACHE = "tool_result_cache"
    EMBEDDING_STORE = "embedding_store"
    RETRIEVED_DOCUMENTS = "retrieved_documents"
    AGENT_STATE = "agent_state"
    OTHER = "other"


@dataclass
class MemoryAllocationRecord:
    conversation_id: str
    category: MemoryCategory
    size_bytes: int
    label: str = ""
    allocated_at: float = field(default_factory=time.time)
    allocation_id: str = ""

    def __post_init__(self) -> None:
        if not self.allocation_id:
            import uuid
            self.allocation_id = str(uuid.uuid4())[:16]
```

## Solution 2: Per-Conversation Memory Ledger

```python
import sys
import time
from threading import Lock
from typing import Any, Dict, List, Optional


class ConversationMemoryLedger:
    """
    Tracks memory allocations per conversation.
    Provides current usage, peak usage, and category breakdown.
    """

    def __init__(self, conversation_id: str, limit_bytes: Optional[int] = None):
        self.conversation_id = conversation_id
        self._limit = limit_bytes
        self._allocations: Dict[str, MemoryAllocationRecord] = {}
        self._peak_bytes = 0
        self._lock = Lock()
        self._created_at = time.time()

    def allocate(self, record: MemoryAllocationRecord) -> None:
        with self._lock:
            if self._limit and self.total_bytes() + record.size_bytes > self._limit:
                raise ConversationMemoryLimitError(
                    self.conversation_id,
                    self.total_bytes(),
                    self._limit,
                )
            self._allocations[record.allocation_id] = record
            current = self.total_bytes()
            if current > self._peak_bytes:
                self._peak_bytes = current

    def free(self, allocation_id: str) -> None:
        with self._lock:
            self._allocations.pop(allocation_id, None)

    def total_bytes(self) -> int:
        return sum(r.size_bytes for r in self._allocations.values())

    def by_category(self) -> Dict[str, int]:
        result: dict = {}
        with self._lock:
            for r in self._allocations.values():
                result[r.category.value] = result.get(r.category.value, 0) + r.size_bytes
        return result

    def summary(self) -> dict:
        with self._lock:
            total = self.total_bytes()
            return {
                "conversation_id": self.conversation_id,
                "total_bytes": total,
                "total_kb": round(total / 1024, 2),
                "peak_bytes": self._peak_bytes,
                "allocation_count": len(self._allocations),
                "by_category": self.by_category(),
                "limit_bytes": self._limit,
                "limit_used_pct": round(total / self._limit * 100, 1) if self._limit else None,
                "age_seconds": round(time.time() - self._created_at, 1),
            }


class ConversationMemoryLimitError(Exception):
    def __init__(self, conv_id: str, current: int, limit: int):
        super().__init__(
            f"conversation '{conv_id}' memory limit reached: {current/1024:.1f}KB / {limit/1024:.1f}KB"
        )
        self.conversation_id = conv_id
        self.current_bytes = current
        self.limit_bytes = limit
```

## Solution 3: Memory Attribution Registry

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class MemoryAttributionRegistry:
    """
    Global registry of per-conversation ledgers.
    Provides cross-conversation ranking and outlier detection.
    """

    def __init__(
        self,
        default_limit_bytes: Optional[int] = 50 * 1024 * 1024,  # 50MB per conversation
    ):
        self._default_limit = default_limit_bytes
        self._ledgers: Dict[str, ConversationMemoryLedger] = {}
        self._lock = Lock()

    def get_or_create(self, conversation_id: str) -> ConversationMemoryLedger:
        with self._lock:
            if conversation_id not in self._ledgers:
                self._ledgers[conversation_id] = ConversationMemoryLedger(
                    conversation_id=conversation_id,
                    limit_bytes=self._default_limit,
                )
            return self._ledgers[conversation_id]

    def record(
        self,
        conversation_id: str,
        category: MemoryCategory,
        size_bytes: int,
        label: str = "",
    ) -> str:
        ledger = self.get_or_create(conversation_id)
        record = MemoryAllocationRecord(
            conversation_id=conversation_id,
            category=category,
            size_bytes=size_bytes,
            label=label,
        )
        ledger.allocate(record)
        return record.allocation_id

    def free(self, conversation_id: str, allocation_id: str) -> None:
        ledger = self._ledgers.get(conversation_id)
        if ledger:
            ledger.free(allocation_id)

    def evict_conversation(self, conversation_id: str) -> None:
        with self._lock:
            self._ledgers.pop(conversation_id, None)

    def total_attributed_bytes(self) -> int:
        with self._lock:
            return sum(l.total_bytes() for l in self._ledgers.values())

    def ranked_by_usage(self, top_n: int = 20) -> List[dict]:
        with self._lock:
            summaries = [l.summary() for l in self._ledgers.values()]
        return sorted(summaries, key=lambda s: s["total_bytes"], reverse=True)[:top_n]
```

## Solution 4: Object Size Estimator

```python
import sys
from typing import Any, List


class ObjectSizeEstimator:
    """
    Estimates memory footprint of common agent data structures
    without deep introspection overhead.
    """

    BYTES_PER_CHAR = 2    # Python str is ~2 bytes/char (UCS-2)
    BYTES_PER_FLOAT = 8   # float64
    DICT_OVERHEAD = 200   # approximate dict object overhead

    @classmethod
    def estimate(cls, obj: Any) -> int:
        if isinstance(obj, str):
            return len(obj) * cls.BYTES_PER_CHAR + 50

        if isinstance(obj, (list, tuple)):
            return sum(cls.estimate(item) for item in obj) + 56 * len(obj)

        if isinstance(obj, dict):
            return (
                sum(cls.estimate(k) + cls.estimate(v) for k, v in obj.items())
                + cls.DICT_OVERHEAD
            )

        if isinstance(obj, (int, float, bool)):
            return 28

        if isinstance(obj, bytes):
            return len(obj) + 33

        # Embedding vectors
        if hasattr(obj, "__len__") and hasattr(obj, "__iter__"):
            try:
                return len(obj) * cls.BYTES_PER_FLOAT + 56
            except Exception:
                pass

        return sys.getsizeof(obj)

    @classmethod
    def estimate_message(cls, message: dict) -> int:
        role = len(message.get("role", "")) * cls.BYTES_PER_CHAR
        content = len(message.get("content", "")) * cls.BYTES_PER_CHAR
        return role + content + cls.DICT_OVERHEAD
```

## Solution 5: Memory Pressure Evictor

```python
import time
from typing import List, Optional


class MemoryPressureEvictor:
    """
    Evicts the most memory-intensive conversations when total attributed
    memory exceeds a process-level threshold.
    """

    def __init__(
        self,
        registry: MemoryAttributionRegistry,
        process_limit_bytes: int = 1024 * 1024 * 1024,  # 1GB
        eviction_target_pct: float = 0.80,  # evict until 80% of limit
    ):
        self._registry = registry
        self._limit = process_limit_bytes
        self._target = eviction_target_pct
        self._eviction_log: List[dict] = []

    def check_and_evict(self) -> dict:
        total = self._registry.total_attributed_bytes()
        if total < self._limit:
            return {"evicted": 0, "total_bytes": total, "pressure": False}

        target_bytes = int(self._limit * self._eviction_target_pct)
        ranked = self._registry.ranked_by_usage(top_n=100)
        evicted = []

        for summary in ranked:
            if self._registry.total_attributed_bytes() <= target_bytes:
                break
            conv_id = summary["conversation_id"]
            freed = summary["total_bytes"]
            self._registry.evict_conversation(conv_id)
            evicted.append({"conversation_id": conv_id, "freed_bytes": freed})
            self._eviction_log.append({
                "ts": time.time(),
                "conversation_id": conv_id,
                "freed_bytes": freed,
            })

        return {
            "evicted": len(evicted),
            "evicted_conversations": evicted,
            "total_bytes_before": total,
            "total_bytes_after": self._registry.total_attributed_bytes(),
            "pressure": True,
        }

    def eviction_history(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [e for e in self._eviction_log if e["ts"] >= cutoff]
```

## Solution 6: Memory Attribution Dashboard

```python
import time


class MemoryAttributionDashboard:
    """
    Combines registry totals, top consumers, and eviction history
    into a single operational snapshot for memory health monitoring.
    """

    def __init__(
        self,
        registry: MemoryAttributionRegistry,
        evictor: MemoryPressureEvictor,
    ):
        self._registry = registry
        self._evictor = evictor

    def render(self) -> dict:
        total = self._registry.total_attributed_bytes()
        top = self._registry.ranked_by_usage(top_n=5)
        evictions = self._evictor.eviction_history(window_seconds=3600.0)

        return {
            "generated_at": time.time(),
            "total_attributed_bytes": total,
            "total_attributed_mb": round(total / 1024 / 1024, 2),
            "tracked_conversations": len(self._registry._ledgers),
            "top_consumers": [
                {
                    "conversation_id": s["conversation_id"],
                    "total_kb": s["total_kb"],
                    "age_seconds": s["age_seconds"],
                    "limit_used_pct": s["limit_used_pct"],
                }
                for s in top
            ],
            "evictions_1h": len(evictions),
            "bytes_evicted_1h": sum(e["freed_bytes"] for e in evictions),
        }
```

## Comparison

| Approach | Per-Conv Tracking | Limit Enforcement | Outlier Detection | Eviction | Dashboard |
|---|---|---|---|---|---|
| ConversationMemoryLedger | Yes (per alloc) | Yes (raises) | No | No | No |
| MemoryAttributionRegistry | Via ledgers | Via ledgers | Yes (ranked) | Yes | No |
| ObjectSizeEstimator | No | No | No | No | No |
| MemoryPressureEvictor | No | No | No | Yes (LRU-like) | No |
| MemoryAttributionDashboard | No | No | Via registry | Via evictor | Yes |

**Best for production**: Set `default_limit_bytes=50MB` per conversation — this prevents any single session from consuming more than a fraction of typical agent memory. Call `registry.record()` at every point where the agent stores data on behalf of a conversation: message append, tool result cache write, embedding storage. Use `ObjectSizeEstimator.estimate_message()` rather than `sys.getsizeof()` — the latter only measures the immediate object, not referenced strings. Run `MemoryPressureEvictor.check_and_evict()` on a 30-second timer and emit the result as a structured log event: a spike in eviction frequency indicates that conversation load has outgrown available memory and horizontal scaling is needed.
