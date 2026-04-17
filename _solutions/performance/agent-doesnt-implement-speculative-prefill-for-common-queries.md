---
title: "Agent Doesn't Implement Speculative Prefill for Common Queries"
description: "Agents that treat every request as unique miss the opportunity to prefill the KV cache with tokens shared across many queries: system prompt, few-shot examples, tool schemas, and boilerplate context. Implement speculative prefill that identifies the stable prefix of each prompt, precomputes its KV cache representation during idle time, and reuses it across requests to reduce time-to-first-token and LLM API costs."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-prefill-for-common-queries
tags: [speculative-prefill, kv-cache, prefix-caching, time-to-first-token, prompt-optimization, cache-warming]
symptoms:
  - "TTFT is identical for the first and thousandth request — shared prefix not cached"
  - "System prompt tokens billed on every API call despite being identical across requests"
  - "No measurement of what fraction of input tokens are repeated across requests"
  - "Prompt assembly concatenates system prompt + tools + history every time from scratch"
  - "Idle agent instances not used to warm prefix caches for predictable upcoming queries"
---

## Why This Happens

LLM APIs that support prompt caching (Anthropic, OpenAI) reuse KV cache entries for prompt prefixes that were seen in recent prior requests. The cache hit requires that the prefix be byte-identical — even a single token difference invalidates the cache. Agents that dynamically assemble the full prompt on every request, inserting dynamic values (timestamps, request IDs) before static content, break the cache. Speculative prefill means structuring prompts so static content comes first and is never modified, and optionally sending warm-up requests during idle periods so the cache is hot before user traffic arrives.

## Solution 1: Prompt Prefix Analyzer

```python
import hashlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class PromptSegment:
    label: str               # e.g. "system_prompt", "tool_schemas", "few_shot"
    content: str
    is_static: bool          # True = eligible for prefix caching
    token_estimate: int = 0

    def fingerprint(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()[:16]


@dataclass
class PromptPrefixAnalysis:
    segments: List[PromptSegment]
    static_prefix: str       # concatenated static segments (cache key)
    static_token_estimate: int
    dynamic_suffix: str
    dynamic_token_estimate: int
    cache_eligible_fraction: float

    def static_fingerprint(self) -> str:
        return hashlib.sha256(self.static_prefix.encode()).hexdigest()[:24]


class PromptPrefixAnalyzer:
    """
    Splits a prompt into a static cacheable prefix and a dynamic suffix.
    Static segments must precede all dynamic segments — the first dynamic
    segment marks the boundary.
    """

    CHARS_PER_TOKEN = 4.0

    def analyze(self, segments: List[PromptSegment]) -> PromptPrefixAnalysis:
        static_parts: List[PromptSegment] = []
        dynamic_parts: List[PromptSegment] = []
        found_dynamic = False

        for seg in segments:
            if not seg.is_static:
                found_dynamic = True
            if found_dynamic:
                dynamic_parts.append(seg)
            else:
                static_parts.append(seg)

        static_text = "".join(s.content for s in static_parts)
        dynamic_text = "".join(s.content for s in dynamic_parts)
        static_tokens = int(len(static_text) / self.CHARS_PER_TOKEN)
        dynamic_tokens = int(len(dynamic_text) / self.CHARS_PER_TOKEN)
        total_tokens = max(static_tokens + dynamic_tokens, 1)

        return PromptPrefixAnalysis(
            segments=segments,
            static_prefix=static_text,
            static_token_estimate=static_tokens,
            dynamic_suffix=dynamic_text,
            dynamic_token_estimate=dynamic_tokens,
            cache_eligible_fraction=round(static_tokens / total_tokens, 4),
        )
```

## Solution 2: Prefix Cache Registry

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional


@dataclass
class PrefixCacheEntry:
    fingerprint: str
    static_prefix: str
    token_estimate: int
    warmed_at: float = field(default_factory=time.time)
    hit_count: int = 0
    last_hit_at: float = field(default_factory=time.time)

    def record_hit(self) -> None:
        self.hit_count += 1
        self.last_hit_at = time.time()

    def age_s(self) -> float:
        return time.time() - self.warmed_at


class PrefixCacheRegistry:
    """
    Tracks which static prefixes are known to be warm in the upstream
    LLM API's KV cache. Entries expire after TTL (cache eviction estimate).
    """

    def __init__(self, ttl_s: float = 300.0):
        self._entries: Dict[str, PrefixCacheEntry] = {}
        self._ttl = ttl_s
        self._lock = Lock()
        self._total_hits = 0
        self._total_misses = 0

    def register(self, fingerprint: str, prefix: str, token_estimate: int) -> None:
        with self._lock:
            self._entries[fingerprint] = PrefixCacheEntry(
                fingerprint=fingerprint,
                static_prefix=prefix,
                token_estimate=token_estimate,
            )

    def get(self, fingerprint: str) -> Optional[PrefixCacheEntry]:
        with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                self._total_misses += 1
                return None
            if entry.age_s() > self._ttl:
                del self._entries[fingerprint]
                self._total_misses += 1
                return None
            entry.record_hit()
            self._total_hits += 1
            return entry

    def stats(self) -> dict:
        with self._lock:
            total = self._total_hits + self._total_misses
            return {
                "warm_entries": len(self._entries),
                "total_hits": self._total_hits,
                "total_misses": self._total_misses,
                "hit_rate": round(self._total_hits / max(total, 1), 4),
            }
```

## Solution 3: Speculative Prefill Scheduler

```python
import asyncio
import time
from typing import Any, Callable, List, Optional


class SpeculativePrefillScheduler:
    """
    During idle periods, sends minimal warm-up requests to the LLM API
    using only the static prefix to prime the KV cache before user traffic
    arrives. Uses a no-op completion (max_tokens=1) to minimize cost.
    """

    def __init__(
        self,
        registry: PrefixCacheRegistry,
        analyzer: PromptPrefixAnalyzer,
        min_idle_s: float = 5.0,
        max_warmups_per_cycle: int = 3,
    ):
        self._registry = registry
        self._analyzer = analyzer
        self._min_idle = min_idle_s
        self._max_warmups = max_warmups_per_cycle
        self._last_request_at: float = time.time()
        self._warmup_count = 0
        self._running = False

    def record_request(self) -> None:
        self._last_request_at = time.time()

    def is_idle(self) -> bool:
        return time.time() - self._last_request_at >= self._min_idle

    async def warm_prefix(
        self,
        segments: List[PromptSegment],
        llm_fn: Callable,    # async (prompt: str, max_tokens: int) -> Any
    ) -> Optional[str]:
        """
        Sends a no-op request to warm the cache for this prefix.
        Returns the fingerprint on success.
        """
        analysis = self._analyzer.analyze(segments)
        fingerprint = analysis.static_fingerprint()

        cached = self._registry.get(fingerprint)
        if cached is not None:
            return fingerprint  # already warm

        try:
            # Use only the static prefix with max_tokens=1 to minimize cost
            await llm_fn(analysis.static_prefix, 1)
            self._registry.register(
                fingerprint,
                analysis.static_prefix,
                analysis.static_token_estimate,
            )
            self._warmup_count += 1
            return fingerprint
        except Exception:
            return None

    async def run_idle_warmup_cycle(
        self,
        candidate_segment_sets: List[List[PromptSegment]],
        llm_fn: Callable,
    ) -> int:
        if not self.is_idle():
            return 0
        warmed = 0
        for segments in candidate_segment_sets[:self._max_warmups]:
            result = await self.warm_prefix(segments, llm_fn)
            if result:
                warmed += 1
        return warmed
```

## Solution 4: Cache-Aware Prompt Assembler

```python
from typing import List, Optional, Tuple


class CacheAwarePromptAssembler:
    """
    Assembles the final prompt for an LLM call, placing static segments
    first to maximize cache hit probability. Returns both the assembled
    prompt and the fingerprint to report to the registry post-call.
    """

    def __init__(
        self,
        analyzer: PromptPrefixAnalyzer,
        registry: PrefixCacheRegistry,
    ):
        self._analyzer = analyzer
        self._registry = registry

    def assemble(
        self,
        segments: List[PromptSegment],
    ) -> Tuple[str, str, bool]:
        """
        Returns (full_prompt, static_fingerprint, cache_hit).
        Caller should call registry.register() after a successful LLM call
        if cache_hit was False.
        """
        # Sort: static segments first, then dynamic
        ordered = sorted(segments, key=lambda s: (0 if s.is_static else 1, segments.index(s)))
        analysis = self._analyzer.analyze(ordered)
        fingerprint = analysis.static_fingerprint()
        full_prompt = analysis.static_prefix + analysis.dynamic_suffix

        cache_hit = self._registry.get(fingerprint) is not None
        return full_prompt, fingerprint, cache_hit

    def post_call_register(
        self,
        fingerprint: str,
        segments: List[PromptSegment],
    ) -> None:
        ordered = sorted(segments, key=lambda s: (0 if s.is_static else 1, segments.index(s)))
        analysis = self._analyzer.analyze(ordered)
        self._registry.register(fingerprint, analysis.static_prefix, analysis.static_token_estimate)
```

## Solution 5: Prefill Savings Estimator

```python
import time
from threading import Lock
from typing import List, Tuple


class PrefillSavingsEstimator:
    """
    Tracks estimated token savings from prefix cache hits.
    At a price of $X per 1M tokens, cache hits on the static prefix
    save X * static_tokens / 1e6 per request.
    """

    def __init__(self, price_per_million_tokens: float = 3.0):
        self._price = price_per_million_tokens / 1_000_000
        self._records: List[Tuple[float, int, bool]] = []
        # (ts, static_tokens, cache_hit)
        self._lock = Lock()

    def record(self, static_token_estimate: int, cache_hit: bool) -> None:
        with self._lock:
            self._records.append((time.time(), static_token_estimate, cache_hit))
            if len(self._records) > 100000:
                self._records = self._records[-50000:]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, tok, hit) for ts, tok, hit in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        total = len(recent)
        hits = sum(1 for _, _, hit in recent if hit)
        saved_tokens = sum(tok for _, tok, hit in recent if hit)
        saved_cost = round(saved_tokens * self._price, 4)

        return {
            "window_seconds": window_seconds,
            "requests": total,
            "cache_hits": hits,
            "hit_rate": round(hits / max(total, 1), 4),
            "saved_tokens_est": saved_tokens,
            "saved_cost_usd_est": saved_cost,
        }
```

## Solution 6: Speculative Prefill Dashboard

```python
import time


class SpeculativePrefillDashboard:
    """
    Combines registry stats, scheduler activity, and savings estimates
    into a single view for tuning and cost reporting.
    """

    def __init__(
        self,
        registry: PrefixCacheRegistry,
        scheduler: SpeculativePrefillScheduler,
        savings: PrefillSavingsEstimator,
    ):
        self._registry = registry
        self._scheduler = scheduler
        self._savings = savings

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "registry": self._registry.stats(),
            "scheduler": {
                "is_idle": self._scheduler.is_idle(),
                "total_warmups": self._scheduler._warmup_count,
            },
            "savings_last_hour": self._savings.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Prefix Detection | Cache Registry | Idle Warmup | Cost Savings | Cache-Aware Assembly |
|---|---|---|---|---|---|
| PromptPrefixAnalyzer | Yes (static/dynamic split) | No | No | No | No |
| PrefixCacheRegistry | No | Yes (TTL eviction) | No | No | No |
| SpeculativePrefillScheduler | Via analyzer | Via registry | Yes | No | No |
| CacheAwarePromptAssembler | Via analyzer | Via registry | No | No | Yes |
| PrefillSavingsEstimator | No | No | No | Yes | No |
| SpeculativePrefillDashboard | No | No | No | No | No |

**Best for production**: Always place static segments (system prompt, tool schemas, few-shot examples) before dynamic segments (conversation history, user query) — most LLM APIs require a byte-identical prefix for cache hits. Use `SpeculativePrefillScheduler` during the 5–30 second idle window between user sessions to pre-warm the cache for the next request. Monitor `hit_rate` from `PrefillSavingsEstimator` — below 60% on a stable system prompt means dynamic content is leaking into the prefix position and breaking the cache. At 3 USD/million tokens, a 70% hit rate on a 2000-token system prompt saves roughly $0.004 per request, which compounds significantly at scale.
