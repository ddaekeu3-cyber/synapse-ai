---
title: "Agent Doesn't Implement Incremental Context Rebuilding"
description: "Agents that rebuild the entire context from scratch on every turn re-serialize history, re-format tool schemas, and re-inject system instructions on each request — work that is largely identical to the previous turn. Implement incremental context rebuilding that computes a stable base context once, tracks only the delta introduced by each new turn, and assembles the final context by appending the delta to the cached base."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-incremental-context-rebuilding
tags: [incremental-context, context-rebuilding, delta-assembly, turn-delta, context-caching, prompt-efficiency]
symptoms:
  - "Context assembly takes 50ms per turn even though 90% of the content is unchanged"
  - "System prompt and tool schemas are re-serialized on every LLM call"
  - "History serialization traverses the full message list every turn"
  - "Token counting runs over the entire context even when only one new message was added"
  - "Profiling shows context assembly is the second-largest contributor to per-turn latency"
---

## Why This Happens

The natural implementation of context assembly iterates the full history list, re-formats every message, re-injects the system prompt, and re-serializes tool schemas on every turn. As history grows, this work grows proportionally. Incremental rebuilding observes that most of the context — system prompt, tool schemas, the existing history — does not change between turns. Only the new user message and the previous assistant response are novel. An incremental assembler caches the serialized base context and appends only the new delta each turn, reducing assembly work from O(n) to O(1) per turn.

## Solution 1: Context Segment

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ContextSegment:
    segment_id: str
    content: str
    token_estimate: int
    fingerprint: str = ""
    last_modified_at: float = field(default_factory=time.time)
    is_stable: bool = True   # stable segments don't change between turns

    def __post_init__(self):
        if not self.fingerprint:
            self.fingerprint = hashlib.sha256(
                self.content.encode()
            ).hexdigest()[:12]
```

## Solution 2: Stable Base Context Cache

```python
import hashlib
import time
from typing import Dict, List, Optional


class StableBaseContextCache:
    """
    Caches the serialized base context (system prompt + tool schemas +
    stable instructions). Invalidates when any stable segment changes.
    """

    def __init__(self):
        self._cached_base: Optional[str] = None
        self._cached_fingerprint: Optional[str] = None
        self._cached_tokens: int = 0
        self._hit_count = 0
        self._miss_count = 0

    def _compute_fingerprint(self, segments: List[ContextSegment]) -> str:
        combined = "|".join(s.fingerprint for s in segments if s.is_stable)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    def get(
        self,
        stable_segments: List[ContextSegment],
    ) -> Optional[str]:
        fp = self._compute_fingerprint(stable_segments)
        if fp == self._cached_fingerprint and self._cached_base is not None:
            self._hit_count += 1
            return self._cached_base
        self._miss_count += 1
        return None

    def set(
        self,
        stable_segments: List[ContextSegment],
        base_context: str,
        token_estimate: int,
    ) -> None:
        fp = self._compute_fingerprint(stable_segments)
        self._cached_base = base_context
        self._cached_fingerprint = fp
        self._cached_tokens = token_estimate

    def cached_tokens(self) -> int:
        return self._cached_tokens

    def hit_rate(self) -> float:
        total = self._hit_count + self._miss_count
        return round(self._hit_count / max(total, 1), 4)

    def stats(self) -> dict:
        return {
            "hits": self._hit_count,
            "misses": self._miss_count,
            "hit_rate": self.hit_rate(),
            "cached_tokens": self._cached_tokens,
        }
```

## Solution 3: Turn Delta Accumulator

```python
import json
import time
from typing import Any, Dict, List, Optional


class TurnDeltaAccumulator:
    """
    Accumulates turn deltas (new user message + assistant response)
    as lightweight serialized strings rather than re-serializing
    the full history each turn.
    """

    def __init__(self, max_turns: int = 200):
        self._max = max_turns
        self._deltas: List[str] = []
        self._delta_tokens: List[int] = []
        self._total_delta_tokens: int = 0

    def append_user_turn(self, content: str) -> str:
        serialized = json.dumps({"role": "user", "content": content})
        self._deltas.append(serialized)
        token_est = len(serialized) // 4
        self._delta_tokens.append(token_est)
        self._total_delta_tokens += token_est
        self._trim()
        return serialized

    def append_assistant_turn(self, content: str) -> str:
        serialized = json.dumps({"role": "assistant", "content": content})
        self._deltas.append(serialized)
        token_est = len(serialized) // 4
        self._delta_tokens.append(token_est)
        self._total_delta_tokens += token_est
        self._trim()
        return serialized

    def _trim(self) -> None:
        while len(self._deltas) > self._max:
            removed_tokens = self._delta_tokens.pop(0)
            self._deltas.pop(0)
            self._total_delta_tokens -= removed_tokens

    def assembled_delta(self) -> str:
        return "\n".join(self._deltas)

    def total_tokens(self) -> int:
        return self._total_delta_tokens

    def turn_count(self) -> int:
        return len(self._deltas) // 2
```

## Solution 4: Incremental Context Assembler

```python
import time
from typing import List, Optional


class IncrementalContextAssembler:
    """
    Assembles the final context by retrieving the cached base
    and appending only the turn delta. Tracks assembly latency
    and token savings from cache hits.
    """

    def __init__(
        self,
        base_cache: StableBaseContextCache,
        delta_accumulator: TurnDeltaAccumulator,
    ):
        self._base_cache = base_cache
        self._delta = delta_accumulator
        self._assembly_times_ms: List[float] = []
        self._full_rebuild_count = 0
        self._incremental_count = 0

    def assemble(
        self,
        stable_segments: List[ContextSegment],
        base_builder_fn=None,
    ) -> dict:
        start = time.time()

        cached_base = self._base_cache.get(stable_segments)

        if cached_base is not None:
            base_context = cached_base
            self._incremental_count += 1
        else:
            # Full rebuild required (first turn or stable segment changed)
            base_context = "\n\n".join(s.content for s in stable_segments if s.is_stable)
            token_est = sum(s.token_estimate for s in stable_segments if s.is_stable)
            self._base_cache.set(stable_segments, base_context, token_est)
            if base_builder_fn:
                base_context = base_builder_fn(stable_segments)
            self._full_rebuild_count += 1

        delta = self._delta.assembled_delta()
        full_context = base_context
        if delta:
            full_context = base_context + "\n\n" + delta

        elapsed_ms = round((time.time() - start) * 1000, 3)
        self._assembly_times_ms.append(elapsed_ms)

        total_tokens = self._base_cache.cached_tokens() + self._delta.total_tokens()

        return {
            "context": full_context,
            "total_tokens_est": total_tokens,
            "base_tokens_est": self._base_cache.cached_tokens(),
            "delta_tokens_est": self._delta.total_tokens(),
            "assembly_ms": elapsed_ms,
            "incremental": cached_base is not None,
        }

    def avg_assembly_ms(self) -> float:
        if not self._assembly_times_ms:
            return 0.0
        recent = self._assembly_times_ms[-200:]
        return round(sum(recent) / len(recent), 3)

    def efficiency_stats(self) -> dict:
        total = self._full_rebuild_count + self._incremental_count
        return {
            "total_assemblies": total,
            "full_rebuilds": self._full_rebuild_count,
            "incremental": self._incremental_count,
            "incremental_rate": round(self._incremental_count / max(total, 1), 4),
            "avg_assembly_ms": self.avg_assembly_ms(),
            "base_cache_stats": self._base_cache.stats(),
        }
```

## Solution 5: Segment Change Detector

```python
import hashlib
from typing import Any, Dict, List, Optional


class SegmentChangeDetector:
    """
    Detects when a stable segment's content has changed and
    invalidates the base cache by updating the segment's fingerprint.
    """

    def __init__(self):
        self._known_fingerprints: Dict[str, str] = {}

    def has_changed(self, segment: ContextSegment) -> bool:
        current_fp = hashlib.sha256(segment.content.encode()).hexdigest()[:12]
        old_fp = self._known_fingerprints.get(segment.segment_id)
        changed = old_fp is not None and old_fp != current_fp
        self._known_fingerprints[segment.segment_id] = current_fp
        # Update the segment's fingerprint so the base cache detects the change
        segment.fingerprint = current_fp
        return changed

    def check_all(self, segments: List[ContextSegment]) -> List[str]:
        """Returns list of segment_ids that changed."""
        return [s.segment_id for s in segments if self.has_changed(s)]
```

## Solution 6: Incremental Context Dashboard

```python
import time


class IncrementalContextDashboard:
    """
    Surfaces assembly efficiency, cache hit rates, and
    latency improvements from incremental rebuilding.
    """

    def __init__(self, assembler: IncrementalContextAssembler):
        self._assembler = assembler

    def render(self) -> dict:
        stats = self._assembler.efficiency_stats()
        return {
            "generated_at": time.time(),
            "efficiency": stats,
            "latency_summary": {
                "avg_assembly_ms": stats["avg_assembly_ms"],
            },
        }
```

## Comparison

| Approach | Base Caching | Delta Tracking | Change Detection | Full/Incremental Mix | Dashboard |
|---|---|---|---|---|---|
| StableBaseContextCache | Yes (fingerprint) | No | Via fingerprint | No | No |
| TurnDeltaAccumulator | No | Yes (per turn) | No | No | No |
| IncrementalContextAssembler | Via cache | Via accumulator | No | Yes | No |
| SegmentChangeDetector | No | No | Yes (hash diff) | No | No |
| IncrementalContextDashboard | No | No | No | No | Yes |

**Best for production**: Mark the system prompt, safety instructions, and tool schemas as `is_stable=True` — these rarely change and account for 20-50% of total context tokens. Run `SegmentChangeDetector.check_all()` before each assembly to detect tool schema updates or system prompt changes that require a base cache invalidation. For a 100-turn conversation, incremental assembly reduces per-turn context work from O(n) to O(1) — the delta is constant size regardless of history depth. Monitor `avg_assembly_ms` before and after enabling incremental assembly; for long conversations the improvement is often 10-30ms per turn which compounds to meaningful latency reduction over the session.
