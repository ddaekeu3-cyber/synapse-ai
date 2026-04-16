---
title: "Agent Doesn't Implement LLM Response Cache Warming on Startup"
description: "Agents that start with a cold cache serve their first requests from the LLM with full latency — common repeated queries, standard planning prompts, and frequently-invoked tool call templates all incur cold-start overhead on the first user after each deployment. Implement LLM response cache warming that pre-populates the cache with high-probability requests during the startup phase, so that the first real user request hits a warm cache."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-llm-response-cache-warming-on-startup
tags: [cache-warming, startup, llm-cache, cold-start, precompute, response-cache]
symptoms:
  - "First requests after every deployment are noticeably slower than subsequent ones"
  - "Cache hit rate starts at 0% after restart and slowly climbs to steady state over 30 minutes"
  - "Common planning prompts that appear in every session miss the cache on first use"
  - "No mechanism to pre-populate the cache before traffic arrives"
  - "High-frequency query patterns are known but not exploited for pre-warming"
---

## Why This Happens

LLM response caches accumulate entries as requests are served. After a deployment or restart, the cache is empty regardless of how warm it was before. The first requests in each new user session must go to the LLM, incurring full latency. For agents with predictable query patterns — fixed system prompt + tool definitions, common planning phrases, frequently-asked user questions — these cold hits are avoidable. Cache warming pre-computes responses for the most probable requests during a controlled startup window, before the first user request is dispatched, converting cold misses to warm hits.

## Solution 1: Warm-Up Request Descriptor

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WarmUpRequest:
    request_id: str
    prompt_messages: List[Dict[str, Any]]  # standard messages format
    model: str
    priority: int = 50                     # lower = warm up first
    expected_hit_rate: float = 0.0         # estimated fraction of sessions that will hit this
    tags: List[str] = field(default_factory=list)
    cache_ttl_seconds: Optional[float] = None
    description: str = ""
```

## Solution 2: Warm-Up Request Catalog

```python
from typing import Dict, List, Optional


class WarmUpRequestCatalog:
    """
    Stores and prioritizes warm-up requests. Entries can be loaded from
    configuration, derived from historical request logs, or registered
    programmatically for known high-frequency prompts.
    """

    def __init__(self):
        self._requests: Dict[str, WarmUpRequest] = {}

    def register(self, request: WarmUpRequest) -> None:
        self._requests[request.request_id] = request

    def register_many(self, requests: List[WarmUpRequest]) -> None:
        for r in requests:
            self.register(r)

    def get_prioritized(
        self,
        max_count: Optional[int] = None,
        min_expected_hit_rate: float = 0.0,
    ) -> List[WarmUpRequest]:
        filtered = [
            r for r in self._requests.values()
            if r.expected_hit_rate >= min_expected_hit_rate
        ]
        sorted_requests = sorted(filtered, key=lambda r: (r.priority, -r.expected_hit_rate))
        if max_count:
            return sorted_requests[:max_count]
        return sorted_requests

    def count(self) -> int:
        return len(self._requests)
```

## Solution 3: Historical Pattern Extractor

```python
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


class HistoricalPatternExtractor:
    """
    Analyzes a sample of historical request logs to identify
    the most frequently occurring prompt patterns for warm-up.
    Uses the first N tokens of the user message as the pattern key.
    """

    def __init__(self, pattern_prefix_length: int = 100):
        self._prefix_len = pattern_prefix_length

    def extract(
        self,
        request_logs: List[Dict[str, Any]],
        top_n: int = 20,
        model: str = "claude-sonnet-4-6",
    ) -> List[WarmUpRequest]:
        """
        request_logs: list of {"messages": [...], "model": str} dicts
        """
        pattern_counts: Counter = Counter()
        pattern_examples: Dict[str, Dict[str, Any]] = {}

        for log in request_logs:
            messages = log.get("messages", [])
            user_msgs = [m for m in messages if m.get("role") == "user"]
            if not user_msgs:
                continue
            content = str(user_msgs[-1].get("content", ""))
            prefix = content[: self._prefix_len].strip()
            if prefix:
                pattern_counts[prefix] += 1
                if prefix not in pattern_examples:
                    pattern_examples[prefix] = log

        total = sum(pattern_counts.values())
        requests = []
        for i, (prefix, count) in enumerate(pattern_counts.most_common(top_n)):
            log = pattern_examples[prefix]
            requests.append(WarmUpRequest(
                request_id=f"historical_{i:03d}",
                prompt_messages=log.get("messages", []),
                model=log.get("model", model),
                priority=i,
                expected_hit_rate=count / max(total, 1),
                description=f"Historical pattern: '{prefix[:40]}…'",
            ))
        return requests
```

## Solution 4: Cache Warm-Up Executor

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class WarmUpResult:
    request_id: str
    success: bool
    latency_ms: float
    error: Optional[str] = None
    cached: bool = False


class CacheWarmUpExecutor:
    """
    Executes warm-up requests concurrently during agent startup.
    Calls the LLM (or cache-backed client) to populate cache entries.
    Reports per-request success and aggregate warm-up statistics.
    """

    def __init__(
        self,
        llm_call_fn: Callable,       # async fn(messages, model, **kwargs) -> str
        max_concurrent: int = 5,
        request_timeout_seconds: float = 30.0,
    ):
        self._llm = llm_call_fn
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timeout = request_timeout_seconds

    async def _warm_one(self, request: WarmUpRequest) -> WarmUpResult:
        start = time.time()
        async with self._semaphore:
            try:
                await asyncio.wait_for(
                    self._llm(
                        messages=request.prompt_messages,
                        model=request.model,
                    ),
                    timeout=self._timeout,
                )
                latency_ms = round((time.time() - start) * 1000, 2)
                return WarmUpResult(
                    request_id=request.request_id,
                    success=True,
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                latency_ms = round((time.time() - start) * 1000, 2)
                return WarmUpResult(
                    request_id=request.request_id,
                    success=False,
                    latency_ms=latency_ms,
                    error=str(exc),
                )

    async def warm_up(self, requests: List[WarmUpRequest]) -> List[WarmUpResult]:
        tasks = [self._warm_one(r) for r in requests]
        return await asyncio.gather(*tasks)
```

## Solution 5: Warm-Up Phase Controller

```python
import asyncio
import time
from typing import Callable, List, Optional


class WarmUpPhaseController:
    """
    Manages the warm-up phase at agent startup: runs warm-up requests
    before the agent begins serving traffic, with a configurable deadline.
    Allows traffic to start even if warm-up is not complete (best-effort).
    """

    def __init__(
        self,
        catalog: WarmUpRequestCatalog,
        executor: CacheWarmUpExecutor,
        max_warm_up_duration_seconds: float = 30.0,
        max_requests_per_startup: int = 50,
        min_expected_hit_rate: float = 0.05,
    ):
        self._catalog = catalog
        self._executor = executor
        self._max_duration = max_warm_up_duration_seconds
        self._max_requests = max_requests_per_startup
        self._min_hit_rate = min_expected_hit_rate
        self._last_run_report: Optional[dict] = None

    async def run(self) -> dict:
        start = time.time()
        requests = self._catalog.get_prioritized(
            max_count=self._max_requests,
            min_expected_hit_rate=self._min_hit_rate,
        )

        if not requests:
            return {"status": "skipped", "reason": "no_warm_up_requests_registered"}

        try:
            results = await asyncio.wait_for(
                self._executor.warm_up(requests),
                timeout=self._max_duration,
            )
        except asyncio.TimeoutError:
            results = []

        elapsed = round(time.time() - start, 2)
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        report = {
            "status": "complete",
            "elapsed_seconds": elapsed,
            "attempted": len(results),
            "succeeded": len(successes),
            "failed": len(failures),
            "avg_latency_ms": round(
                sum(r.latency_ms for r in successes) / max(len(successes), 1), 2
            ),
            "errors": [
                {"request_id": r.request_id, "error": r.error}
                for r in failures
            ],
        }
        self._last_run_report = report
        return report
```

## Solution 6: Warm-Up Effectiveness Tracker

```python
import time
from typing import List, Optional


class WarmUpEffectivenessTracker:
    """
    Measures cache hit rate before and after warm-up phases to
    verify that warming is producing real improvements in hit rate.
    """

    def __init__(self):
        self._pre_warm_hit_rate: Optional[float] = None
        self._post_warm_snapshots: List[dict] = []

    def record_pre_warm_hit_rate(self, hit_rate: float) -> None:
        self._pre_warm_hit_rate = hit_rate

    def record_post_warm_snapshot(self, hit_rate: float, minutes_since_warm: float) -> None:
        self._post_warm_snapshots.append({
            "ts": time.time(),
            "hit_rate": hit_rate,
            "minutes_since_warm": minutes_since_warm,
        })

    def effectiveness_report(self) -> dict:
        if not self._post_warm_snapshots:
            return {"status": "no_post_warm_data"}

        latest = self._post_warm_snapshots[-1]
        improvement = None
        if self._pre_warm_hit_rate is not None:
            improvement = round(latest["hit_rate"] - self._pre_warm_hit_rate, 4)

        return {
            "pre_warm_hit_rate": self._pre_warm_hit_rate,
            "latest_post_warm_hit_rate": latest["hit_rate"],
            "hit_rate_improvement": improvement,
            "snapshots": len(self._post_warm_snapshots),
        }
```

## Comparison

| Approach | Request Catalog | Historical Mining | Concurrent Execution | Deadline Control | Effectiveness Tracking |
|---|---|---|---|---|---|
| WarmUpRequestCatalog | Yes | No | No | No | No |
| HistoricalPatternExtractor | No | Yes | No | No | No |
| CacheWarmUpExecutor | No | No | Yes (semaphore) | No | No |
| WarmUpPhaseController | Via catalog | No | Via executor | Yes | No |
| WarmUpEffectivenessTracker | No | No | No | No | Yes |

**Best for production**: Run warm-up with `max_warm_up_duration_seconds=20` and use `asyncio.wait_for` to enforce the deadline — the agent must start serving traffic within a predictable time even if warm-up is incomplete. Populate `WarmUpRequestCatalog` with the top 20 entries from `HistoricalPatternExtractor` applied to the previous day's request logs: this ensures the warm-up set reflects actual usage patterns rather than hand-curated guesses. Use `WarmUpEffectivenessTracker` to compare hit rates in the first 5 minutes after deployment against the steady-state baseline — if improvement is less than 10 percentage points, the warm-up set does not match actual traffic and should be re-derived from fresher logs. Limit warm-up to requests with `expected_hit_rate >= 0.05` to avoid wasting startup budget on low-probability entries.
