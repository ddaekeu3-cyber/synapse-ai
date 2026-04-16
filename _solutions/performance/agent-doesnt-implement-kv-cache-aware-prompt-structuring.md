---
title: "Agent Doesn't Implement KV-Cache-Aware Prompt Structuring"
description: "Agents that rebuild their full prompt on every request — interleaving static system instructions with dynamic user context — defeat LLM provider KV caching, paying full prefill cost each turn. Restructure prompts so the longest stable prefix (system prompt, tool schemas, few-shot examples) is always prepended unchanged, pushing only the dynamic tail to the end, so the provider's KV cache hits on the prefix and charges only for the incremental tokens."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-kv-cache-aware-prompt-structuring
tags: [kv-cache, prompt-structuring, prefill-cost, token-efficiency, context-prefix, inference-optimization]
symptoms:
  - "Full prompt token count billed on every request despite static system instructions"
  - "Time-to-first-token does not improve across turns in a long conversation"
  - "Tool schemas and few-shot examples re-prefilled on every call"
  - "Prompt assembly code concatenates static and dynamic content in arbitrary order"
  - "No separation between the invariant prefix and the per-request suffix"
---

## Why This Happens

LLM inference providers maintain a KV cache keyed on the token sequence seen so far. A cache hit means the provider skips re-computing attention keys and values for the matching prefix, reducing prefill cost and latency. The cache is invalidated whenever any token in the cached portion changes — including whitespace differences, reordered fields, or a timestamp injected into the system prompt. Agents that assemble prompts without regard to stability order consistently break the prefix, paying full prefill cost on every request. The fix is to canonicalize prompt assembly so the longest static segment always heads the prompt and dynamic content is appended at the tail.

## Solution 1: Prompt Segment Classifier

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
import hashlib


class SegmentType(str, Enum):
    STATIC = "static"         # never changes across requests
    QUASI_STATIC = "quasi_static"  # changes rarely (e.g., user profile loaded at session start)
    DYNAMIC = "dynamic"       # changes every request (user message, retrieved context)


@dataclass
class PromptSegment:
    content: str
    segment_type: SegmentType
    label: str = ""           # for debugging / cache hit attribution
    token_count_estimate: int = 0

    def __post_init__(self) -> None:
        if self.token_count_estimate == 0:
            self.token_count_estimate = max(1, len(self.content) // 4)

    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()[:12]


class PromptSegmentClassifier:
    """
    Classifies a set of prompt segments by stability and sorts them
    so static content precedes quasi-static which precedes dynamic.
    This ordering maximizes the cacheable prefix length.
    """

    STABILITY_ORDER = {
        SegmentType.STATIC: 0,
        SegmentType.QUASI_STATIC: 1,
        SegmentType.DYNAMIC: 2,
    }

    def sort_for_cache(self, segments: List[PromptSegment]) -> List[PromptSegment]:
        return sorted(segments, key=lambda s: self.STABILITY_ORDER[s.segment_type])

    def cacheable_token_estimate(self, segments: List[PromptSegment]) -> int:
        """Tokens in the stable prefix (STATIC + QUASI_STATIC segments)."""
        return sum(
            s.token_count_estimate
            for s in segments
            if s.segment_type != SegmentType.DYNAMIC
        )

    def dynamic_token_estimate(self, segments: List[PromptSegment]) -> int:
        return sum(
            s.token_count_estimate
            for s in segments
            if s.segment_type == SegmentType.DYNAMIC
        )
```

## Solution 2: Stable Prefix Registry

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class StablePrefixEntry:
    def __init__(self, segments: List[PromptSegment]):
        self.segments = segments
        self.assembled: str = "\n\n".join(s.content for s in segments)
        self.token_estimate: int = sum(s.token_count_estimate for s in segments)
        self.content_hash: str = hashlib.sha256(self.assembled.encode()).hexdigest()[:16]
        self.created_at: float = time.time()
        self.hit_count: int = 0


class StablePrefixRegistry:
    """
    Stores pre-assembled static and quasi-static prompt prefixes.
    Keyed by a logical name (e.g., "default", "code_assistant").
    Detects when content has changed and invalidates the entry.
    """

    def __init__(self):
        self._entries: Dict[str, StablePrefixEntry] = {}
        self._lock = Lock()

    def register(self, name: str, segments: List[PromptSegment]) -> StablePrefixEntry:
        with self._lock:
            entry = StablePrefixEntry(segments)
            self._entries[name] = entry
            return entry

    def get(self, name: str) -> Optional[StablePrefixEntry]:
        with self._lock:
            entry = self._entries.get(name)
            if entry:
                entry.hit_count += 1
            return entry

    def invalidate(self, name: str) -> None:
        with self._lock:
            self._entries.pop(name, None)

    def all_stats(self) -> Dict[str, dict]:
        with self._lock:
            return {
                name: {
                    "token_estimate": e.token_estimate,
                    "hit_count": e.hit_count,
                    "content_hash": e.content_hash,
                    "age_seconds": round(time.time() - e.created_at, 1),
                }
                for name, e in self._entries.items()
            }
```

## Solution 3: KV-Cache-Aware Prompt Assembler

```python
from typing import List, Optional, Tuple


class KVCacheAwarePromptAssembler:
    """
    Assembles a final prompt by placing the stable prefix first and
    appending dynamic segments at the tail. Tracks the split point
    so callers can annotate cache boundaries for providers that
    accept explicit cache-control hints (e.g., Anthropic cache_control).
    """

    def __init__(
        self,
        registry: StablePrefixRegistry,
        classifier: PromptSegmentClassifier,
        segment_separator: str = "\n\n",
    ):
        self._registry = registry
        self._classifier = classifier
        self._sep = segment_separator

    def assemble(
        self,
        prefix_name: str,
        dynamic_segments: List[PromptSegment],
    ) -> Tuple[str, int, int]:
        """
        Returns (full_prompt, cacheable_token_estimate, dynamic_token_estimate).
        The cacheable portion is everything before the first dynamic segment.
        """
        prefix_entry = self._registry.get(prefix_name)
        if prefix_entry is None:
            raise KeyError(f"No stable prefix registered under '{prefix_name}'")

        dynamic_text = self._sep.join(s.content for s in dynamic_segments)
        full_prompt = prefix_entry.assembled + self._sep + dynamic_text

        dynamic_tokens = sum(s.token_count_estimate for s in dynamic_segments)
        return full_prompt, prefix_entry.token_estimate, dynamic_tokens

    def assemble_messages(
        self,
        prefix_name: str,
        dynamic_segments: List[PromptSegment],
    ) -> List[dict]:
        """
        Returns messages list suitable for chat completions API, with
        the stable prefix as the first system message and dynamic content
        as a user message. Keeps the prefix as a separate message so
        providers can cache it independently.
        """
        prefix_entry = self._registry.get(prefix_name)
        if prefix_entry is None:
            raise KeyError(f"No stable prefix registered under '{prefix_name}'")

        dynamic_text = self._sep.join(s.content for s in dynamic_segments)
        return [
            {"role": "system", "content": prefix_entry.assembled},
            {"role": "user", "content": dynamic_text},
        ]
```

## Solution 4: Prefix Stability Monitor

```python
import time
from collections import deque
from typing import Deque, List, Optional, Tuple


class PrefixStabilityMonitor:
    """
    Tracks how often the stable prefix changes between requests.
    Frequent changes indicate that content which should be static
    is being regenerated (e.g., a timestamp injected into the system prompt).
    """

    def __init__(self, window_size: int = 200):
        self._window_size = window_size
        self._observations: Deque[Tuple[float, str, bool]] = deque()
        # (ts, content_hash, was_cache_hit)

    def observe(self, current_hash: str, previous_hash: Optional[str]) -> bool:
        """Returns True if the prefix was stable (cache hit)."""
        is_hit = current_hash == previous_hash if previous_hash else False
        self._observations.append((time.time(), current_hash, is_hit))
        if len(self._observations) > self._window_size:
            self._observations.popleft()
        return is_hit

    def hit_rate(self, window_seconds: float = 3600.0) -> float:
        cutoff = time.time() - window_seconds
        recent = [o for o in self._observations if o[0] >= cutoff]
        if not recent:
            return 0.0
        hits = sum(1 for _, _, hit in recent if hit)
        return round(hits / len(recent), 4)

    def instability_events(self, window_seconds: float = 3600.0) -> int:
        cutoff = time.time() - window_seconds
        return sum(
            1 for ts, _, hit in self._observations
            if ts >= cutoff and not hit
        )

    def report(self, window_seconds: float = 3600.0) -> dict:
        return {
            "window_seconds": window_seconds,
            "hit_rate": self.hit_rate(window_seconds),
            "instability_events": self.instability_events(window_seconds),
            "observations_total": len(self._observations),
        }
```

## Solution 5: KV Cache Savings Estimator

```python
import time
from typing import List


class KVCacheSavingsEstimator:
    """
    Estimates token-cost savings from KV cache hits by comparing
    what would have been billed (full prefill) vs. what was billed
    (dynamic tail only) on each cache hit.
    """

    def __init__(self, cost_per_1k_input_tokens: float = 0.003):
        self._rate = cost_per_1k_input_tokens / 1000.0
        self._records: List[dict] = []

    def record_request(
        self,
        cacheable_tokens: int,
        dynamic_tokens: int,
        was_cache_hit: bool,
    ) -> dict:
        full_cost = (cacheable_tokens + dynamic_tokens) * self._rate
        actual_cost = dynamic_tokens * self._rate if was_cache_hit else full_cost
        saved = full_cost - actual_cost

        record = {
            "ts": time.time(),
            "cacheable_tokens": cacheable_tokens,
            "dynamic_tokens": dynamic_tokens,
            "was_cache_hit": was_cache_hit,
            "full_cost_usd": round(full_cost, 6),
            "actual_cost_usd": round(actual_cost, 6),
            "saved_usd": round(saved, 6),
        }
        self._records.append(record)
        return record

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        hits = [r for r in recent if r["was_cache_hit"]]
        total_saved = sum(r["saved_usd"] for r in recent)
        total_full = sum(r["full_cost_usd"] for r in recent)

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "cache_hits": len(hits),
            "hit_rate": round(len(hits) / len(recent), 4),
            "total_saved_usd": round(total_saved, 4),
            "total_would_have_cost_usd": round(total_full, 4),
            "savings_pct": round(total_saved / max(total_full, 1e-9) * 100, 1),
        }
```

## Solution 6: KV Cache Prompt Structuring Dashboard

```python
import time


class KVCachePromptStructuringDashboard:
    """
    Combines prefix registry stats, stability monitoring, and savings
    estimates into a single operational snapshot.
    """

    def __init__(
        self,
        registry: StablePrefixRegistry,
        monitor: PrefixStabilityMonitor,
        savings_estimator: KVCacheSavingsEstimator,
    ):
        self._registry = registry
        self._monitor = monitor
        self._savings = savings_estimator

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "registered_prefixes": self._registry.all_stats(),
            "stability": self._monitor.report(window_seconds),
            "savings": self._savings.summary(window_seconds),
        }
```

## Comparison

| Approach | Segment Ordering | Prefix Caching | Stability Monitoring | Cost Estimation | Dashboard |
|---|---|---|---|---|---|
| PromptSegmentClassifier | Yes (sort by stability) | No | No | No | No |
| StablePrefixRegistry | No | Yes (pre-assembled) | No | No | No |
| KVCacheAwarePromptAssembler | Via classifier | Via registry | No | No | No |
| PrefixStabilityMonitor | No | No | Yes (hit rate) | No | No |
| KVCacheSavingsEstimator | No | No | No | Yes (USD) | No |
| KVCachePromptStructuringDashboard | No | No | No | No | Yes |

**Best for production**: Register one `StablePrefixEntry` per agent persona at startup — include system prompt, tool schemas, and all few-shot examples in the static prefix, and never inject request-scoped content (timestamps, request IDs, user names) into it. Monitor `PrefixStabilityMonitor.hit_rate()`: anything below 0.90 means something dynamic is leaking into the prefix. Use `KVCacheSavingsEstimator` to quantify ROI — for agents with a 2000-token system prompt running at 100 req/min, a 90% cache hit rate saves roughly $0.03/min at standard input pricing, compounding to meaningful monthly savings at scale.
