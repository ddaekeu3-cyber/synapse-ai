---
title: "Agent Doesn't Implement Predictive Cache Warming Based on Usage Patterns"
description: "Agents with reactive caches are cold at the start of every peak period — a morning traffic surge finds empty caches and every request pays full LLM latency until the cache warms organically. Implement predictive cache warming that analyzes historical usage patterns, identifies high-probability queries for upcoming time windows, and pre-populates the cache before peak traffic arrives."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-predictive-cache-warming-based-on-usage-patterns
tags: [cache-warming, predictive-caching, usage-patterns, precompute, peak-traffic, cold-start]
symptoms:
  - "Cache hit rate is 0% for the first 15 minutes of every business day"
  - "Morning peak traffic has 3× higher latency than afternoon traffic due to cold cache"
  - "No mechanism to pre-warm before a scheduled event that will drive predictable queries"
  - "Query patterns repeat daily but cache expires overnight, forcing organic re-warming"
  - "Cannot answer 'what queries will arrive in the next hour?' without historical analysis"
---

## Why This Happens

Caches warm reactively — each cache miss triggers a backend call, which populates the entry for subsequent requests. In predictable traffic patterns (daily peaks, scheduled events, recurring queries), reactive warming means the first wave of peak traffic always hits cold cache. Predictive warming inverts this: analyze which queries arrived during the equivalent time window yesterday, pre-execute them against the LLM before traffic arrives, and store results so the first real request hits a warm cache.

## Solution 1: Query Usage Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class QueryUsageRecord:
    query_fingerprint: str     # stable hash of the query
    query_text: str
    model: str
    hour_of_week: int          # 0–167 (168 hours in a week)
    day_of_week: int           # 0=Monday, 6=Sunday
    hour_of_day: int           # 0–23
    hit_count: int = 1
    last_seen_at: float = field(default_factory=time.time)
    avg_response_time_ms: float = 0.0
    cached_response: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def hour_of_week_label(self) -> str:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{days[self.day_of_week]} {self.hour_of_day:02d}:00"
```

## Solution 2: Usage Pattern Store

```python
import hashlib
import time
from collections import defaultdict
from typing import Dict, List, Optional


class UsagePatternStore:
    """
    Accumulates query usage records keyed by (fingerprint, hour_of_week).
    Identifies queries that appear consistently in specific time windows.
    """

    def __init__(self, max_records: int = 50_000) -> None:
        self._max = max_records
        # (fingerprint, hour_of_week) -> QueryUsageRecord
        self._records: Dict[tuple, QueryUsageRecord] = {}
        # fingerprint -> set of hour_of_week values where this query appears
        self._fingerprint_hours: Dict[str, set] = defaultdict(set)

    @staticmethod
    def _hour_of_week() -> tuple:
        t = time.localtime()
        dow = t.tm_wday
        hod = t.tm_hour
        how = dow * 24 + hod
        return dow, hod, how

    def record_query(
        self,
        query_fingerprint: str,
        query_text: str,
        model: str,
        response_time_ms: float = 0.0,
    ) -> None:
        dow, hod, how = self._hour_of_week()
        key = (query_fingerprint, how)

        if key in self._records:
            rec = self._records[key]
            rec.hit_count += 1
            rec.last_seen_at = time.time()
            rec.avg_response_time_ms = (
                rec.avg_response_time_ms * 0.9 + response_time_ms * 0.1
            )
        else:
            if len(self._records) >= self._max:
                self._evict_coldest()
            self._records[key] = QueryUsageRecord(
                query_fingerprint=query_fingerprint,
                query_text=query_text,
                model=model,
                hour_of_week=how,
                day_of_week=dow,
                hour_of_day=hod,
                avg_response_time_ms=response_time_ms,
            )

        self._fingerprint_hours[query_fingerprint].add(how)

    def _evict_coldest(self) -> None:
        if not self._records:
            return
        coldest = min(self._records, key=lambda k: self._records[k].hit_count)
        fp = self._records[coldest].query_fingerprint
        del self._records[coldest]
        self._fingerprint_hours[fp].discard(coldest[1])

    def top_queries_for_hour(
        self,
        hour_of_week: int,
        top_k: int = 50,
        min_hit_count: int = 3,
    ) -> List[QueryUsageRecord]:
        candidates = [
            rec for (fp, how), rec in self._records.items()
            if how == hour_of_week and rec.hit_count >= min_hit_count
        ]
        candidates.sort(key=lambda r: -r.hit_count)
        return candidates[:top_k]

    def recurring_queries(
        self,
        min_hours_present: int = 5,
        top_k: int = 20,
    ) -> List[str]:
        """Returns fingerprints that appear in many different hours."""
        recurring = [
            fp for fp, hours in self._fingerprint_hours.items()
            if len(hours) >= min_hours_present
        ]
        return recurring[:top_k]
```

## Solution 3: Warm-Up Schedule Planner

```python
import time
from typing import List


@dataclass
class WarmUpTask:
    query_fingerprint: str
    query_text: str
    model: str
    priority: float   # higher = warm first
    estimated_response_time_ms: float
    reason: str   # "top_hourly" | "recurring" | "pre_event"


class WarmUpSchedulePlanner:
    """
    Generates a prioritized list of cache warm-up tasks for an upcoming
    time window based on historical usage patterns.
    """

    def __init__(
        self,
        pattern_store: UsagePatternStore,
        lookahead_hours: int = 1,
        max_warmup_tasks: int = 100,
        max_warmup_budget_ms: float = 30_000.0,  # 30s total warmup budget
    ) -> None:
        self._store = pattern_store
        self._lookahead = lookahead_hours
        self._max_tasks = max_warmup_tasks
        self._budget = max_warmup_budget_ms

    def _target_hours(self) -> List[int]:
        t = time.localtime()
        current_how = t.tm_wday * 24 + t.tm_hour
        return [(current_how + h) % 168 for h in range(1, self._lookahead + 1)]

    def plan(self) -> List[WarmUpTask]:
        tasks: List[WarmUpTask] = []
        seen_fingerprints = set()

        for target_hour in self._target_hours():
            top_queries = self._store.top_queries_for_hour(target_hour, top_k=50)
            for rec in top_queries:
                if rec.query_fingerprint in seen_fingerprints:
                    continue
                seen_fingerprints.add(rec.query_fingerprint)
                tasks.append(WarmUpTask(
                    query_fingerprint=rec.query_fingerprint,
                    query_text=rec.query_text,
                    model=rec.model,
                    priority=float(rec.hit_count),
                    estimated_response_time_ms=rec.avg_response_time_ms,
                    reason="top_hourly",
                ))

        # Add recurring queries with lower priority
        for fp in self._store.recurring_queries():
            if fp not in seen_fingerprints:
                rec = next(
                    (r for (f, _), r in self._store._records.items() if f == fp), None
                )
                if rec:
                    tasks.append(WarmUpTask(
                        query_fingerprint=fp,
                        query_text=rec.query_text,
                        model=rec.model,
                        priority=1.0,
                        estimated_response_time_ms=rec.avg_response_time_ms,
                        reason="recurring",
                    ))
                    seen_fingerprints.add(fp)

        # Sort by priority and fit within budget
        tasks.sort(key=lambda t: -t.priority)
        selected = []
        budget_used = 0.0
        for task in tasks[:self._max_tasks]:
            if budget_used + task.estimated_response_time_ms > self._budget:
                break
            selected.append(task)
            budget_used += task.estimated_response_time_ms

        return selected
```

## Solution 4: Predictive Cache Warmer

```python
import asyncio
from typing import Any, Callable, List, Optional


class PredictiveCacheWarmer:
    """
    Executes warm-up tasks from the planner, populates the cache,
    and reports warming results.
    """

    def __init__(
        self,
        cache_store: Any,    # any cache with put(fingerprint, model, response) method
        llm_fn: Callable,    # async fn(query, model) -> response
        max_concurrent: int = 3,
        task_timeout_seconds: float = 15.0,
    ) -> None:
        self._cache = cache_store
        self._llm_fn = llm_fn
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timeout = task_timeout_seconds
        self._warmed = 0
        self._failed = 0
        self._skipped = 0

    async def _warm_one(self, task: WarmUpTask) -> bool:
        async with self._semaphore:
            try:
                response = await asyncio.wait_for(
                    self._llm_fn(task.query_text, task.model),
                    timeout=self._timeout,
                )
                self._cache.put(task.query_fingerprint, task.model, response)
                self._warmed += 1
                return True
            except Exception:
                self._failed += 1
                return False

    async def warm(self, tasks: List[WarmUpTask]) -> dict:
        if not tasks:
            return {"warmed": 0, "failed": 0, "skipped": 0}

        results = await asyncio.gather(
            *[self._warm_one(task) for task in tasks],
            return_exceptions=True,
        )

        return {
            "tasks": len(tasks),
            "warmed": self._warmed,
            "failed": self._failed,
            "success_rate": round(self._warmed / max(len(tasks), 1), 4),
        }

    def stats(self) -> dict:
        total = self._warmed + self._failed
        return {
            "total_warmed": self._warmed,
            "total_failed": self._failed,
            "success_rate": round(self._warmed / max(total, 1), 4),
        }
```

## Solution 5: Warming Scheduler

```python
import asyncio
import time
from typing import Optional


class WarmingScheduler:
    """
    Runs predictive cache warming on a schedule — e.g., 30 minutes before
    each peak hour, triggered by a background task.
    """

    def __init__(
        self,
        planner: WarmUpSchedulePlanner,
        warmer: PredictiveCacheWarmer,
        warmup_lead_time_minutes: float = 30.0,
        warmup_interval_seconds: float = 3600.0,
    ) -> None:
        self._planner = planner
        self._warmer = warmer
        self._lead_time = warmup_lead_time_minutes * 60
        self._interval = warmup_interval_seconds
        self._last_run_at: Optional[float] = None
        self._run_count = 0

    async def run_once(self) -> dict:
        tasks = self._planner.plan()
        result = await self._warmer.warm(tasks)
        self._last_run_at = time.time()
        self._run_count += 1
        return result

    async def run_loop(self) -> None:
        """Background loop — run with asyncio.create_task()."""
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)

    def status(self) -> dict:
        return {
            "run_count": self._run_count,
            "last_run_at": self._last_run_at,
            "next_run_in_seconds": (
                round(self._interval - (time.time() - self._last_run_at), 1)
                if self._last_run_at else 0.0
            ),
            **self._warmer.stats(),
        }
```

## Solution 6: Predictive Warming Dashboard

```python
import time


class PredictiveWarmingDashboard:
    """
    Combines warming scheduler status, pattern store stats,
    and next warmup plan into a single performance report.
    """

    def __init__(
        self,
        scheduler: WarmingScheduler,
        planner: WarmUpSchedulePlanner,
        pattern_store: UsagePatternStore,
    ) -> None:
        self._scheduler = scheduler
        self._planner = planner
        self._store = pattern_store

    def render(self) -> dict:
        status = self._scheduler.status()
        next_plan = self._planner.plan()

        return {
            "generated_at": time.time(),
            "scheduler": status,
            "next_warmup": {
                "task_count": len(next_plan),
                "estimated_budget_ms": sum(t.estimated_response_time_ms for t in next_plan),
                "top_queries": [
                    {"query": t.query_text[:80], "priority": t.priority, "reason": t.reason}
                    for t in next_plan[:5]
                ],
            },
        }
```

## Comparison

| Approach | Usage Recording | Pattern Analysis | Warm-Up Planning | Cache Population | Scheduling |
|---|---|---|---|---|---|
| UsagePatternStore | Yes (hour-of-week) | Yes (top queries) | No | No | No |
| WarmUpSchedulePlanner | No | Via store | Yes (priority + budget) | No | No |
| PredictiveCacheWarmer | No | No | No | Yes | No |
| WarmingScheduler | No | No | Via planner | Via warmer | Yes |
| PredictiveWarmingDashboard | No | No | No | No | Yes |

**Best for production**: Run the warming scheduler 30–45 minutes before known peak windows (e.g., 8:30am for a 9am business peak). Set `max_concurrent=3` during warm-up to avoid competing with live traffic on the LLM API rate limit. Use `min_hit_count=3` when selecting warm-up candidates — queries seen only once are noise; queries seen 3+ times in the same hour slot are reliable predictors. Monitor `success_rate` after each warming run: below 80% indicates the LLM API is already under load during the warm-up window and the lead time should be increased.
